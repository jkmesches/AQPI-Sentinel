"""Thin upstream proxy — fetch radarca PNGs through Sentinel so the frontend
can use them as MapLibre raster sources without CORS issues."""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Request, Response

from ...config import PRODUCTS, RADAR_FOLDER, SETTINGS, image_path, moment_to_prefix

router = APIRouter(prefix="/api/upstream")


@router.get("/product_latest.png")
async def latest_product_image(product_id: str, request: Request):
    """Return the latest scan PNG for a product, by id. Used by MapView
    to overlay composite imagery on the map."""
    if product_id not in PRODUCTS:
        raise HTTPException(404, f"unknown product: {product_id}")
    cfg = PRODUCTS[product_id]
    ctx = request.app.state.context
    pd = await ctx.http.get(
        f"{SETTINGS.base}/api/productDetail", params={"file": cfg["details"]},
    )
    if pd.status_code != 200:
        raise HTTPException(502, "productDetail unreachable")
    steps = pd.json().get("steps") or []
    if not steps:
        raise HTTPException(404, "no scans")
    latest = steps[-1]["imageName"]
    file_path = image_path(product_id, latest)
    ir = await ctx.http.get(f"{SETTINGS.base}/api/imageData", params={"file": file_path})
    if ir.status_code != 200:
        raise HTTPException(502, f"upstream imageData HTTP {ir.status_code}")
    return Response(
        content=ir.content,
        media_type="image/png",
        headers={"cache-control": "no-store", "x-scan-name": latest},
    )


@router.get("/product_steps")
async def product_steps(product_id: str, request: Request):
    """Return the time-step manifest for a product so the frontend can drive
    play/step controls on the map. {n, current_idx, steps:[{i,ts,imageName}]}."""
    if product_id not in PRODUCTS:
        raise HTTPException(404, f"unknown product: {product_id}")
    cfg = PRODUCTS[product_id]
    ctx = request.app.state.context
    pd = await ctx.http.get(
        f"{SETTINGS.base}/api/productDetail", params={"file": cfg["details"]},
    )
    if pd.status_code != 200:
        raise HTTPException(502, "productDetail unreachable")
    raw = pd.json().get("steps") or []
    out = [
        {"i": i, "ts": s.get("timestamp"), "imageName": s.get("imageName"),
         "day": s.get("day"), "date": s.get("date"), "time": s.get("time")}
        for i, s in enumerate(raw)
    ]
    return {"product_id": product_id, "n": len(out), "current_idx": len(out) - 1, "steps": out}


