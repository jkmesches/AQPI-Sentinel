"""Thin upstream proxy — fetch radarca PNGs through Sentinel so the frontend
can use them as MapLibre raster sources without CORS issues."""
from __future__ import annotations
import asyncio as _asyncio
import datetime as _dt
import time as _time_mod
from zoneinfo import ZoneInfo as _ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Response

from ...config import PRODUCTS, RADAR_FOLDER, SETTINGS, image_path, moment_to_prefix
from ...errors import humanize_error

router = APIRouter(prefix="/api/upstream")


@router.get("/product_latest.png")
async def latest_product_image(product_id: str, request: Request):
    """Return the latest scan PNG for a product, by id. Used by MapView
    to overlay composite imagery on the map."""
    if product_id not in PRODUCTS:
        raise HTTPException(404, f"Unknown product: {product_id}")
    cfg = PRODUCTS[product_id]
    ctx = request.app.state.context
    # Step lists change at the product's own cadence (120s for the fast
    # radar composites), so re-fetching one per scrub position was pure
    # waste: this endpoint made TWO upstream calls per frame — a
    # productDetail to resolve the step plus the imageData itself — which is
    # exactly the 2.00x amplification measured on 2026-08-27.
    steps = await _product_steps_cached(ctx, product_id, cfg["details"])
    if not steps:
        raise HTTPException(404, "No scans available")
    latest = steps[-1]["imageName"]
    file_path = image_path(product_id, latest)
    try:
        body, ct, prov = await _serve_source(request.app, ctx, file_path)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Upstream image fetch failed: {humanize_error(e)}")
    return Response(
        content=body,
        media_type="image/png",
        headers={"cache-control": "no-store", "x-scan-name": latest},
    )


@router.get("/product_steps")
async def product_steps(product_id: str, request: Request):
    """Return the time-step manifest for a product so the frontend can drive
    play/step controls on the map. {n, current_idx, steps:[{i,ts,imageName}]}.

    Wraps the upstream call so transport errors surface as a clean 502 to the
    client instead of a 500 with an httpx traceback — the frontend retries
    on 5xx and a bare 500 is indistinguishable from a code bug in the logs.
    """
    if product_id not in PRODUCTS:
        raise HTTPException(404, f"unknown product: {product_id}")
    cfg = PRODUCTS[product_id]
    ctx = request.app.state.context
    raw = await _product_steps_cached(ctx, product_id, cfg["details"])
    out = [
        {"i": i, "ts": s.get("timestamp"), "imageName": s.get("imageName"),
         "day": s.get("day"), "date": s.get("date"), "time": s.get("time")}
        for i, s in enumerate(raw)
    ]
    return {"product_id": product_id, "n": len(out), "current_idx": len(out) - 1, "steps": out}


@router.get("/product_image.png")
async def product_image_by_step(product_id: str, step: int, request: Request):
    """Return the PNG for an arbitrary step (0-indexed; negative wraps from end)."""
    _PROXY_HITS["product_image"] += 1
    if product_id not in PRODUCTS:
        raise HTTPException(404, f"Unknown product: {product_id}")
    cfg = PRODUCTS[product_id]
    ctx = request.app.state.context
    try:
        pd = await ctx.http.get(
            f"{SETTINGS.base}/api/productDetail", params={"file": cfg["details"]},
        )
    except Exception as e:
        raise HTTPException(502, f"Upstream unavailable: {humanize_error(e)}")
    if pd.status_code != 200:
        raise HTTPException(502, f"Upstream returned HTTP {pd.status_code}")
    steps = pd.json().get("steps") or []
    if not steps:
        raise HTTPException(404, "No scans available")
    if step < 0:
        step = len(steps) + step
    if step < 0 or step >= len(steps):
        raise HTTPException(400, f"Step out of range (0..{len(steps)-1})")
    name = steps[step]["imageName"]
    file_path = image_path(product_id, name)

    # LRU -> local archive -> upstream. Scrubbing over frames Sentinel has
    # already captured now costs radarca nothing at all.
    try:
        body, ct, prov = await _serve_source(request.app, ctx, file_path)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Upstream image fetch failed: {humanize_error(e)}")

    return Response(
        content=body, media_type=ct or "image/png",
        # === Cacheable, but deliberately short ===
        # MapView requests this URL twice per frame — preloadImage() then
        # MapLibre's updateImage() — so a cacheable response collapses the
        # pair into one network fetch. That only works because preloadImage
        # now sets crossOrigin='anonymous'; without it the preload caches an
        # OPAQUE entry that the CORS fetch cannot reuse, which is what broke
        # every frame on 2026-08-27.
        #
        # Short TTL because the URL is keyed by STEP INDEX and the
        # step->frame mapping shifts as the rolling window advances. The
        # client also appends a per-render cache-buster, so reuse only ever
        # happens within a single render — 30s is far more headroom than the
        # preload/update gap needs, and far below the ~120s publish cycle.
        headers={"cache-control": "public, max-age=30",
                 "x-sentinel-cache": prov,
                 "x-scan-name": name,
                 "x-scan-ts": steps[step].get("timestamp") or "",
                 "x-step": str(step), "x-total": str(len(steps))},
    )


