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

from .checks.base import Check, CheckResult, utcnow
from .checks.transports import CheckContext
from .db.store import Store
from .registry import CHECKS

log = logging.getLogger(__name__)


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
                log.exception("check %s raised", check.id)
                result = CheckResult(
                    check_id=check.id, target=check.target, stage=check.stage,
                    status="error",
                    started_at=t0, finished_at=utcnow(),
                    summary=f"{type(e).__name__}: {e}",
                    payload={"exception": type(e).__name__, "message": str(e)},
                )

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