@router.get("/product_image.png")
async def product_image_by_step(product_id: str, step: int, request: Request):
    """Return the PNG for an arbitrary step (0-indexed; negative wraps from end)."""
    if product_id not in PRODUCTS:
        raise HTTPException(404, f"unknown product: {product_id}")
    cfg = PRODUCTS[product_id]
    ctx = request.app.state.context
    pd = await ctx.http.get(
        f"{SETTINGS.base}/api/productDetail", params={"file": cfg["details"]},
    )
    if pd.status_code != 200:
        raise HTTPException(502, "productDetail unreachable")
    steps = pd.json().get("steps") or []
    if not steps:
        raise HTTPException(404, "no scans")
    if step < 0:
        step = len(steps) + step
    if step < 0 or step >= len(steps):
        raise HTTPException(400, f"step out of range (0..{len(steps)-1})")
    name = steps[step]["imageName"]
    file_path = image_path(product_id, name)
    ir = await ctx.http.get(f"{SETTINGS.base}/api/imageData", params={"file": file_path})
    if ir.status_code != 200:
        raise HTTPException(502, f"upstream imageData HTTP {ir.status_code}")
    return Response(
        content=ir.content, media_type="image/png",
        headers={"cache-control": "no-store",
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


@router.get("/image_by_source.png")
async def image_by_source(source: str, request: Request):
    """Serve the captured image for an L4 source path.

    Lookup order:
      1. In-memory LRU cache (this process).
      2. Local archive (data/archive + image_index → sha256).
      3. Upstream radarca (last-resort, populates archive on success).

    Returns 502 only if the upstream rotated the file out AND we don't
    have it archived locally."""
    cached = _IMAGE_CACHE.get(source)
    if cached is not None:
        body, ct = cached
        _IMAGE_CACHE.move_to_end(source)
        return Response(
            content=body, media_type=ct,
            headers={"cache-control": "public, max-age=300", "x-sentinel-cache": "lru"},
        )

    # Local archive — survives upstream rotation.
    app = request.app
    store = getattr(app.state, "store", None)
    pool = store.pool if store else None
    if pool is not None and SETTINGS.archive_enabled:
        hit = await _archive_lookup(pool, SETTINGS.archive_root, source)
        if hit is not None:
            body, ct = hit
            _IMAGE_CACHE[source] = (body, ct)
            while len(_IMAGE_CACHE) > _IMAGE_CACHE_MAX:
                _IMAGE_CACHE.popitem(last=False)
            return Response(
                content=body, media_type=ct,
                headers={"cache-control": "public, max-age=300", "x-sentinel-cache": "disk"},
            )

    # Upstream fallback.
    ctx = app.state.context
    ir = await ctx.http.get(f"{SETTINGS.base}/api/imageData", params={"file": source})
    if ir.status_code != 200 or not ir.headers.get("content-type", "").startswith("image/"):
        raise HTTPException(502, f"upstream imageData HTTP {ir.status_code}")
    body = ir.content
    ct = ir.headers.get("content-type", "image/png")
    _IMAGE_CACHE[source] = (body, ct)
    while len(_IMAGE_CACHE) > _IMAGE_CACHE_MAX:
        _IMAGE_CACHE.popitem(last=False)
    # Opportunistically archive — same logic as a live L4 capture, so a
    # source we proxy-fetched for the first time gets a long-term home.
    if pool is not None and SETTINGS.archive_enabled:
        import asyncio
        asyncio.create_task(
            _archive_save(
                pool, SETTINGS.archive_root,
                source=source, content=body, content_type=ct,
                origin_url=f"{SETTINGS.base}/api/imageData?file={source}",
            )
        )
    return Response(
        content=body, media_type=ct,
        headers={"cache-control": "public, max-age=300", "x-sentinel-cache": "upstream"},
    )


_XBAND_TS_RE = __import__("re").compile(r"_(\d{8})-(\d{4})\.png$")

# in-memory cache: scan_path → non-empty pixel fraction (0..1)
_activity_cache: dict[str, float] = {}


async def _activity_for(ctx, scan_path: str) -> float:
    cached = _activity_cache.get(scan_path)
    if cached is not None:
        return cached
    r = await ctx.http.get(f"{SETTINGS.base}/api/imageData", params={"file": scan_path})
    if r.status_code != 200:
        _activity_cache[scan_path] = 0.0
        return 0.0
    try:
        import io as _io
        import numpy as _np
        from PIL import Image as _Img
        arr = _np.asarray(_Img.open(_io.BytesIO(r.content)).convert("RGBA"))
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
        pd = await ctx.http.get(
            f"{SETTINGS.base}/api/productDetail", params={"file": cfg["details"]},
        )
        if pd.status_code != 200:
            raise HTTPException(502, "productDetail unreachable")
        for i, s in enumerate(pd.json().get("steps") or []):
            path = image_path(product_id, s["imageName"])
            out.append({"i": i, "ts": s.get("timestamp"),
                        "activity": await _activity_for(ctx, path)})
    elif radar:
        if radar not in RADAR_FOLDER:
            raise HTTPException(404, f"unknown radar: {radar}")
        folder = RADAR_FOLDER[radar]
        if prefix is None:
            try:
                prefix = moment_to_prefix(radar, moment)
            except KeyError:
                raise HTTPException(400, f"unknown moment: {moment}")
        r = await ctx.http.get(
            f"{SETTINGS.base}/api/xbandRadarImages/",
            params={"radarFolder": folder, "productPrefix": prefix},
        )
        if r.status_code != 200:
            raise HTTPException(502, "xbandRadarImages unreachable")
        for i, fname in enumerate(r.json().get("images") or []):
            ts = _parse_xband_ts(fname)
            out.append({"i": i,
                        "ts": ts.isoformat() if ts else None,
                        "activity": await _activity_for(ctx, fname)})
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
    r = await ctx.http.get(
        f"{SETTINGS.base}/api/xbandRadarImages/",
        params={"radarFolder": folder, "productPrefix": prefix},
    )
    if r.status_code != 200:
        raise HTTPException(502, f"upstream xbandRadarImages HTTP {r.status_code}")
    imgs = r.json().get("images") or []
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

    ir = await ctx.http.get(f"{SETTINGS.base}/api/imageData", params={"file": chosen})
    if ir.status_code != 200:
        raise HTTPException(502, f"upstream imageData HTTP {ir.status_code}")
    return Response(
        content=ir.content,
        media_type="image/png",
        headers={
            "cache-control": "no-store",
            "x-scan-name": chosen,
            "x-scan-ts": chosen_ts.isoformat() if chosen_ts else "",
            "x-prefix": prefix,
            "x-moment": moment,
        },
    )