from collections import OrderedDict as _OrderedDict

from ...archive import lookup_by_source as _archive_lookup
from ...archive import save_image as _archive_save

# LRU cache (process-local). Bytes are kept in-memory so re-rendering the
# same timeline cell doesn't hit the disk or radarca again. Bounded so
# memory stays predictable; eviction is FIFO-by-recency.
_IMAGE_CACHE: "_OrderedDict[str, tuple[bytes, str]]" = _OrderedDict()
_IMAGE_CACHE_MAX = 256

# === Load-bearing: single-flight, negative cache, and a concurrency cap ===
#
# Measured 2026-08-27: one map scrub produced 347 upstream imageData calls for
# only 182 distinct URLs. Individual frames were fetched 7-11 times, and the
# water_depth image that 404s upstream was re-requested 11 times in one pass.
#
# Cause: there is an `await` between the LRU check and the LRU write, so N
# concurrent requests for the same frame all miss the cache and all hit
# upstream (a thundering herd on our own proxy). A non-200 cached nothing at
# all, so a known-missing image was re-fetched on every single request.
#
# This matters more than ordinary inefficiency: radarca is slow and fragile —
# measured p50 3.4s / p90 8.3s while completely idle — and it is someone
# else's production research system. Our dashboard should not be capable of
# multiplying one operator's scrub into a burst against it.
#
# _INFLIGHT      collapses concurrent identical fetches into one.
# _NEG_CACHE     remembers a non-200 briefly so we stop hammering a 404.
# _UPSTREAM_SEM  caps how many user-driven image fetches can be in flight at
#                once, independent of the scheduled checks' own traffic.
_INFLIGHT: dict[str, "_asyncio.Task[tuple[bytes, str]]"] = {}
_NEG_CACHE: dict[str, float] = {}
_NEG_TTL_S = 120.0
_UPSTREAM_SEM = _asyncio.Semaphore(6)

# Short-TTL memo of productDetail step lists. The fast composites publish
# every 120s, so a few seconds of staleness is invisible to a scrubbing
# operator while collapsing a whole scrub's worth of lookups into one call.
# Inbound image-proxy request counter, surfaced in /api/_debug/stats. The
# browser-side request count can't distinguish a network fetch from an HTTP
# cache hit, so this is the only honest way to see what the map actually
# costs the backend.
_PROXY_HITS: dict[str, int] = {"product_image": 0, "xband_scan": 0,
                               "image_by_source": 0, "tilt_image": 0}

_PD_TTL_S = 90.0
_PD_CACHE: dict[str, tuple[float, list]] = {}
_PD_REFRESHING: set[str] = set()


# === Load-bearing: stale-while-revalidate, not plain TTL ===
#
# These listings say WHICH frames exist; the images themselves come from our
# LRU/archive in milliseconds. With a plain 20s TTL, selecting a radar more
# than 20s after the last request blocked on radarca — measured 6.2s for one
# selection whose image was already in the LRU. radarca's p50 is 3.4s and p90
# 8.3s, so a user interaction must never wait on it.
#
# Fresh window: serve immediately. Stale but present: serve the stale list AND
# refresh in the background — radars publish every ~120s, so a listing a
# minute old still names the frames that exist. Only a completely cold entry
# blocks, and the scheduled L2 checks touch every radar every 120s, so that is
# rare after boot.
_XB_TTL_S = 90.0
_XB_CACHE: dict[tuple[str, str], tuple[float, list]] = {}
_XB_REFRESHING: set[tuple[str, str]] = set()


async def _xband_fetch_listing(ctx, folder: str, prefix: str) -> list:
    r = await ctx.http.get(
        f"{SETTINGS.base}/api/xbandRadarImages/",
        params={"radarFolder": folder, "productPrefix": prefix},
    )
    if r.status_code != 200:
        raise HTTPException(502, f"upstream xbandRadarImages HTTP {r.status_code}")
    return r.json().get("images") or []


async def _xband_refresh_bg(ctx, folder: str, prefix: str) -> None:
    key = (folder, prefix)
    try:
        imgs = await _xband_fetch_listing(ctx, folder, prefix)
        _XB_CACHE[key] = (_time_mod.monotonic() + _XB_TTL_S, imgs)
    except Exception:
        pass          # keep serving the stale list; the next request retries
    finally:
        _XB_REFRESHING.discard(key)


