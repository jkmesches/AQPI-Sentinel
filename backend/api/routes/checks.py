"""GET /api/checks — registry introspection + per-check latest/history."""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Request

from ...registry import CHECKS
import datetime as _dt

router = APIRouter(prefix="/api/checks")


@router.get("")
async def list_checks():
    return [
        {
            "id":         c.id,
            "stage":      c.stage,
            "target":     c.target,
            "cadence_s":  c.cadence_s,
            "depends_on": c.depends_on,
        }
        for c in CHECKS.values()
    ]


def _serialize_run(row: dict) -> dict:
    return {
        "id":          row["id"],
        "check_id":    row["check_id"],
        "target":      row["target"],
        "stage":       row["stage"],
        "status":      row["status"],
        "started_at":  row["started_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat(),
        "summary":     row.get("summary"),
        "payload":     row.get("payload"),
        "artifacts":   row.get("artifacts") or [],
    }


@router.get("/{check_id}/latest")
async def latest(check_id: str, request: Request):
    if check_id not in CHECKS:
        raise HTTPException(404, "unknown check")
    row = await request.app.state.store.latest_run(check_id)
    if not row:
        return None
    return _serialize_run(row)


@router.get("/{check_id}/history")
async def history(check_id: str, request: Request, limit: int = 100):
    if check_id not in CHECKS:
        raise HTTPException(404, "unknown check")
    rows = await request.app.state.store.history(check_id, limit=limit)
    return [_serialize_run(r) for r in rows]


@router.get("/{check_id}/metrics")
async def metrics(check_id: str, metric: str, request: Request, limit: int = 500,
                  since: str | None = None):
    if check_id not in CHECKS:
        raise HTTPException(404, "unknown check")
    check = CHECKS[check_id]
    rows = await request.app.state.store.metric_series(
        check_id, check.target, metric, limit=limit, since=_parse_since(since)
    )
    return [{"ts": r["ts"].isoformat(), "value": r["value"]} for r in rows]


def _parse_since(raw: str | None):
    if not raw:
        return None
    try:
        v = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"bad since: {raw!r}")
    return v if v.tzinfo else v.replace(tzinfo=_dt.timezone.utc)


# Cap on series per request. Keeps one malformed query from turning into an
# unbounded fan-out of database round-trips.
_MAX_SERIES = 64


@router.get("/-/metrics_recent")
async def metrics_recent(request: Request, series: str, since: str | None = None,
                         limit: int = 40):
    """Many (check, metric) series in one request.

    The dashboard's sparklines used to be seeded once at page load and then
    fed only by WebSocket `run` events — which are broadcast ONLY on a status
    CHANGE. On a healthy system that is almost never: measured on production,
    104 of 2,698 runs in two hours, 3.85%. So 96% of metric samples never
    reached the browser, the Sparkline component's window kept advancing on
    its own 5s ticker, and the trace visibly drained away from the right while
    an operator sat and watched a perfectly healthy system.

    That is worse than a cosmetic bug: an emptying right edge is exactly the
    signal the component uses to mean "this upstream has stopped", the one it
    was rewritten to show after the 2026-05-19 outage. It was firing on
    healthy checks because the browser had stopped being told anything.

    Polling this on the existing refresh tick fixes it without touching the
    transition-only broadcast, which exists for a good reason — streaming
    every run wedged the tab at ~30 events/minute.

    `series` is a comma-separated list of `check_id|metric`. With `since`,
    only newer samples come back, so a steady-state poll returns a handful of
    points rather than the whole window.
    """
    want = [p for p in (series or "").split(",") if p.strip()]
    if not want:
        raise HTTPException(400, "series is required")
    if len(want) > _MAX_SERIES:
        raise HTTPException(400, f"too many series ({len(want)} > {_MAX_SERIES})")
    parsed_since = _parse_since(since)
    store = request.app.state.store

    out: dict[str, list[dict]] = {}
    for entry in want:
        check_id, _, metric = entry.partition("|")
        if not metric or check_id not in CHECKS:
            # Skip unknown pairs rather than failing the batch: the dashboard
            # asks for a fixed list and one renamed check should not blank
            # every other sparkline on the page.
            continue
        rows = await store.metric_series(
            check_id, CHECKS[check_id].target, metric,
            limit=limit, since=parsed_since,
        )
        out[entry] = [{"ts": r["ts"].isoformat(), "value": r["value"]} for r in rows]
    return out
