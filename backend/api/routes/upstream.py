"""Thin upstream proxy — fetch radarca PNGs through Sentinel so the frontend
can use them as MapLibre raster sources without CORS issues."""
from __future__ import annotations
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
    latest = steps[-1]["imageName"]
    file_path = image_path(product_id, latest)
    try:
        ir = await ctx.http.get(f"{SETTINGS.base}/api/imageData", params={"file": file_path})
    except Exception as e:
        raise HTTPException(502, f"Upstream image fetch failed: {humanize_error(e)}")
    if ir.status_code != 200:
        raise HTTPException(502, f"Upstream image returned HTTP {ir.status_code}")
    return Response(
        content=ir.content,
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
    try:
        pd = await ctx.http.get(
            f"{SETTINGS.base}/api/productDetail", params={"file": cfg["details"]},
        )
    except Exception as e:
        raise HTTPException(502, f"Upstream unavailable: {humanize_error(e)}")
    if pd.status_code != 200:
        raise HTTPException(502, f"Upstream returned HTTP {pd.status_code}")
    try:
        raw = pd.json().get("steps") or []
    except Exception:
        raise HTTPException(502, "Upstream returned malformed JSON")
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
    try:
        ir = await ctx.http.get(f"{SETTINGS.base}/api/imageData", params={"file": file_path})
    except Exception as e:
        raise HTTPException(502, f"Upstream image fetch failed: {humanize_error(e)}")
    if ir.status_code != 200:
        raise HTTPException(502, f"Upstream image returned HTTP {ir.status_code}")
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


def _tilt_guard(radar: str, el: int, moment: str) -> None:
    if radar not in _TILT_RADARS:
        raise HTTPException(404, f"no tilt imagery for radar {radar}")
    if moment not in _TILT_MOMENTS:
        raise HTTPException(400, f"unknown moment {moment}")
    if el < 1 or el > 4:
        raise HTTPException(400, "el must be 1..4")


@router.get("/tilt_steps")
async def tilt_steps(radar: str, el: int, moment: str):
    """Metadata for a (radar, elevation) tilt: center, range, scan angle,
    plus the newest frame's timestamp.

    Phase 1 reads only frame 0 (newest) — enough to georeference + label
    a static overlay. Frame enumeration for scrubbing is a Phase 2 add.
    """
    _tilt_guard(radar, el, moment)
    url = f"{_RD_BASE}/{radar}/json/el_{el}/radar_plot_0.json"
    try:
        r = await _rd_client().get(url)
    except Exception as e:
        raise HTTPException(502, f"radar-display unavailable: {humanize_error(e)}")
    if r.status_code != 200:
        raise HTTPException(502, f"radar-display returned HTTP {r.status_code}")
    try:
        j = r.json()
    except Exception:
        raise HTTPException(502, "radar-display returned malformed JSON")
    return {
        "radar": radar,
        "el": el,
        "moment": moment,
        "center": [j["Longitude"]["Value"], j["Latitude"]["Value"]],
        "range_km": j["MaxRange"]["Value"],
        "angle": j["Scan"]["Angle"]["Value"],
        "frames": _TILT_FRAMES,
        "latest_ts": j.get("Time", {}).get("Value"),
    }


@router.get("/tilt_image.png")
async def tilt_image(radar: str, el: int, moment: str, frame: int = 0):
    """Proxy a single per-elevation PPI PNG (frame 0 = newest)."""
    _tilt_guard(radar, el, moment)
    if frame < 0 or frame >= _TILT_FRAMES:
        raise HTTPException(400, f"frame out of range (0..{_TILT_FRAMES - 1})")
    url = f"{_RD_BASE}/{radar}/images/el_{el}/{moment}_{frame}.png"
    try:
        r = await _rd_client().get(url)
    except Exception as e:
        raise HTTPException(502, f"radar-display unavailable: {humanize_error(e)}")
    if r.status_code != 200:
        raise HTTPException(502, f"radar-display returned HTTP {r.status_code}")
    return Response(
        content=r.content,
        media_type="image/png",
        headers={"cache-control": "no-store", "x-tilt-angle-el": str(el)},
    )