async def _xband_listing_local(pool, folder: str, prefix: str) -> list:
    """Reconstruct a radar's recent frame list from our own archive.

    We already hold 95-100% of every radar's rolling window — measured
    2026-08-28: XSCR 23/23, XSCV 26/27, XSCW 25/26, XSWR 25/26, CBAND 13/17 —
    because the L4 check archives each radar's newest frame every 120s. So
    the listing is answerable locally, and answering it locally means the map
    keeps working even when radarca does not.

    What we are always missing is the NEWEST frame or two: L4 archives one
    cycle behind, so the live edge lags by up to a check interval. That is
    why this is the floor rather than the whole story — a background refresh
    still pulls upstream to discover frames newer than ours.
    """
    if pool is None:
        return []
    rows = await pool.fetch(
        """
        SELECT source FROM image_index
        WHERE source LIKE $1 AND source LIKE $2
        ORDER BY source DESC
        LIMIT 40
        """,
        # `_` is LIKE's single-char wildcard, which conveniently matches the
        # literal underscore in `<folder>_<prefix>_<timestamp>.png` — so the
        # pattern needs no ESCAPE clause.
        f"xband_chivo/{folder}/%", f"%_{prefix}_%",
    )
    # filenames embed the timestamp, so lexical order IS chronological
    return [r["source"] for r in reversed(rows)]


async def _xband_listing_cached(ctx, folder: str, prefix: str, pool=None) -> list:
    key = (folder, prefix)
    now = _time_mod.monotonic()
    hit = _XB_CACHE.get(key)
    if hit is not None:
        if hit[0] <= now and key not in _XB_REFRESHING:
            _XB_REFRESHING.add(key)
            _asyncio.create_task(_xband_refresh_bg(ctx, folder, prefix))
        return hit[1]                       # fresh or stale, answer now

    # Cold. Prefer our own archive over blocking on a 3-8s upstream call —
    # this is the last path that could still make a click wait on radarca.
    local = await _xband_listing_local(pool, folder, prefix)
    if local:
        _XB_CACHE[key] = (now, local)       # already stale: refresh right away
        if key not in _XB_REFRESHING:
            _XB_REFRESHING.add(key)
            _asyncio.create_task(_xband_refresh_bg(ctx, folder, prefix))
        return local

    imgs = await _xband_fetch_listing(ctx, folder, prefix)
    _XB_CACHE[key] = (now + _XB_TTL_S, imgs)
    return imgs


async def _pd_fetch(ctx, details_path: str) -> list:
    try:
        pd = await ctx.http.get(
            f"{SETTINGS.base}/api/productDetail", params={"file": details_path},
        )
    except Exception as e:
        raise HTTPException(502, f"Upstream unavailable: {humanize_error(e)}")
    if pd.status_code != 200:
        raise HTTPException(502, f"Upstream returned HTTP {pd.status_code}")
    return pd.json().get("steps") or []


async def _pd_refresh_bg(ctx, product_id: str, details_path: str) -> None:
    try:
        _PD_CACHE[product_id] = (_time_mod.monotonic() + _PD_TTL_S,
                                 await _pd_fetch(ctx, details_path))
    except Exception:
        pass
    finally:
        _PD_REFRESHING.discard(product_id)


async def _product_steps_cached(ctx, product_id: str, details_path: str) -> list:
    """Stale-while-revalidate, for the same reason as _xband_listing_cached:
    switching composite must not block on a 3-8s upstream call."""
    now = _time_mod.monotonic()
    hit = _PD_CACHE.get(product_id)
    if hit is not None:
        if hit[0] <= now and product_id not in _PD_REFRESHING:
            _PD_REFRESHING.add(product_id)
            _asyncio.create_task(_pd_refresh_bg(ctx, product_id, details_path))
        return hit[1]
    steps = await _pd_fetch(ctx, details_path)
    _PD_CACHE[product_id] = (now + _PD_TTL_S, steps)
    return steps


@router.get("/image_by_source.png")
async def image_by_source(source: str, request: Request):
    """Serve the captured image for an L4 source path.

    Lookup order:
      1. In-memory LRU cache (this process).
      2. Local archive (data/archive + image_index → sha256).
      3. Upstream radarca (last-resort, populates archive on success).

    Returns 502 only if the upstream rotated the file out AND we don't
    have it archived locally."""
    _PROXY_HITS["image_by_source"] += 1
    body, ct, prov = await _serve_source(request.app, request.app.state.context, source)
    return Response(
        content=body, media_type=ct,
        headers={"cache-control": "public, max-age=300", "x-sentinel-cache": prov},
    )


