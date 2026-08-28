"""GET /api/_debug/stats — point-in-time snapshot of the backend's
resource state. Cheap to call (no DB round-trip) so you can hit it from a
side terminal while the UI feels stuck and see immediately whether the
backend is wedged (httpx in-flight pegged, asyncpg pool exhausted, WS
clients piling up, event loop lagging) or whether the freeze is entirely
in the browser.
"""
from __future__ import annotations
import asyncio
import os
import time

from fastapi import APIRouter, Request

try:
    import resource as _resource  # POSIX-only
except ImportError:  # pragma: no cover — Windows fallback
    _resource = None  # type: ignore[assignment]

router = APIRouter(prefix="/api/_debug")

_PROC_START = time.time()


async def _event_loop_lag_ms() -> float:
    """Schedule a zero-delay sleep and measure how late it actually fires.
    A healthy loop returns <2ms; a wedged loop can take 100s+."""
    t0 = time.monotonic()
    await asyncio.sleep(0)
    return (time.monotonic() - t0) * 1000.0


@router.get("/stats")
async def stats(request: Request):
    app = request.app

    # Postgres pool ---------------------------------------------------
    pg: dict[str, object] = {"available": False}
    pool = getattr(getattr(app.state, "store", None), "pool", None)
    if pool is not None:
        try:
            size = pool.get_size()
            idle = pool.get_idle_size()
            pg = {
                "available":   True,
                "min_size":    pool.get_min_size(),
                "max_size":    pool.get_max_size(),
                "current":     size,
                "idle":        idle,
                "in_use":      size - idle,
            }
        except Exception as e:
            pg = {"available": True, "error": f"{type(e).__name__}: {e}"}

    # httpx -----------------------------------------------------------
    http_stats: dict[str, object] = {"available": False}
    ctx = getattr(app.state, "context", None)
    http = getattr(ctx, "http", None) if ctx else None
    if http is not None:
        http_stats = {
            "available":      True,
            "in_flight":      getattr(http, "in_flight", None),
            "total_requests": getattr(http, "total_requests", None),
            # Retry health. attempted-vs-succeeded is the honest read on
            # whether the retry is absorbing transient upstream blips or just
            # doubling load against something genuinely down: a high attempted
            # count with a low succeeded count means the latter.
            "retries_attempted": getattr(http, "retries_attempted", None),
            "retries_succeeded": getattr(http, "retries_succeeded", None),
        }

    # Inbound image-proxy hits. Distinguishes "the browser asked us" from
    # "we asked radarca" — the two differ once browser caching is working.
    # Imported at call time (not module scope) to avoid an import cycle;
    # NOT wrapped in a bare except, because the first version of this used
    # the wrong module path and the swallowed ImportError made the field
    # silently absent rather than obviously broken.
    from .upstream import _PROXY_HITS
    http_stats["proxy_hits"] = dict(_PROXY_HITS)

    # WebSocket clients -----------------------------------------------
    ws_state: dict[str, object] = {"available": False}
    ws = getattr(app.state, "ws", None)
    if ws is not None:
        ws_state = {
            "available": True,
            "clients":   len(getattr(ws, "_conns", []) or []),
        }

    # Network monitor -------------------------------------------------
    net_state: dict[str, object] = {"available": False}
    net = getattr(ctx, "network", None) if ctx else None
    if net is not None:
        net_state = {"available": True, **net.snapshot()}

    # Image cache -----------------------------------------------------
    image_cache: dict[str, object]
    try:
        from .upstream import _IMAGE_CACHE, _IMAGE_CACHE_MAX
        image_cache = {"size": len(_IMAGE_CACHE), "max": _IMAGE_CACHE_MAX}
    except Exception:
        image_cache = {"size": None}

    # Scheduler -------------------------------------------------------
    sched = getattr(app.state, "scheduler", None)
    sched_state: dict[str, object] = {"available": False}
    if sched is not None:
        tasks = getattr(sched, "_tasks", [])
        sched_state = {
            "available":     True,
            "tasks_total":   len(tasks),
            "tasks_running": sum(1 for t in tasks if not t.done()),
            "tasks_done":    sum(1 for t in tasks if t.done()),
        }

    # Process ---------------------------------------------------------
    rss_kb: int | None = None
    if _resource is not None:
        try:
            rss_kb = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
        except Exception:
            rss_kb = None

    return {
        "uptime_s":         round(time.time() - _PROC_START, 1),
        "pid":              os.getpid(),
        "rss_kb":           rss_kb,
        "event_loop_lag_ms": round(await _event_loop_lag_ms(), 2),
        "asyncpg":          pg,
        "httpx":            http_stats,
        "websocket":        ws_state,
        "scheduler":        sched_state,
        "network_monitor":  net_state,
        "image_cache":      image_cache,
    }
