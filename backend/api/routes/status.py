"""GET /api/status — top-level rollup, the dashboard's first paint source."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api")


def _serialize(row: dict) -> dict:
    """asyncpg returns datetime / list-of-text natively; JSON-serialize them."""
    return {
        "check_id":    row["check_id"],
        "target":      row["target"],
        "stage":       row["stage"],
        "status":      row["status"],
        "started_at":  row["started_at"].isoformat(),
        "finished_at": row["finished_at"].isoformat(),
        "summary":     row.get("summary"),
    }


@router.get("/status")
async def status(request: Request):
    store = request.app.state.store
    rows = await store.latest_per_check()
    by_stage: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_stage[r["stage"]].append(_serialize(r))
    counts = {
        stage: {
            "pass":  sum(1 for c in cs if c["status"] == "pass"),
            "warn":  sum(1 for c in cs if c["status"] == "warn"),
            "fail":  sum(1 for c in cs if c["status"] == "fail"),
            "error": sum(1 for c in cs if c["status"] == "error"),
            "total": len(cs),
        }
        for stage, cs in by_stage.items()
    }
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "stages": dict(by_stage),
        "counts": counts,
    }