async def _serve_source(app, ctx, source: str, fetcher=None,
                        origin_url: str | None = None,
                        archive_only: bool = False) -> tuple[bytes, str, str]:
    """Resolve one upstream image path to bytes, cheapest source first.

    Order: in-process LRU -> local archive -> upstream.

    The archive step is the important one and was missing from the map's two
    hot paths (product_image.png, xband_scan.png) — they went straight
    upstream every time despite Sentinel already holding 63k composite frames
    and 152k radar frames locally, back to 2026-05-16. Anything we have ever
    captured is served from our own copy; upstream is only touched for frames
    we have genuinely never seen.

    Returns (body, content_type, provenance) where provenance is one of
    lru | disk | upstream, surfaced as the x-sentinel-cache header so it is
    obvious in devtools which frames actually cost radarca anything.
    """
    cached = _IMAGE_CACHE.get(source)
    if cached is not None:
        body, ct = cached
        _IMAGE_CACHE.move_to_end(source)
        return body, ct, "lru"

    store = getattr(app.state, "store", None)
    pool = store.pool if store else None
    if pool is not None and SETTINGS.archive_enabled:
        hit = await _archive_lookup(pool, SETTINGS.archive_root, source)
        if hit is not None:
            body, ct = hit
            _IMAGE_CACHE[source] = (body, ct)
            while len(_IMAGE_CACHE) > _IMAGE_CACHE_MAX:
                _IMAGE_CACHE.popitem(last=False)
            return body, ct, "disk"

    # A frame that has aged out of the origin's rolling window exists only in
    # our archive; there is no URL left to fetch it from. Say so rather than
    # asking upstream for something it cannot have.
    if archive_only:
        raise HTTPException(404, f"{source} is not in the archive")

    exp = _NEG_CACHE.get(source)
    if exp is not None:
        if exp > _time_mod.monotonic():
            raise HTTPException(
                502, f"upstream image unavailable for {source} (negative-cached)"
            )
        _NEG_CACHE.pop(source, None)

    task = _INFLIGHT.get(source)
    if task is None:
        task = _asyncio.create_task(
            _fetch_image_upstream(ctx, pool, source, fetcher, origin_url))
        _INFLIGHT[source] = task
        task.add_done_callback(lambda t, k=source: _INFLIGHT.pop(k, None))
    body, ct = await _asyncio.shield(task)
    return body, ct, "upstream"


async def _fetch_image_upstream(ctx, pool, source: str, fetcher=None,
                                origin_url: str | None = None) -> tuple[bytes, str]:
    """Fetch one image, populate caches, archive it.

    ``fetcher`` lets a second origin reuse every protection built around this
    path — the LRU, the single-flight, the negative cache and the archive are
    all keyed on the LOGICAL source string, so they do not care where the
    bytes came from. Default (None) is radarca's imageData, under
    _UPSTREAM_SEM so a scrub can't open an unbounded number of concurrent
    connections against a slow origin. Callers supplying a fetcher are
    responsible for their own origin's concurrency cap, because the right
    limit differs per upstream.
    """
    if fetcher is None:
        async with _UPSTREAM_SEM:
            ir = await ctx.http.get(f"{SETTINGS.base}/api/imageData", params={"file": source})
        origin_url = f"{SETTINGS.base}/api/imageData?file={source}"
    else:
        ir = await fetcher()
    if ir.status_code != 200 or not ir.headers.get("content-type", "").startswith("image/"):
        _NEG_CACHE[source] = _time_mod.monotonic() + _NEG_TTL_S
        raise HTTPException(502, f"upstream image HTTP {ir.status_code} for {source}")
    body = ir.content
    ct = ir.headers.get("content-type", "image/png")
    _IMAGE_CACHE[source] = (body, ct)
    while len(_IMAGE_CACHE) > _IMAGE_CACHE_MAX:
        _IMAGE_CACHE.popitem(last=False)
    # Opportunistically archive — same logic as a live L4 capture, so a
    # source we proxy-fetched for the first time gets a long-term home.
    if pool is not None and SETTINGS.archive_enabled:
        _asyncio.create_task(
            _archive_save(
                pool, SETTINGS.archive_root,
                source=source, content=body, content_type=ct,
                origin_url=origin_url,
            )
        )
    return body, ct


_XBAND_TS_RE = __import__("re").compile(r"_(\d{8})-(\d{4})\.png$")

# in-memory cache: scan_path → non-empty pixel fraction (0..1)
_activity_cache: dict[str, float] = {}


async def _activity_for(app, ctx, scan_path: str) -> float:
    """Non-empty pixel fraction for one frame.

    Goes through _serve_source, so the sparkline is computed from our own
    archived copy wherever we have one. This endpoint is the single largest
    consumer of upstream bandwidth in the whole app — selecting a composite
    asks it for EVERY step in the timeline (31 frames), and before this it
    fetched all of them straight from radarca and decoded each with PIL.
    """
    cached = _activity_cache.get(scan_path)
    if cached is not None:
        return cached
    try:
        body, _ct, _prov = await _serve_source(app, ctx, scan_path)
    except Exception:
        _activity_cache[scan_path] = 0.0
        return 0.0
    try:
        import io as _io
        import numpy as _np
        from PIL import Image as _Img
        arr = _np.asarray(_Img.open(_io.BytesIO(body)).convert("RGBA"))
        ratio = float((arr[:, :, 3] > 10).sum()) / arr[:, :, 3].size
    except Exception:
        ratio = 0.0
    _activity_cache[scan_path] = ratio
    return ratio


