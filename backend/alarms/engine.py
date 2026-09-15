"""Alarm engine — state machine + periodic dispatch ticker.

Two responsibilities, kept in one class so the lifecycle is obvious:

  1. ``evaluate(result)`` — called by the scheduler after every CheckResult.
     Opens / updates / closes alarm rows based on the run's status.

  2. ``run()`` — background loop. Every TICK_S seconds: for each open,
     unacked, unsuppressed, unsilenced alarm, fire any policy steps whose
     delay has elapsed (and which we haven't already fired). Honors
     repeat_interval for periodic re-pings.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..registry import CHECKS
from . import silences as silence_lib
from . import suppression as sup_lib
from .models import AlertsConfig
from .router import Router, severity_for_status, _SEV_RANK
from .sinks import build_sinks

log = logging.getLogger(__name__)
TICK_S = 15


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AlarmEngine:
    # Auto-close ACKed alarms whose underlying check has been pass/skip
    # for at least this many seconds. Catches alarms left orphaned when a
    # check transitions warn→skip (heuristic fix, dependency change) and
    # the close-on-pass handler in evaluate() doesn't fire. Run cadence
    # for the cleanup loop:
    AUTOCLOSE_CLEAN_S = 6 * 3600   # 6 h of clean runs required to close
    AUTOCLOSE_CADENCE_S = 1800     # run the sweep at most every 30 min

    def __init__(self, store, cfg: AlertsConfig | None = None):
        self.store = store
        self.cfg = cfg if cfg is not None else AlertsConfig()
        self.router = Router(self.cfg)
        self.sinks = build_sinks(self.cfg)
        # include_alarm_only: correlation deps suppress duplicate pages but
        # must not demote verdicts (that is the scheduler's index, built without).
        self.depends_on_index = sup_lib.build_depends_on_index(
            CHECKS.values(), include_alarm_only=True
        )
        self._stop = asyncio.Event()
        self._ticker_task: asyncio.Task | None = None
        # listeners notified on alarm/run events (P1.5 WebSocket)
        self._listeners: list = []
        # last time we ran the stale-ACKed auto-close sweep
        self._last_autoclose: datetime | None = None

    async def _patch_smtp_from_settings(self, cfg: AlertsConfig) -> None:
        """Pull canonical SMTP from settings.smtp into ``cfg.smtp``.

        Without this, any caller that hands us a new AlertsConfig (e.g. the
        /admin/alerts PUT serializing back a config whose opaque smtp field
        is None) would wipe the email sink. settings.smtp is the source of
        truth — alerts_config.smtp is legacy and intentionally ignored.
        """
        from ..auth.email import load_smtp_settings, to_alerts_smtp_dict
        from .models import SmtpConfig
        smtp_settings = await load_smtp_settings(self.store.pool)
        if smtp_settings is None:
            cfg.smtp = None
        else:
            cfg.smtp = SmtpConfig.model_validate(to_alerts_smtp_dict(smtp_settings))

    async def reload(self, new_cfg: AlertsConfig) -> None:
        """Hot-swap the engine's config. Triggered from the admin UI after
        a routing-config edit lands so changes don't require a restart."""
        await self._patch_smtp_from_settings(new_cfg)
        old_sinks = self.sinks
        self.cfg = new_cfg
        self.router = Router(new_cfg)
        self.sinks = build_sinks(new_cfg)
        wh = old_sinks.get("webhook") if isinstance(old_sinks, dict) else None
        if wh and hasattr(wh, "aclose"):
            try:
                await wh.aclose()
            except Exception:
                pass

    async def refresh_smtp(self, pool=None) -> None:
        """Reload SMTP from settings.smtp and rebuild sinks. Called by the
        /admin/email PUT so changes take effect without a process restart.
        The ``pool`` argument is unused but kept for backwards compat."""
        await self._patch_smtp_from_settings(self.cfg)
        old_sinks = self.sinks
        self.sinks = build_sinks(self.cfg)
        wh = old_sinks.get("webhook") if isinstance(old_sinks, dict) else None
        if wh and hasattr(wh, "aclose"):
            try:
                await wh.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self):
        self._ticker_task = asyncio.create_task(self.run(), name="alarm-ticker")
        log.info(
            "alarm engine started — %d routes, %d receivers, sinks=%s",
            len(self.cfg.routes), len(self.cfg.receivers), sorted(self.sinks.keys()),
        )

    async def stop(self):
        self._stop.set()
        if self._ticker_task:
            self._ticker_task.cancel()
            try:
                await self._ticker_task
            except (asyncio.CancelledError, Exception):
                pass
        # close webhook client if any
        wh = self.sinks.get("webhook")
        if wh and hasattr(wh, "aclose"):
            await wh.aclose()

    def add_listener(self, fn):
        """Register a callable(event_type, payload) -> awaitable for WS push.
        ``event_type`` ∈ {alarm_open, alarm_close, alarm_promote, run}."""
        self._listeners.append(fn)

    async def _emit(self, event_type: str, payload):
        for fn in self._listeners:
            try:
                await fn(event_type, payload)
            except Exception:
                log.exception("listener %r raised", fn)

    # ------------------------------------------------------------------
    # State machine — called on every CheckResult
    # ------------------------------------------------------------------
    async def evaluate(self, result):
        existing = await self.store.find_open_alarm(result.check_id, result.target)
        # An INCONCLUSIVE skip is not news. The scheduler demotes fail/error
        # to skip when an upstream dependency is unhealthy (or our own DNS /
        # uplink is down) so the dashboard doesn't go all-red over one fault
        # — but that is a decision not to judge, not an observation of
        # recovery. Closing on it is actively harmful: for a target that is
        # permanently down, every upstream blip closes the alarm, the next
        # real fail opens a NEW one, and escalation restarts from step 1.
        # Leave the alarm exactly as it is and wait for a real verdict.
        if sup_lib.is_inconclusive(result.status, result.payload):
            return

        # Treat an INTRINSIC skip the same as pass for alarm bookkeeping. A
        # check that returns skip ran but couldn't make a meaningful
        # assessment (e.g. a forecast product whose only differentiating
        # sub-checks are themselves skip-eligible). It's not a problem
        # state, so we close any existing alarm and don't open a new one.
        if result.status in ("pass", "skip"):
            if existing:
                await self.store.close_alarm(existing["id"], when=result.finished_at)
                await self._emit("alarm_close", {**dict(existing), "closed_at": result.finished_at.isoformat()})
            return
        if existing:
            # alarm already open — let the ticker handle promotions; no new row
            return

        # === Hold-down: has this actually persisted? ===
        #
        # Route is resolved from the result rather than from an alarm row,
        # because there is no alarm row yet — that is the decision being made.
        hold_s = self.cfg.hold_down_for(
            self.router.match(
                {"check_id": result.check_id, "target": result.target,
                 "stage": result.stage, "status_at_open": result.status},
                {},
            )
        )
        if hold_s > 0:
            try:
                started = await self.store.non_pass_streak_start(
                    result.check_id, result.target
                )
            except Exception:
                # Never let a hold-down lookup swallow a real alarm. Failing
                # open here means at worst the old, noisier behavior.
                log.exception("hold-down lookup failed for %s; opening anyway",
                              result.check_id)
                started = None
            if started is not None:
                persisted = (result.finished_at - started).total_seconds()
                if persisted < hold_s:
                    log.debug(
                        "hold-down: %s non-pass for %.0fs (< %ds) — not opening yet",
                        result.check_id, persisted, hold_s,
                    )
                    return

        # open a new one
        sev = severity_for_status(result.status)
        latest_status_by_check = {
            cid: row["status"]
            for cid, row in (await self.store.latest_per_check_map()).items()
        }
        suppressed_by = sup_lib.compute_suppression(
            result.check_id, latest_status_by_check, self.depends_on_index
        )
        alarm_id = await self.store.open_alarm(
            check_id=result.check_id,
            target=result.target,
            stage=result.stage,
            severity=sev,
            opened_at=result.finished_at,
            suppressed_by=suppressed_by,
            message=result.summary,
            payload={
                "status_at_open": result.status,
                "result_payload": result.payload,
            },
        )
        row = await self.store.fetch_alarm(alarm_id)
        await self._emit("alarm_open", dict(row))
        if suppressed_by:
            log.info("alarm %s opened SUPPRESSED by %s", result.check_id, suppressed_by)
        else:
            log.info("alarm %s opened (severity=%s)", result.check_id, sev)

    # ------------------------------------------------------------------
    # Ticker — drives escalation + repeats + duration-promotion
    # ------------------------------------------------------------------
    async def run(self):
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                log.exception("alarm tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_S)
                return
            except asyncio.TimeoutError:
                pass

    async def _tick(self):
        rows = await self.store.list_open_alarms()
        now = _utcnow()

        # Periodic stale-ACKed sweep. Cheap query, runs every 30 min, and
        # ensures alarms acknowledged but no longer relevant don't linger
        # forever (e.g. when a check goes warn→skip after a heuristic
        # change). Independent of `rows` — needs to run even if all open
        # alarms got list-filtered to empty above.
        if (self._last_autoclose is None
            or (now - self._last_autoclose).total_seconds() >= self.AUTOCLOSE_CADENCE_S):
            try:
                await self._close_stale_acked(now)
            except Exception:
                log.exception("auto-close stale ACKed alarms failed")
            self._last_autoclose = now

        if not rows:
            return
        active_silences = await self.store.list_active_silences(now)
        # One snapshot of every check's latest status, shared by all open
        # alarms this tick. Both the suppression re-evaluation and the
        # current-status severity read need it, and neither is worth a
        # per-alarm query.
        try:
            latest = {
                cid: row["status"]
                for cid, row in (await self.store.latest_per_check_map()).items()
            }
        except Exception:
            # Fail closed onto the old behavior rather than dropping a tick:
            # without the snapshot, suppression and severity simply stay as
            # they were, which is what they did before this existed.
            log.exception("latest-status snapshot failed; tick degrades to "
                          "open-time severity and frozen suppression")
            latest = {}
        for alarm in rows:
            try:
                await self._process(alarm, now, active_silences, latest)
            except Exception:
                log.exception("processing alarm %s", alarm.get("id"))

    async def _close_stale_acked(self, now: datetime) -> None:
        """Close ACKed alarms whose underlying check has been cleanly
        pass/skip for AUTOCLOSE_CLEAN_S. Single SQL pass; emits alarm_close
        for each so the UI updates.

        "Cleanly" excludes inconclusive skips (see alarms.suppression): a
        window of demoted skips means we stopped judging, not that the
        target recovered, and must not be enough to retire an alarm.
        """
        pool = self.store.pool
        inconclusive = list(sup_lib.INCONCLUSIVE_SKIP_REASONS)
        # Note: alarm_acks may have multiple rows per alarm (re-acks). The
        # EXISTS subquery just checks "ever acked."
        rows = await pool.fetch(
            f"""
            SELECT a.id, a.check_id, a.target, a.stage, a.severity,
                   a.opened_at, a.message, a.payload
            FROM alarms a
            WHERE a.closed_at IS NULL
              AND EXISTS (SELECT 1 FROM alarm_acks k WHERE k.alarm_id = a.id)
              AND NOT EXISTS (
                SELECT 1 FROM check_runs r
                WHERE r.check_id = a.check_id
                  AND r.target   = a.target
                  AND r.finished_at > now() - interval '{self.AUTOCLOSE_CLEAN_S} seconds'
                  AND (r.status NOT IN ('pass', 'skip')
                       OR (r.status = 'skip'
                           AND coalesce(r.payload->>'reason', '') = ANY($1::text[])))
              )
              AND EXISTS (
                SELECT 1 FROM check_runs r
                WHERE r.check_id = a.check_id
                  AND r.target   = a.target
                  AND r.finished_at > now() - interval '{self.AUTOCLOSE_CLEAN_S} seconds'
                  AND (r.status = 'pass'
                       OR (r.status = 'skip'
                           AND coalesce(r.payload->>'reason', '') <> ALL($1::text[])))
              )
            """,
            inconclusive,
        )
        for row in rows:
            await self.store.close_alarm(row["id"], when=now)
            log.info(
                "auto-closed stale ACKed alarm %s (%s/%s): no non-OK runs in %dh",
                row["id"], row["check_id"], row["target"], self.AUTOCLOSE_CLEAN_S // 3600,
            )
            await self._emit("alarm_close", {
                **dict(row),
                "closed_at": now.isoformat(),
                "auto_close_reason": "acked_and_recovered",
            })

    async def _process(self, alarm: dict, now: datetime, silences: list[dict],
                       latest: dict[str, str] | None = None):
        latest = latest or {}
        status_now = latest.get(alarm["check_id"])
        if status_now:
            alarm = {**alarm, "status_now": status_now}

        # === Suppression, re-evaluated ===
        #
        # This used to be a bare `if alarm["suppressed_by"]: return`, reading a
        # column written once at open time. A cascade attribution is a claim
        # about the present ("the origin is down, so of course this is too"),
        # and it was being stored as though it were a claim about the alarm.
        #
        # On 2026-09-13 21:40 four product alarms opened attributed to
        # layer0.origin.episode. The episode passed. The products did not —
        # they went on to serve an empty manifest from 02:22 to 14:10 the next
        # day — and the stale attribution held all four silent for 17h 30m.
        if alarm["suppressed_by"]:
            if not latest:
                return                      # no snapshot: leave it as it was
            still = sup_lib.compute_suppression(
                alarm["check_id"], latest, self.depends_on_index,
            )
            if still:
                if still != alarm["suppressed_by"]:
                    await self.store.update_alarm_suppression(alarm["id"], still)
                    alarm["suppressed_by"] = still
                return
            await self.store.update_alarm_suppression(alarm["id"], None)
            alarm["suppressed_by"] = None
            log.info("alarm %s no longer suppressed — cause cleared, alarm stands",
                     alarm["id"])
            await self._emit("alarm_unsuppress", {"id": alarm["id"]})

        sil = silence_lib.find_active_silence(silences, alarm, now)
        if sil:
            return

        # Route lookup
        ctx = {"now": now, "alarm": alarm, "open_alarms": [], "latest_runs": {}}
        # populate open_alarms only when needed (kept lazy)
        route = self.router.match(alarm, ctx)
        if route is None:
            return

        # === Severity (floor + duration + current status) ===
        #
        # Computed BEFORE the ack check, deliberately. An ack is a statement
        # about notification — "stop telling me" — not about how bad the thing
        # is, and the two were conflated: `is_acked` returned before this line,
        # so acknowledging an alarm froze its severity for the rest of its
        # life. On 2026-09-14 one bulk ack at 04:00 pinned five alarms,
        # including a 14h 06m total stall, at the severity they happened to
        # hold ninety minutes in.
        new_sev = self.router.compute_severity(alarm, route, now)
        if new_sev != alarm["severity"]:
            await self.store.update_alarm_severity(alarm["id"], new_sev)
            alarm["severity"] = new_sev
            await self._emit("alarm_promote", {"id": alarm["id"], "severity": new_sev})

        # === Ack ===
        #
        # Silences notification only, and only while it still describes what is
        # happening. See _ack_still_covers.
        if await self.store.is_acked(alarm["id"]):
            if await self._ack_still_covers(alarm, status_now):
                return
            await self.store.unack_alarm(alarm["id"])
            log.info("alarm %s ack lapsed — condition worsened to severity=%s "
                     "status=%s", alarm["id"], alarm["severity"], status_now)
            await self._emit("alarm_ack_lapsed", {
                "id": alarm["id"], "severity": alarm["severity"],
                "status": status_now,
            })

        # Which steps are due AND not yet fired?
        policy = self.cfg.policy(route.policy)
        if policy is None:
            return
        for step_idx, step in enumerate(policy.steps):
            due_at = alarm["opened_at"] + timedelta(seconds=step.delay_s)
            if due_at > now:
                break
            already = await self.store.notification_count_for_step(
                alarm["id"], step_idx
            )
            if already > 0:
                # has the repeat_interval passed since last send?
                if not route.repeat_interval_s:
                    continue
                last = await self.store.last_notification_at(alarm["id"], step_idx)
                if last and (now - last).total_seconds() < route.repeat_interval_s:
                    continue
            await self._dispatch(alarm, route, step, step_idx)

    # Status severity for the ack comparison. Deliberately separate from the
    # alarm severity ladder: this ranks how bad the CHECK is, not how loudly
    # we page about it.
    _STATUS_RANK = {"pass": 0, "skip": 0, "warn": 1, "fail": 2, "error": 2}

    async def _ack_still_covers(self, alarm: dict, status_now: str | None) -> bool:
        """Does the ack still describe what is happening?

        An ack means "I have seen this, stop telling me". It should not also
        mean "and stop telling me about whatever this becomes". Ack the
        degraded thing at 04:00 and it is reasonable to hear nothing more
        about the degraded thing; it is not reasonable to hear nothing when
        the product stops serving data entirely at 04:10.

        That is what happened. Five alarms acked in one sweep at 2026-09-14
        04:00:38 covered, among others, a nowcast that had stopped publishing
        ninety minutes earlier and did not resume for another eleven hours.
        Zero notifications were sent for any of them.

        Lapses when the check status or the alarm severity has risen above
        what was on record at ack time. Rows acked before those columns
        existed have no baseline, and a missing baseline keeps the ack — an
        ack that cannot be compared is left alone rather than revoked on a
        guess.
        """
        try:
            st = await self.store.ack_state(alarm["id"])
        except Exception:
            log.exception("ack_state lookup failed for alarm %s; honoring ack",
                          alarm["id"])
            return True
        if not st:
            return True                     # raced with an un-ack

        sev_at = st.get("severity_at_ack")
        if sev_at and _SEV_RANK.get(alarm["severity"], 0) > _SEV_RANK.get(sev_at, 0):
            return False

        status_at = st.get("status_at_ack")
        if status_at and status_now:
            if self._STATUS_RANK.get(status_now, 0) > \
               self._STATUS_RANK.get(status_at, 0):
                return False
        return True

    async def _expand_receiver_groups(self, recv, when):
        """Apply group expansion + schedule gate to a receiver.

        Returns a Receiver clone with emails expanded, or None if the
        receiver should be skipped (all referenced groups off-duty). When
        group_ids is empty this is the identity function — direct-email-only
        receivers behave exactly as before.

        Schedule check uses backend.groups.effective_is_active which walks
        the parent chain so an inherited downtime/window suppresses the
        child correctly.
        """
        from .. import groups as _groups
        gids = list(recv.group_ids or [])
        if not gids:
            return recv
        active_emails: list[str] = []
        any_active = False
        for gid in gids:
            try:
                if not await _groups.effective_is_active(self.store.pool, gid, when):
                    continue
                any_active = True
                rows = await self.store.pool.fetch(
                    "SELECT u.email FROM group_members gm "
                    "JOIN users u ON u.id = gm.user_id "
                    "WHERE gm.group_id = $1 AND u.disabled_at IS NULL "
                    "  AND u.email IS NOT NULL",
                    gid,
                )
                active_emails.extend(r["email"] for r in rows)
            except Exception:
                log.exception("group expansion failed for gid=%s", gid)
        if not any_active:
            return None
        merged = list(dict.fromkeys([*recv.email, *active_emails]))  # dedupe, keep order
        return recv.model_copy(update={"email": merged})

    async def _dispatch(self, alarm, route, step, step_idx):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        # Dedup overlap: if user A is reachable through two different
        # receivers (direct email + group membership) inside the same
        # step, send them ONE message, not two. Track emails already
        # claimed by an earlier receiver in this step and remove them
        # from later receivers' email lists. Webhook + console don't
        # have an addressee identity, so they aren't deduped here.
        emails_seen_this_step: set[str] = set()

        # Synthesize an anonymous receiver per direct-named group so the
        # rest of the dispatch loop treats it uniformly. The synthetic
        # receiver carries only group_ids; _expand_receiver_groups does
        # the schedule check + member expansion.
        from .models import Receiver as _Receiver
        synthetic_receivers: list[_Receiver] = []
        for gid in getattr(step, "group_ids", []) or []:
            synthetic_receivers.append(_Receiver(
                name=f"group:{gid}",
                group_ids=[int(gid)],
            ))

        all_recvs: list[tuple[str, _Receiver | None]] = []
        for r in synthetic_receivers:
            all_recvs.append((r.name, r))
        for receiver_name in step.receivers:
            all_recvs.append((receiver_name, None))

        for receiver_name, synth in all_recvs:
            recv = synth if synth is not None else self.cfg.receiver(receiver_name)
            if recv is None:
                log.warning("route refs unknown receiver %r", receiver_name)
                continue
            # Group expansion + schedule gate. If the receiver references
            # any groups and ALL of them are inactive (downtime / off-week
            # / outside time-windows), skip the receiver entirely — the
            # whole on-call cohort is off duty. Active groups contribute
            # their members' emails to the dispatch list.
            recv = await self._expand_receiver_groups(recv, now)
            if recv is None:
                log.info("receiver %r skipped: all referenced groups are off-duty",
                         receiver_name)
                continue
            # Drop emails an earlier receiver in this step already covered.
            if recv.email:
                fresh = [e for e in recv.email if e.lower() not in emails_seen_this_step]
                if not fresh and not recv.webhook and not recv.console:
                    log.info(
                        "receiver %r skipped: all emails already notified earlier in step",
                        receiver_name,
                    )
                    continue
                if fresh != recv.email:
                    recv = recv.model_copy(update={"email": fresh})
                emails_seen_this_step.update(e.lower() for e in fresh)
            channels = [c for c in ("email", "webhook", "console")
                        if getattr(recv, c, None) or (c == "console" and recv.console)]
            for ch in channels:
                sink = self.sinks.get(ch)
                if sink is None:
                    await self.store.write_notification(
                        alarm_id=alarm["id"], receiver=receiver_name, channel=ch,
                        step_idx=step_idx, template=None,
                        body_excerpt=None, status="failed",
                        error="sink unavailable")
                    continue
                try:
                    sr = await sink.send(alarm, recv, route, step_idx)
                    await self.store.write_notification(
                        alarm_id=alarm["id"], receiver=receiver_name, channel=ch,
                        step_idx=step_idx, template=recv.template,
                        body_excerpt=sr.body_excerpt,
                        status="sent" if sr.delivered else "failed",
                        error=sr.error)
                except Exception as e:
                    log.exception("sink %s raised", ch)
                    await self.store.write_notification(
                        alarm_id=alarm["id"], receiver=receiver_name, channel=ch,
                        step_idx=step_idx, template=None,
                        body_excerpt=None, status="failed",
                        error=str(e))
