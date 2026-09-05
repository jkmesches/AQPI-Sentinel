"""Background capture of every published moment and tilt.

Sentinel's archive was demand-driven: a frame was stored only if someone had
looked at it. That made it dense in Reflectivity and empty everywhere else. In
the seven days to 2026-09-05 the archive gained 19,540 Reflectivity frames and
36 Velocity, 35 ZDR, 35 PhiDP, 1 RhoHV — so the moments an operator would most
want to compare against reflectivity were exactly the ones not kept, and the
first look at any of them was also the slowest.

This walks every stream on a fixed cadence and pulls what it does not already
have, which both fills the archive and leaves the LRU warm for the interface.

=== Load-bearing: this is the one component that generates unasked-for load ===

Every other upstream request Sentinel makes is either a scheduled check or an
operator looking at something. This is neither, so it is OFF by default and
paced deliberately:

  - It reuses ``_serve_source``, so a stream already in the LRU or the archive
    costs nothing upstream. Steady-state cost is one fetch per genuinely new
    frame, not one per sweep.
  - Its own semaphore bounds it independently of user traffic, and it takes
    that budget on top of, not instead of, the per-origin caps — a prewarm
    sweep cannot starve a scrub of its concurrency.
  - It skips combinations known not to exist. X-band radars publish no RhoHV
    (EXPECTED_ABSENT_MOMENTS); requesting it anyway would 404 every stream on
    every sweep forever, which is precisely the kind of pointless load the
    negative cache exists to stop and the kind of noise that trains operators
    to ignore logs.

Measured shape of a full sweep on the reference deployment: 25 moment streams
(5 X-band x 4 moments, CBAND x 5) and 60 tilt streams (5 radars x 4 elevations
x 3 moments). At the default 300 s interval that is ~49k images/day and
~860 MB/day on top of the ~280 MB/day the demand-driven archive was already
taking.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

from .config import PRODUCTS, RADAR_FOLDER, SETTINGS, moment_to_prefix
from .checks.layer2_radar import EXPECTED_ABSENT_MOMENTS

log = logging.getLogger(__name__)

# Moments Sentinel knows how to resolve to an upstream prefix.
ALL_MOMENTS = ["Reflectivity", "Differential Reflectivity", "Velocity",
               "PhiDP", "RhoHV"]


def moment_streams() -> list[tuple[str, str]]:
    """(radar, moment) pairs that actually exist upstream."""
    out: list[tuple[str, str]] = []
    for radar in RADAR_FOLDER:
        absent = EXPECTED_ABSENT_MOMENTS.get(radar, set())
        for moment in ALL_MOMENTS:
            if moment in absent:
                continue
            out.append((radar, moment))
    return out


def tilt_streams() -> list[tuple[str, int, str]]:
    """(radar, elevation, moment) tilts served by radar-display."""
    from .api.routes.upstream import _TILT_MOMENTS, _TILT_RADARS
    return [(r, el, m)
            for r in sorted(_TILT_RADARS)
            for el in (1, 2, 3, 4)
            for m in sorted(_TILT_MOMENTS)]


class Prewarmer:
    def __init__(self, app):
        self.app = app
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._sem = asyncio.Semaphore(max(1, SETTINGS.prewarm_concurrency))
        self.stats = {"sweeps": 0, "fetched": 0, "already_had": 0, "errors": 0,
                      "last_sweep_s": 0.0, "last_finished_at": None}

    async def start(self) -> None:
        if not SETTINGS.prewarm_enabled:
            log.info("prewarm disabled (SENTINEL_PREWARM_ENABLED=0)")
            return
        self._task = asyncio.create_task(self.run(), name="prewarm")
        log.info(
            "prewarm started — %d moment streams, %d tilt streams, every %ds, "
            "concurrency %d",
            len(moment_streams()), len(tilt_streams()),
            SETTINGS.prewarm_interval_s, SETTINGS.prewarm_concurrency,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def run(self) -> None:
        # Let the scheduler's own first pass go out before adding to it.
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=60)
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            t0 = asyncio.get_event_loop().time()
            try:
                await self._sweep()
            except Exception:
                log.exception("prewarm sweep failed")
            self.stats["sweeps"] += 1
            self.stats["last_sweep_s"] = round(
                asyncio.get_event_loop().time() - t0, 1)
            self.stats["last_finished_at"] = datetime.now(timezone.utc).isoformat()
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=SETTINGS.prewarm_interval_s)
                return
            except asyncio.TimeoutError:
                pass

    async def _sweep(self) -> None:
        tasks = [self._one_moment(r, m) for r, m in moment_streams()]
        tasks += [self._one_tilt(r, el, m) for r, el, m in tilt_streams()]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _one_moment(self, radar: str, moment: str) -> None:
        from .api.routes.upstream import (_serve_source, _xband_listing_cached)
        ctx = self.app.state.context
        store = getattr(self.app.state, "store", None)
        async with self._sem:
            try:
                folder = RADAR_FOLDER[radar]
                prefix = moment_to_prefix(radar, moment)
                imgs = await _xband_listing_cached(
                    ctx, folder, prefix, store.pool if store else None)
                if not imgs:
                    return
                _b, _ct, prov = await _serve_source(self.app, ctx, imgs[-1])
                self._count(prov)
            except Exception as e:
                self.stats["errors"] += 1
                log.debug("prewarm %s/%s: %s", radar, moment, e)

    async def _one_tilt(self, radar: str, el: int, moment: str) -> None:
        from .api.routes.upstream import (
            _RD_BASE, _RD_SEM, _rd_client, _serve_source, _tilt_source_key,
            _tilt_steps_raw)
        ctx = self.app.state.context
        async with self._sem:
            try:
                steps = await _tilt_steps_raw(radar, el, moment)
            except Exception as e:
                self.stats["errors"] += 1
                log.debug("prewarm tilt steps %s el_%s %s: %s", radar, el, moment, e)
                return
            # Every frame in the window, not just the newest: the window holds
            # ~16 minutes and a sweep runs every 5, so taking only frame 0
            # would still capture everything — but a skipped or slow sweep
            # would silently punch holes in the archive that nothing refills,
            # because the frames age out of the origin entirely.
            for st in steps:
                if self._stop.is_set():
                    return
                ts = st.get("ts_utc")
                if ts is None:
                    continue
                source = _tilt_source_key(radar, el, moment, ts)
                url = f"{_RD_BASE}/{radar}/images/el_{el}/{moment}_{st['frame']}.png"

                async def _fetch(u=url):
                    async with _RD_SEM:
                        return await _rd_client().get(u)

                try:
                    _b, _ct, prov = await _serve_source(
                        self.app, ctx, source, fetcher=_fetch, origin_url=url)
                    self._count(prov)
                except Exception as e:
                    self.stats["errors"] += 1
                    log.debug("prewarm tilt %s el_%s %s f%s: %s",
                              radar, el, moment, st["frame"], e)

    def _count(self, prov: str) -> None:
        if prov == "upstream":
            self.stats["fetched"] += 1
        else:
            self.stats["already_had"] += 1