@router.get("/activity")
async def step_activity(
    request: Request,
    product_id: str | None = None,
    radar: str | None = None,
    moment: str = "Reflectivity",
    prefix: str | None = None,
):
    """Non-empty pixel fraction per step in the current timeline. Used by the
    map's time strip to draw a tiny "is there weather" sparkline so an operator
    can tell at a glance whether a flat playback is "no activity" vs "broken
    pipeline"."""
    ctx = request.app.state.context
    out = []

    if product_id:
        if product_id not in PRODUCTS:
            raise HTTPException(404, f"unknown product: {product_id}")
        cfg = PRODUCTS[product_id]
        for i, s in enumerate(await _product_steps_cached(ctx, product_id, cfg["details"])):
            path = image_path(product_id, s["imageName"])
            out.append({"i": i, "ts": s.get("timestamp"),
                        "activity": await _activity_for(request.app, ctx, path)})
    elif radar:
        if radar not in RADAR_FOLDER:
            raise HTTPException(404, f"unknown radar: {radar}")
        folder = RADAR_FOLDER[radar]
        if prefix is None:
            try:
                prefix = moment_to_prefix(radar, moment)
            except KeyError:
                raise HTTPException(400, f"unknown moment: {moment}")
        _store = getattr(request.app.state, "store", None)
        for i, fname in enumerate(await _xband_listing_cached(
                ctx, folder, prefix, _store.pool if _store else None)):
            ts = _parse_xband_ts(fname)
            out.append({"i": i,
                        "ts": ts.isoformat() if ts else None,
                        "activity": await _activity_for(request.app, ctx, fname)})
    else:
        raise HTTPException(400, "need product_id or radar")

    return {"n": len(out), "steps": out}


@router.get("/radar_steps")
async def radar_steps(radar: str, request: Request,
                       moment: str = "Reflectivity",
                       prefix: str | None = None):
    """Manifest of recent scans for one radar/moment, parsed from the file
    names returned by xbandRadarImages. Used by the frontend when no composite
    is selected — gives the scrubber a real timeline that matches what's
    actually available upstream."""
    if radar not in RADAR_FOLDER:
        raise HTTPException(404, f"unknown radar: {radar}")
    folder = RADAR_FOLDER[radar]
    if prefix is None:
        try:
            prefix = moment_to_prefix(radar, moment)
        except KeyError:
            raise HTTPException(400, f"unknown moment: {moment}")
    ctx = request.app.state.context
    r = await ctx.http.get(
        f"{SETTINGS.base}/api/xbandRadarImages/",
        params={"radarFolder": folder, "productPrefix": prefix},
    )
    if r.status_code != 200:
        raise HTTPException(502, "xbandRadarImages unreachable")
    imgs = r.json().get("images") or []
    steps = []
    for i, fname in enumerate(imgs):
        ts = _parse_xband_ts(fname)
        if ts is None:
            continue
        steps.append({
            "i":         len(steps),
            "ts":        ts.isoformat(),
            "imageName": fname,
            "day":       ts.strftime("%a"),
            "date":      ts.strftime("%d-%b-%Y"),
            "time":      ts.strftime("%H:%M:%S"),
        })
    return {"radar": radar, "moment": moment, "prefix": prefix,
            "n": len(steps), "current_idx": len(steps) - 1,
            "steps": steps}


def _parse_xband_ts(filename: str):
    """Pull the datetime from a name like 'scwa_CorrReflectivity_20260515-2336.png'."""
    m = _XBAND_TS_RE.search(filename)
    if not m:
        return None
    from datetime import datetime, timezone
    try:
        d = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
        return d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@router.get("/xband_scan.png")
async def latest_xband_scan(
    radar: str,
    request: Request,
    moment: str = "Reflectivity",
    prefix: str | None = None,
    time: str | None = None,
):
    """Per-radar scan PNG. With ``time=<ISO>``, returns the scan with the
    closest timestamp (within the rolling-1h window). Without, returns latest.

    Used by the map's per-radar overlay so it can scrub in sync with the
    composite scrubber.
    """
    _PROXY_HITS["xband_scan"] += 1
    if radar not in RADAR_FOLDER:
        raise HTTPException(404, f"unknown radar: {radar}")
    folder = RADAR_FOLDER[radar]
    # `prefix=` overrides moment mapping if explicitly set (for power users
    # or future automation that knows the upstream productPrefix string).
    if prefix is None:
        try:
            prefix = moment_to_prefix(radar, moment)
        except KeyError:
            raise HTTPException(400, f"unknown moment: {moment}")
    ctx = request.app.state.context
    # Listing memo: scrubbing a radar overlay re-requested this per frame,
    # doubling the upstream cost of every scrub position.
    _store = getattr(request.app.state, "store", None)
    imgs = await _xband_listing_cached(ctx, folder, prefix,
                                       _store.pool if _store else None)
    if not imgs:
        raise HTTPException(404, "no scans in window")

    chosen = imgs[-1]
    chosen_ts = None
    if time:
        from datetime import datetime, timezone
        try:
            target = datetime.fromisoformat(time.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, f"bad time: {time}")
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        best = None
        best_delta = None
        for fname in imgs:
            ts = _parse_xband_ts(fname)
            if ts is None:
                continue
            delta = abs((ts - target).total_seconds())
            if best is None or delta < best_delta:
                best, best_delta, chosen_ts = fname, delta, ts
        if best is not None:
            chosen = best

    body, ct, prov = await _serve_source(request.app, ctx, chosen)
    return Response(
        content=body,
        media_type=ct or "image/png",
        headers={
            # Cacheable for the same reason as product_image.png, and safely
            # longer: this URL carries radar+moment+time, so it identifies
            # its content rather than a shifting index. pokeOverlays bumps
            # _forceBuster to force a refresh of the live frame.
            "cache-control": "public, max-age=60",
            "x-sentinel-cache": prov,
            "x-scan-name": chosen,
            "x-scan-ts": chosen_ts.isoformat() if chosen_ts else "",
            "x-prefix": prefix,
            "x-moment": moment,
        },
    )


