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
from .router import Router, severity_for_status
from .sinks import build_sinks

log = logging.getLogger(__name__)
TICK_S = 15


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AlarmEngine:
    def __init__(self, store, cfg: AlertsConfig | None = None):
        self.store = store
        self.cfg = cfg if cfg is not None else AlertsConfig()
        self.router = Router(self.cfg)
        self.sinks = build_sinks(self.cfg)
        self.depends_on_index = sup_lib.build_depends_on_index(CHECKS.values())
        self._stop = asyncio.Event()
        self._ticker_task: asyncio.Task | None = None
        # listeners notified on alarm/run events (P1.5 WebSocket)
        self._listeners: list = []

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
        if result.status == "pass":
            if existing:
                await self.store.close_alarm(existing["id"], when=result.finished_at)
                await self._emit("alarm_close", {**dict(existing), "closed_at": result.finished_at.isoformat()})
            return
        if existing:
            # alarm already open — let the ticker handle promotions; no new row
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
        if not rows:
            return
        now = _utcnow()
        active_silences = await self.store.list_active_silences(now)
        for alarm in rows:
            try:
                await self._process(alarm, now, active_silences)
            except Exception:
                log.exception("processing alarm %s", alarm.get("id"))

    async def _process(self, alarm: dict, now: datetime, silences: list[dict]):
        if alarm["suppressed_by"]:
            return
        if await self.store.is_acked(alarm["id"]):
            return
        sil = silence_lib.find_active_silence(silences, alarm, now)
        if sil:
            return

        # Route lookup
        ctx = {"now": now, "alarm": alarm, "open_alarms": [], "latest_runs": {}}
        # populate open_alarms only when needed (kept lazy)
        route = self.router.match(alarm, ctx)
        if route is None:
            return

        # Severity (with floor + duration promotion)
        new_sev = self.router.compute_severity(alarm, route, now)
        if new_sev != alarm["severity"]:
            await self.store.update_alarm_severity(alarm["id"], new_sev)
            alarm["severity"] = new_sev
            await self._emit("alarm_promote", {"id": alarm["id"], "severity": new_sev})

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

    async def _dispatch(self, alarm, route, step, step_idx):
        for receiver_name in step.receivers:
            recv = self.cfg.receiver(receiver_name)
            if recv is None:
                log.warning("route refs unknown receiver %r", receiver_name)
                continue
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
