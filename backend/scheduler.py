"""Asyncio supervisor — one task per registered check.

Each task loop: run → persist → sleep(cadence − elapsed). Sleep is
interruptible by the stop event so shutdown is prompt.

Failures inside a check don't crash the loop — exceptions become a CheckRun
with ``status="error"`` and the loop keeps going.
"""
from __future__ import annotations
import asyncio
import logging
import random
import socket
import time

from .alarms import suppression as _sup
from .check_labels import pretty_check_label
from .checks.base import Check, CheckResult, utcnow
from .checks.transports import CheckContext
from .db.store import Store
import httpx

from .errors import humanize_error
from .checks.layer0_episode import record_upstream_timeout, in_episode
from .registry import CHECKS

log = logging.getLogger(__name__)


# Substrings + errno markers that indicate the failure is local DNS /
# name-resolution flake, not an upstream-side problem. Matched against
# `str(exception).lower()`. EAI errno values:
#   -2 (EAI_NONAME) — "Name or service not known"
#   -3 (EAI_AGAIN)  — "Temporary failure in name resolution"
#   -5 (EAI_NODATA) — "No address associated with hostname"
_DNS_ERROR_MARKERS = (
    "gaierror",
    "[errno -2]",
    "[errno -3]",
    "[errno -5]",
    "name or service not known",
    "no address associated with hostname",
    "temporary failure in name resolution",
    # Match the friendly form too — backend.errors.humanize_error now
    # rewrites these exception strings before they reach a summary, and
    # the summary-side demote relies on string matching.
    "dns lookup failed",
)