# ----------------------------------------------------------------------
# Stream gauges (USGS sites from radarca's stream_data.csv).
#
# The frontend's "Stream gauges" map layer needs the full site list to
# render the markers and the per-COMID time-series endpoints to populate
# the click popup. Both are proxied here so the browser stays same-origin
# (avoids CORS) and so we can cache the (effectively static) site list
# server-side instead of asking 100 browsers to fetch the 16.7 KB CSV
# every time someone toggles the layer on.
# ----------------------------------------------------------------------

import csv as _csv
import io as _io2
import time as _time

# Module-level cache for the parsed site list. radarca's CSV ships a
# `Last-Modified: Wed, 07 Jan 2026` per the public characterization —
# the list is effectively static, but we re-fetch hourly so any
# additions don't sit stale for a day.
_GAUGES_CACHE: dict[str, object] = {"data": None, "fetched_at": 0.0}
_GAUGES_TTL_S = 3600


@router.get("/stream_gauges")
async def stream_gauges(request: Request):
    """Return the parsed USGS site list from /data/stream_data.csv.

    Shape:
        {count: int,
         sites: [{comid: str, lat: float, lon: float, status: 'B'|'R'}]}

    `status` is 'B' (basic — site exists upstream) or 'R' (real-time —
    has live data). Cached in-process for an hour.
    """
    now = _time.time()
    if (
        _GAUGES_CACHE["data"] is not None
        and now - _GAUGES_CACHE["fetched_at"] < _GAUGES_TTL_S
    ):
        return _GAUGES_CACHE["data"]
    ctx = request.app.state.context
    try:
        r = await ctx.http.get(f"{SETTINGS.base}/data/stream_data.csv")
    except Exception as e:
        raise HTTPException(502, f"Upstream unavailable: {humanize_error(e)}")
    if r.status_code != 200:
        raise HTTPException(502, f"Upstream returned HTTP {r.status_code}")
    sites = []
    reader = _csv.DictReader(_io2.StringIO(r.text))
    for row in reader:
        try:
            sites.append({
                "comid": row["COMID"],
                "lat": float(row["LatSite"]),
                "lon": float(row["LonSite"]),
                "status": row["Status"],
            })
        except (KeyError, ValueError):
            continue
    payload = {"count": len(sites), "sites": sites}
    _GAUGES_CACHE["data"] = payload
    _GAUGES_CACHE["fetched_at"] = now
    return payload


@router.get("/stream_data")
async def stream_data(comid: str, kind: str, ts: str, request: Request):
    """Proxy radarca's per-COMID stream-data endpoints.

    Parameters:
        comid: USGS site COMID (string of digits).
        kind:  'forecast' or 'observed'.
        ts:    For forecast → 'YYYYMMDD_HH' (the validator accepts this
               despite the error message claiming HHMM).
               For observed → 'YYYYMMDD'.

    Returns the upstream JSON as-is — typically
        {headers: [...], values: [...]}
    Empty headers/values is a valid response (no data for that hour).
    """
    if kind not in ("forecast", "observed"):
        raise HTTPException(400, "kind must be 'forecast' or 'observed'")
    path = (
        f"/api/get_stream_data/{comid}/{ts}"
        if kind == "forecast"
        else f"/api/get_observed_stream_data/{comid}/{ts}"
    )
    ctx = request.app.state.context
    try:
        r = await ctx.http.get(f"{SETTINGS.base}{path}")
    except Exception as e:
        raise HTTPException(502, f"Upstream unavailable: {humanize_error(e)}")
    if r.status_code != 200:
        raise HTTPException(502, f"Upstream returned HTTP {r.status_code}")
    try:
        return r.json()
    except Exception:
        raise HTTPException(502, "Upstream returned malformed JSON")


