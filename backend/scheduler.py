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

from .checks.base import Check, CheckResult, utcnow
from .checks.transports import CheckContext
from .db.store import Store
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


class Scheduler:
    def __init__(self, store: Store, ctx: CheckContext, engine=None):
        self.store = store
        self.ctx = ctx
        self.engine = engine                        # AlarmEngine | None
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        # optional sync callback for live broadcast; set by the app factory.
        self.on_result = None                       # type: ignore[assignment]

    async def start(self) -> None:
        for check in CHECKS.values():
            t = asyncio.create_task(self._loop(check), name=f"check:{check.id}")
            self._tasks.append(t)
        log.info("scheduler started with %d checks", len(self._tasks))

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
            summary="local network offline — upstream not reachable",
            payload={
                "reason":            "local_network_offline",
                "original_status":   result.status,
                "original_summary":  result.summary,
                "network":           snap,
            },
        )

    async def _loop(self, check: Check) -> None:
        # initial random jitter (0..1 s) to spread the first wave
        await asyncio.sleep(random.uniform(0, 1.0))
        while not self._stop.is_set():
            t0 = utcnow()
            try:
                result = await check.run(self.ctx)
            except asyncio.CancelledError:
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
                        summary=f"local DNS unavailable: {type(e).__name__}: {e}",
                        payload={
                            "reason":    "local_dns_error",
                            "exception": type(e).__name__,
                            "message":   str(e),
                        },
                    )
                else:
                    log.exception("check %s raised", check.id)
                    result = CheckResult(
                        check_id=check.id, target=check.target, stage=check.stage,
                        status="error",
                        started_at=t0, finished_at=utcnow(),
                        summary=f"{type(e).__name__}: {e}",
                        payload={"exception": type(e).__name__, "message": str(e)},
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
            delay = max(1.0, check.cadence_s - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                # if we get here the stop event fired
                return
            except asyncio.TimeoutError:
                pass