def _is_local_dns_error(e: BaseException) -> bool:
    """Heuristic: does this exception look like our DNS resolver failed?

    True → demote to skip (local infra), False → keep as error (real
    upstream problem). Walks __cause__/__context__ since httpx wraps
    the underlying socket.gaierror.
    """
    cur: BaseException | None = e
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, socket.gaierror):
            return True
        msg = str(cur).lower()
        if any(m in msg for m in _DNS_ERROR_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _summary_looks_like_dns(summary: str | None) -> bool:
    """Companion to _is_local_dns_error for check evaluators that catch the
    transport exception internally and return a CheckResult with the formatted
    message in the summary string (layer1_product / layer3_overlay / layer4_*).
    Without this, those checks fire status=error on DNS flakes and bypass the
    scheduler-level catch-block demotion.
    """
    if not summary:
        return False
    s = summary.lower()
    return any(m in s for m in _DNS_ERROR_MARKERS)


def _find_unhealthy_ancestor(
    check_id: str,
    latest_by_check: dict[str, str],
    deps: dict[str, list[str]],
) -> str | None:
    """BFS over depends_on. Returns the id of the first ancestor whose
    latest status is fail or error, or None. Used by the cascade-demote
    pass below so downstream cells skip during an upstream outage instead
    of all painting themselves red independently."""
    visited: set[str] = set()
    frontier = list(deps.get(check_id, []))
    while frontier:
        nxt: list[str] = []
        for dep_id in frontier:
            if dep_id in visited:
                continue
            visited.add(dep_id)
            st = latest_by_check.get(dep_id)
            if st in ("fail", "error"):
                return dep_id
            nxt.extend(deps.get(dep_id, []))
        frontier = nxt
    return None


# === Load-bearing: de-correlating the check fleet ===
#
# Checks loop on a fixed period, so any set that starts together stays in
# lockstep forever. Measured 2026-08-31: 10-15 checks landing in the same
# second was routine and 33-34 happened, and the restart that afternoon put 9
# read timeouts in the first 79 requests (11%, against a historical 0.1-1.2%)
# purely because everything fired at once. radarca answers a sequential probe
# in 17.9s worst-case but blows past 40s under a concurrent burst during one of
# its slow episodes, so our own synchronization is what converts its slowness
# into our timeouts.
#
# JITTER_FRAC is a fraction of each check's own cadence, so a 60s check is
# nudged less than a 300s one and no check's period is distorted much. It is
# zero-mean: over many cycles the average cadence is unchanged, but phases
# random-walk apart instead of staying locked.
JITTER_FRAC = 0.05          # +/- 5% of cadence
JITTER_MIN_S = 2.0          # ...but always enough to actually separate two checks


def _jittered_delay(cadence_s: float, elapsed_s: float,
                    rand=random.uniform) -> float:
    """Seconds to sleep before this check's next run.

    Subtracts the time the run itself took so the cadence is a period, not a
    gap, then applies zero-mean jitter. Never returns less than 1s: a check
    that overran its whole cadence must still yield to the event loop rather
    than spin.
    """
    base = max(1.0, cadence_s - elapsed_s)
    j = max(JITTER_MIN_S, cadence_s * JITTER_FRAC)
    return max(1.0, base + rand(-j, j))


class Scheduler:
    def __init__(self, store: Store, ctx: CheckContext, engine=None):
        self.store = store
        self.ctx = ctx
        self.engine = engine                        # AlarmEngine | None
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        # optional sync callback for live broadcast; set by the app factory.
        self.on_result = None                       # type: ignore[assignment]
        # In-process latest-status cache used by the cascade-demote pass.
        # Updated after every check run with the final (possibly demoted)
        # status — note that's deliberate: a check that itself got demoted
        # to skip should not then mark its own downstream as cascade-skip
        # (skip ≠ unhealthy).
        self._latest_status: dict[str, str] = {}
        # Pre-built depends_on index. Same shape as AlarmEngine's, but the
        # scheduler walks it independently because cascade-demote needs to
        # run BEFORE the engine sees the result.
        self._depends_on: dict[str, list[str]] = _sup.build_depends_on_index(CHECKS.values())
        # Transitive deps + topological rank — precomputed once for two
        # race-condition guards on top of cascade-demote:
        #
        # (1) await-upstream gate: before a tick runs, wait briefly if any
        #     TRANSITIVE upstream check is currently in-flight, so a
        #     simultaneously-ticking parent's new status is visible to the
        #     demote pass below.
        # (2) topological stagger: on cold start, delay each check's first
        #     tick by `rank * 2s` so the initial wave runs roots-first.
        #     Without this, every check fires at t=0+jitter and the order
        #     in which they complete depends on network round-trip times.
        #
        # Bound the wait at 10 s (max_wait_s) — if an upstream is genuinely
        # hung, we don't block forever. Critical timing is measured in
        # minutes; 10 s is well inside the noise floor.
        self._transitive_deps: dict[str, set[str]] = {
            c.id: self._compute_transitive(c.id) for c in CHECKS.values()
        }
        self._rank: dict[str, int] = self._compute_ranks()
        self._running: set[str] = set()
        self._max_wait_s: float = 10.0

    def _compute_transitive(self, check_id: str) -> set[str]:
        out: set[str] = set()
        frontier = list(self._depends_on.get(check_id, []))
        while frontier:
            nxt: list[str] = []
            for d in frontier:
                if d in out:
                    continue
                out.add(d)
                nxt.extend(self._depends_on.get(d, []))
            frontier = nxt
        return out

    def _compute_ranks(self) -> dict[str, int]:
        """Topological rank — 0 for checks with no deps, otherwise
        max(parent_rank) + 1. Used to stagger initial-start so the
        cascade-demote pass has authoritative upstream state on tick 1."""
        ranks: dict[str, int] = {}
        def rank(cid: str, on_stack: set[str]) -> int:
            if cid in ranks:
                return ranks[cid]
            if cid in on_stack:
                return 0  # cycle guard (shouldn't happen but be safe)
            deps = self._depends_on.get(cid) or []
            if not deps:
                ranks[cid] = 0
                return 0
            on_stack.add(cid)
            r = 1 + max((rank(d, on_stack) for d in deps), default=0)
            on_stack.discard(cid)
            ranks[cid] = r
            return r
        for c in CHECKS.values():
            rank(c.id, set())
        return ranks

    async def _await_upstream_settled(self, check: Check) -> None:
        """Block briefly if any transitive upstream is mid-tick. Bounded
        by self._max_wait_s so a hung upstream doesn't gum up the pipeline.
        Returns immediately when there's nothing in flight that we
        depend on (the common case)."""
        deps = self._transitive_deps.get(check.id) or set()
        if not deps:
            return
        deadline = time.monotonic() + self._max_wait_s
        # Poll the in-flight set. asyncio.Event-per-check would be more
        # elegant but the polling cost is trivial at 36 checks × 5 Hz.
        while any(d in self._running for d in deps):
            if time.monotonic() >= deadline:
                # Cap reached — log once and proceed. Demote pass below
                # will still apply with whatever upstream status the
                # cache has at this moment.
                log.warning(
                    "check %s proceeding without upstream settle (waited %.1fs)",
                    check.id, self._max_wait_s,
                )
                return
            await asyncio.sleep(0.2)

    async def start(self) -> None:
        # Seed latest-status from the store so cascade-demote works on
        # tick 1 after a backend restart — without this, the first wave
        # of downstream checks would each emit one independent failure
        # before the cache fills.
        try:
            rows = await self.store.latest_per_check_map()
            for cid, row in rows.items():
                self._latest_status[cid] = row.get("status") or "skip"
        except Exception:
            log.exception("scheduler: latest-status seed failed; cascade-demote starts empty")
        # Topological-stagger initial-start. Root checks (rank 0) start
        # immediately; deeper layers wait `rank * 2s` so the first wave
        # propagates roots → leaves in order.
        # Was 2.0s with a 0-1s jitter inside _loop, which spread ~20 rank-0
        # checks across one second — the boot thundering herd. Widened so the
        # within-rank spread fills the whole stagger step: rank N now occupies
        # [N*STAGGER_S, (N+1)*STAGGER_S), so ranks still start in order and
        # never overlap, while the checks inside a rank fan out across the
        # full window. Max rank is 3, so a cold start is fully launched in
        # ~32s instead of ~7s, which is the right trade for not opening with a
        # burst that upstream answers with timeouts.
        STAGGER_S = 8.0
        for check in CHECKS.values():
            initial_delay = (self._rank.get(check.id, 0) * STAGGER_S
                             + random.uniform(0.0, STAGGER_S))
            t = asyncio.create_task(self._loop(check, initial_delay),
                                     name=f"check:{check.id}")
            self._tasks.append(t)
        log.info("scheduler started with %d checks (max topological rank %d)",
                 len(self._tasks), max(self._rank.values(), default=0))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        log.info("scheduler stopped")

    def _maybe_downgrade_for_dns_summary(self, check: Check, result: CheckResult) -> CheckResult:
        """If a check evaluator caught its own transport error and packed the
        DNS marker into result.summary, demote the row to skip + reason=
        local_dns_error so the timeline doesn't paint it red as if it were an
        upstream outage. Mirrors the catch-block demote in _loop. The network
        control check is exempt — it must surface real DNS state.
        """
        if result.status not in ("fail", "error"):
            return result
        if check.id.startswith("layer0.net."):
            return result
        if not _summary_looks_like_dns(result.summary):
            return result
        log.info("check %s demoted on DNS marker in summary: %s",
                 check.id, result.summary)
        return CheckResult(
            check_id=result.check_id, target=result.target, stage=result.stage,
            status="skip",
            started_at=result.started_at, finished_at=result.finished_at,
            summary=f"local DNS unavailable: {result.summary}",
            payload={
                **(result.payload or {}),
                "reason":          "local_dns_error",
                "original_status": result.status,
                "original_summary": result.summary,
            },
            metrics=result.metrics,
        )

    def _maybe_demote_for_unhealthy_dep(self, check: Check, result: CheckResult) -> CheckResult:
        """If any depends_on ancestor's current status is unhealthy, demote
        this fail/error to skip. Avoids painting 37 red downstream cells
        every time one upstream check goes red. The alarm engine still
        records `suppressed_by` on the side for the audit trail; this
        demote just keeps the dashboard honest about what's actually
        broken vs what's collateral damage.

        Skipped here exactly when:
          - result is fail/error (skip / pass / warn are untouched)
          - check has depends_on, and some ancestor's latest status is
            fail/error per the in-process cache.

        Original status + summary live in payload so the drilldown can
        still surface the raw observation for forensics.
        """
        if result.status not in ("fail", "error"):
            return result
        suppressor = _find_unhealthy_ancestor(check.id, self._latest_status, self._depends_on)
        if not suppressor:
            return result
        # Use the friendly upstream name in the user-facing summary so
        # the message reads naturally ("Origin reachable" not
        # "layer0.origin.alive"). The raw check_id is still preserved in
        # payload.suppressed_by for routing / drilldown deeplinks.
        upstream_check = CHECKS.get(suppressor)
        upstream_label = pretty_check_label(
            suppressor, upstream_check.target if upstream_check else "",
        )
        log.info("check %s demoted to skip — upstream %s unhealthy", check.id, suppressor)
        return CheckResult(
            check_id=result.check_id, target=result.target, stage=result.stage,
            status="skip",
            started_at=result.started_at, finished_at=result.finished_at,
            summary=f"Upstream \"{upstream_label}\" unhealthy",
            payload={
                **(result.payload or {}),
                "reason":           "upstream_unhealthy",
                "suppressed_by":    suppressor,
                "original_status":  result.status,
                "original_summary": result.summary,
            },
            metrics=result.metrics,
        )

    def _maybe_downgrade_for_network(self, check: Check, result: CheckResult) -> CheckResult:
        net = getattr(self.ctx, "network", None)
        if net is None or net.online:
            return result
        if result.status not in ("fail", "error"):
            return result
        # Don't muzzle the control check itself.
        if check.id.startswith("layer0.net."):
            return result
        snap = net.snapshot()
        return CheckResult(
            check_id=result.check_id, target=result.target, stage=result.stage,
            status="skip",
            started_at=result.started_at, finished_at=result.finished_at,
            summary="Sentinel Internet offline",
            payload={
                "reason":            "local_network_offline",
                "original_status":   result.status,
                "original_summary":  result.summary,
                "network":           snap,
            },
        )

    async def _loop(self, check: Check, initial_delay_s: float = 0.0) -> None:
        # The caller already folded the within-rank spread into this value
        # (see STAGGER_S in start); adding more here would blur rank ordering.
        await asyncio.sleep(initial_delay_s)
        while not self._stop.is_set():
            # Block if any transitive upstream is mid-tick. Bounded.
            await self._await_upstream_settled(check)
            self._running.add(check.id)
            t0 = utcnow()
            try:
                result = await check.run(self.ctx)
            except asyncio.CancelledError:
                self._running.discard(check.id)
                raise
            except Exception as e:  # noqa: BLE001 — we want to swallow everything
                # Local DNS / name-resolution flakes shouldn't fire alarms.
                # If the failure looks like our resolver, demote to skip
                # so the timeline stays clean and the operator doesn't
                # get paged on a transient blip on our side. The network
                # control check itself is exempt — it MUST surface real
                # offline state.
                if _is_local_dns_error(e) and not check.id.startswith("layer0.net."):
                    log.info("check %s skipped on local DNS flake: %s: %s",
                             check.id, type(e).__name__, e)
                    result = CheckResult(
                        check_id=check.id, target=check.target, stage=check.stage,
                        status="skip",
                        started_at=t0, finished_at=utcnow(),
                        summary=f"Local DNS unavailable ({humanize_error(e)})",
                        payload={
                            "reason":    "local_dns_error",
                            "exception": type(e).__name__,
                            "message":   str(e),
                        },
                    )
                else:
                    log.exception("check %s raised", check.id)
                    # A read timeout means upstream accepted the connection and
                    # then went quiet — it is alive but saturated. Record it so
                    # layer0.origin.episode can tell one upstream slow episode
                    # from N independent failures, and tag the payload as a gap
                    # in OUR visibility rather than evidence the monitored thing
                    # is broken. The verdict stays `error` either way: the cell
                    # must keep saying we could not measure.
                    is_read_timeout = isinstance(e, httpx.ReadTimeout)
                    payload = {"exception": type(e).__name__, "message": str(e)}
                    metrics: dict[str, float] = {}
                    if is_read_timeout:
                        record_upstream_timeout(check.id, t0)
                        payload["reason"] = "upstream_api"
                        payload["episode"] = in_episode()
                        # === Load-bearing: make the censoring visible ===
                        #
                        # A timed-out request writes no latency_ms, so every
                        # percentile over that metric is conditioned on NOT
                        # timing out. The distribution is censored at the
                        # ceiling and can never show a value above it — which
                        # is how "p99 is 19.2s, so our 20s timeout is not
                        # cutting into traffic" got believed while 1-2% of runs
                        # were being killed at exactly 20s.
                        #
                        # `timed_out` is that missing mass. Read it as the
                        # fraction of attempts the ceiling truncated:
                        #   SELECT avg(value) FROM metric_samples
                        #   WHERE metric = 'timed_out'
                        # and treat any latency percentile above (1 - that) as
                        # unmeasured rather than as the value it reports.
                        # `latency_censored_ms` records where we gave up, which
                        # is a lower bound on what the request would have taken.
                        elapsed_ms = (utcnow() - t0).total_seconds() * 1000
                        # Always meaningful on its own: "we gave up at X ms",
                        # a lower bound on what the request would have taken.
                        metrics["latency_censored_ms"] = elapsed_ms
                        # Only for checks that also emit the 0 case. Emitting
                        # it for every check would give `timed_out` a
                        # denominator made only of failures — measured 0.87 on
                        # first deploy, which reads as "87% of requests time
                        # out" and is pure artefact.
                        if getattr(check, "reports_timeout_rate", False):
                            metrics["timed_out"] = 1.0
                    result = CheckResult(
                        check_id=check.id, target=check.target, stage=check.stage,
                        status="error",
                        started_at=t0, finished_at=utcnow(),
                        summary=humanize_error(e),
                        payload=payload,
                        metrics=metrics,
                    )

            # Local-network blame shield: if our own internet is down (per the
            # NetworkMonitor) and the check came back fail/error, demote to
            # `skip` so we don't fire alarms or draw red cells for what is
            # actually a problem on our side.
            #
            # Exempt the control check itself (it MUST surface offline as
            # fail) and the synthetic L4 'reason' skips (already skip).
            # Same DNS-flake suppression as the exception catch above, but
            # applied to results whose check evaluator caught the transport
            # error internally and returned status=error with the DNS marker
            # in summary. Without this, layer1_product / layer3_overlay /
            # layer4_image rows still painted red on local DNS hiccups.
            result = self._maybe_downgrade_for_dns_summary(check, result)
            result = self._maybe_downgrade_for_network(check, result)
            # Cascade-demote: the await-upstream gate above ensures that
            # by the time we reach this line, any concurrently-ticking
            # parent has already updated self._latest_status. So this
            # demote pass sees authoritative dep state.
            result = self._maybe_demote_for_unhealthy_dep(check, result)

            # Free the in-flight slot + update the latest-status cache
            # BEFORE writing to the store / firing the engine, so
            # downstreams that are waiting on us pick up the new state
            # the moment we hand control back to the loop.
            self._latest_status[check.id] = result.status
            self._running.discard(check.id)

            try:
                await self.store.write_check_run(result)
            except Exception:
                log.exception("failed to persist %s", check.id)

            if self.engine is not None:
                try:
                    await self.engine.evaluate(result)
                except Exception:
                    log.exception("alarm engine evaluate(%s) failed", check.id)

            if self.on_result is not None:
                try:
                    self.on_result(result)
                except Exception:
                    log.exception("on_result broadcast for %s failed", check.id)

            elapsed = (utcnow() - t0).total_seconds()
            delay = _jittered_delay(check.cadence_s, elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                # if we get here the stop event fired
                return
            except asyncio.TimeoutError:
                pass