# ----------------------------------------------------------------------
# Per-elevation tilt imagery (CSU Web Radar Display).
#
# radarca's /api/xbandRadarImages serves a single pre-rendered tilt per
# moment. The separate radar-display app exposes the full per-elevation
# PPI stack: for each X-band radar, 4 elevation folders × 3 moments × 7
# frames (frame 0 = newest, ~2-min cadence). Each frame is a 665×665
# georeferenced PPI PNG plus a radar_plot_<n>.json carrying the center
# lat/lon, MaxRange, and scan angle.
#
# We proxy it (a) to dodge CORS and (b) because radar-display's TLS cert
# is currently EXPIRED — a dedicated verify=False client handles that
# host ONLY, leaving full TLS verification on every other upstream call.
# ----------------------------------------------------------------------

import httpx as _httpx

_RD_BASE = "https://radardisplay.engr.colostate.edu"
# Lazily-built client that skips cert verification — scoped to the
# radar-display host (expired cert as of 2026-05). Do NOT route other
# upstream traffic through this.
_rd_client_inst: "_httpx.AsyncClient | None" = None


def _rd_client() -> "_httpx.AsyncClient":
    global _rd_client_inst
    if _rd_client_inst is None:
        _rd_client_inst = _httpx.AsyncClient(verify=False, timeout=12.0)
    return _rd_client_inst


# X-band radars present in the radar-display directory. CBAND + NEXRAD
# are not served there.
_TILT_RADARS = {"XEBY", "XSCR", "XSCV", "XSCW", "XSWR"}
_TILT_MOMENTS = {"reflectivity", "velocity", "copolarcorrelation"}
_TILT_FRAMES = 7  # _0.._6

# radar-display gets its own concurrency cap. It is a different origin from
# radarca with different characteristics — small files, fast responses
# (~40 ms observed) — so it tolerates more parallelism, but "more" is not
# "unbounded": tilt_steps alone fans out to 7 JSON fetches per call, and the
# prewarmer walks 60 streams.
_RD_SEM = _asyncio.Semaphore(6)

# === Load-bearing: radar-display stamps frames in MOUNTAIN time ===
#
# The JSON `Time.Value` is a naive string like "Sat, 05 Sep 2026 13:54:35"
# with no offset and no zone name. It is NOT UTC and it is not the radars'
# own local time either — the radars are in California, but radar-display is
# hosted at CSU, so the stamps are America/Denver. Verified 2026-09-05: frame
# 0 read 13:56:55 while UTC was 19:58:02 and Denver was 13:58:02.
#
# This matters because the archive keys tilt frames by their capture time. A
# naive parse would file every frame 6-7 hours out, and the error would change
# twice a year with DST, so the archive would be quietly wrong in a way that
# looks like a clock problem rather than a parsing one.
_RD_TZ = _ZoneInfo("America/Denver")
_RD_TS_FMT = "%a, %d %b %Y %H:%M:%S"
_RD_SOURCE_PREFIX = "radar_display/"


def _rd_parse_ts(raw: str | None):
    """radar-display's naive Mountain-time stamp -> aware UTC datetime."""
    if not raw:
        return None
    try:
        naive = _dt.datetime.strptime(raw.strip(), _RD_TS_FMT)
    except (ValueError, TypeError):
        return None
    return naive.replace(tzinfo=_RD_TZ).astimezone(_dt.timezone.utc)


def _tilt_source_key(radar: str, el: int, moment: str, ts_utc) -> str:
    """Logical archive key for one tilt frame.

    Keyed by CAPTURE TIME, never by frame index. The frame index is a
    position in a rolling 7-deep window that shifts every ~140 s, so
    `..._0.png` names a different image minute to minute — keying on it would
    make the archive collide with itself and make history unqueryable.
    """
    return (f"{_RD_SOURCE_PREFIX}{radar}/el_{el}/{moment}/"
            f"{ts_utc.strftime('%Y%m%dT%H%M%SZ')}.png")


# Short-TTL memo of the per-(radar, el, moment) frame list. One tilt_steps
# call is 7 JSON fetches; without this, every image request would repeat them.
# Frames advance every ~140 s, so 45 s of staleness costs at most a slightly
# late newest-frame while collapsing a scrub into one metadata pass.
_TILT_TTL_S = 45.0
_TILT_CACHE: dict[str, tuple[float, list]] = {}


async def _tilt_steps_raw(radar: str, el: int, moment: str) -> list[dict]:
    """Frame list for one tilt: [{frame, ts, ts_utc}], newest first."""
    key = f"{radar}/{el}/{moment}"
    hit = _TILT_CACHE.get(key)
    now = _time_mod.monotonic()
    if hit is not None and hit[0] > now:
        return hit[1]

    async def _fetch(frame: int):
        url = f"{_RD_BASE}/{radar}/json/el_{el}/radar_plot_{frame}.json"
        try:
            async with _RD_SEM:
                r = await _rd_client().get(url)
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:
            return None

    results = await _asyncio.gather(*[_fetch(f) for f in range(_TILT_FRAMES)])
    steps = []
    for f, j in enumerate(results):
        if j is None:
            continue
        raw = (j.get("Time", {}) or {}).get("Value")
        steps.append({"frame": f, "ts": raw, "ts_utc": _rd_parse_ts(raw), "_raw": j})
    _TILT_CACHE[key] = (now + _TILT_TTL_S, steps)
    return steps


