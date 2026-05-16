"""GET /api/checks — registry introspection + per-check latest/history."""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Request

from ...registry import CHECKS

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
async def metrics(check_id: str, metric: str, request: Request, limit: int = 500):
    if check_id not in CHECKS:
        raise HTTPException(404, "unknown check")
    check = CHECKS[check_id]
    rows = await request.app.state.store.metric_series(
        check_id, check.target, metric, limit=limit
    )
    return [{"ts": r["ts"].isoformat(), "value": r["value"]} for r in rows]