def _tilt_guard(radar: str, el: int, moment: str) -> None:
    if radar not in _TILT_RADARS:
        raise HTTPException(404, f"no tilt imagery for radar {radar}")
    if moment not in _TILT_MOMENTS:
        raise HTTPException(400, f"unknown moment {moment}")
    if el < 1 or el > 4:
        raise HTTPException(400, "el must be 1..4")


@router.get("/tilt_steps")
async def tilt_steps(radar: str, el: int, moment: str):
    """Metadata + per-frame timestamps for a (radar, elevation) tilt.

    Frame 0 is newest. Each step carries both the origin's raw stamp and the
    UTC instant it denotes, so the frontend can label and scrub the loop
    without needing to know that radar-display reports Mountain time.
    """
    _tilt_guard(radar, el, moment)
    steps = await _tilt_steps_raw(radar, el, moment)
    if not steps:
        raise HTTPException(502, "radar-display returned no tilt frames")
    newest = steps[0]["_raw"]
    return {
        "radar": radar,
        "el": el,
        "moment": moment,
        "center": [newest["Longitude"]["Value"], newest["Latitude"]["Value"]],
        "range_km": newest["MaxRange"]["Value"],
        "angle": newest["Scan"]["Angle"]["Value"],
        "steps": [
            {"frame": st["frame"], "ts": st["ts"],
             "ts_utc": st["ts_utc"].isoformat() if st["ts_utc"] else None}
            for st in steps
        ],
    }


@router.get("/tilt_image.png")
async def tilt_image(radar: str, el: int, moment: str, request: Request,
                     frame: int = 0, time: str | None = None):
    """One per-elevation PPI PNG, served cheapest-source-first.

    Until v0.2.3 this went straight to radar-display on every request with
    `cache-control: no-store` and none of the protections the radarca image
    paths have had since 2026-08-27 — no LRU, no single-flight, no negative
    cache, no concurrency cap, no archive. Scrubbing a tilt loop is exactly
    the workload those were built for, against an origin that had none of them.

    With `time=<ISO>`, returns the frame nearest that instant. Because every
    frame we serve is archived, this reaches back past radar-display's own
    7-frame (~16 min) window: the archive is deeper than the origin. A frame
    outside the live window is served from the archive or 404s — never
    re-requested upstream, because no URL still names it.
    """
    _tilt_guard(radar, el, moment)
    _PROXY_HITS["tilt_image"] += 1
    ctx = request.app.state.context

    if time is not None:
        try:
            target = _dt.datetime.fromisoformat(time.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, f"bad time: {time}")
        if target.tzinfo is None:
            target = target.replace(tzinfo=_dt.timezone.utc)
        steps = await _tilt_steps_raw(radar, el, moment)
        live = [st for st in steps if st["ts_utc"] is not None]
        best = min(live, key=lambda st: abs((st["ts_utc"] - target).total_seconds()),
                   default=None)
        # Inside the live window we can still fetch it; outside, the archive is
        # the only possible source, so snap the key to the requested instant.
        if best is not None and abs((best["ts_utc"] - target).total_seconds()) <= 90:
            ts_utc, frame, archive_only = best["ts_utc"], best["frame"], False
        else:
            ts_utc, archive_only = target, True
    else:
        if frame < 0 or frame >= _TILT_FRAMES:
            raise HTTPException(400, f"frame out of range (0..{_TILT_FRAMES - 1})")
        steps = await _tilt_steps_raw(radar, el, moment)
        match = next((st for st in steps if st["frame"] == frame), None)
        if match is None or match["ts_utc"] is None:
            raise HTTPException(
                502, f"no timestamp for {radar} el_{el} {moment} frame {frame}")
        ts_utc, archive_only = match["ts_utc"], False

    source = _tilt_source_key(radar, el, moment, ts_utc)
    url = f"{_RD_BASE}/{radar}/images/el_{el}/{moment}_{frame}.png"

    async def _fetch():
        async with _RD_SEM:
            try:
                return await _rd_client().get(url)
            except Exception as e:
                raise HTTPException(
                    502, f"radar-display unavailable: {humanize_error(e)}")

    body, ct, prov = await _serve_source(
        request.app, ctx, source, fetcher=_fetch, origin_url=url,
        archive_only=archive_only,
    )
    return Response(
        content=body,
        media_type=ct,
        headers={
            # Frame bytes are immutable once captured — the key is the capture
            # instant, not a rolling index — so the browser may keep them.
            "cache-control": "public, max-age=300",
            "x-tilt-angle-el": str(el),
            "x-tilt-ts": ts_utc.isoformat(),
            "x-sentinel-cache": prov,
        },
    )
