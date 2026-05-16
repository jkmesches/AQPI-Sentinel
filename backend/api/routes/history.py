"""History endpoints — read-only over older check_runs / alarms / metric_samples.

Range bounds are ISO-8601 strings. Filter params are all optional.
"""
from __future__ import annotations
import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response

router = APIRouter(prefix="/api/history")


def _parse_iso(s: str | None, default: datetime) -> datetime:
    if not s:
        return default
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"bad ISO datetime: {s!r}")


def _ser_alarm(a: dict) -> dict:
    return {
        "id":            a["id"],
        "check_id":      a["check_id"],
        "target":        a["target"],
        "stage":         a["stage"],
        "severity":      a["severity"],
        "opened_at":     a["opened_at"].isoformat(),
        "closed_at":     a["closed_at"].isoformat() if a.get("closed_at") else None,
        "suppressed_by": a.get("suppressed_by"),
        "message":       a["message"],
    }


def _ser_run(r: dict) -> dict:
    return {
        "id":          r["id"],
        "check_id":    r["check_id"],
        "target":      r["target"],
        "stage":       r["stage"],
        "status":      r["status"],
        "started_at":  r["started_at"].isoformat(),
        "finished_at": r["finished_at"].isoformat(),
        "summary":     r.get("summary"),
    }


@router.get("/alarms")
async def history_alarms(
    request: Request,
    since: str | None = None, until: str | None = None,
    stage: str | None = None, target: str | None = None,
    severity: str | None = None, limit: int = 500,
):
    now = datetime.now(timezone.utc)
    since_dt = _parse_iso(since, now - timedelta(days=7))
    until_dt = _parse_iso(until, now)
    pool = request.app.state.store.pool
    where = ["opened_at >= $1", "opened_at <= $2"]
    args: list = [since_dt, until_dt]
    if stage:
        args.append(stage); where.append(f"stage = ${len(args)}")
    if target:
        args.append(target); where.append(f"target = ${len(args)}")
    if severity:
        args.append(severity); where.append(f"severity = ${len(args)}")
    args.append(limit)
    sql = (
        "SELECT * FROM alarms WHERE " + " AND ".join(where)
        + f" ORDER BY opened_at DESC LIMIT ${len(args)}"
    )
    rows = await pool.fetch(sql, *args)
    return [_ser_alarm(dict(r)) for r in rows]


@router.get("/checks")
async def history_checks(
    request: Request,
    since: str | None = None, until: str | None = None,
    stage: str | None = None, target: str | None = None,
    status: str | None = None, limit: int = 1000,
):
    now = datetime.now(timezone.utc)
    since_dt = _parse_iso(since, now - timedelta(hours=1))
    until_dt = _parse_iso(until, now)
    pool = request.app.state.store.pool
    where = ["finished_at >= $1", "finished_at <= $2"]
    args: list = [since_dt, until_dt]
    if stage:
        args.append(stage); where.append(f"stage = ${len(args)}")
    if target:
        args.append(target); where.append(f"target = ${len(args)}")
    if status:
        args.append(status); where.append(f"status = ${len(args)}")
    args.append(limit)
    sql = (
        "SELECT id, check_id, target, stage, status, started_at, finished_at, summary "
        "FROM check_runs WHERE " + " AND ".join(where)
        + f" ORDER BY finished_at DESC LIMIT ${len(args)}"
    )
    rows = await pool.fetch(sql, *args)
    return [_ser_run(dict(r)) for r in rows]


@router.get("/export.csv")
async def history_export_csv(
    request: Request, type: str = "alarms",
    since: str | None = None, until: str | None = None,
    stage: str | None = None, target: str | None = None,
):
    if type not in ("alarms", "checks"):
        raise HTTPException(400, "type must be alarms|checks")
    fn = history_alarms if type == "alarms" else history_checks
    rows = await fn(request, since=since, until=until, stage=stage, target=target, limit=10000)
    buf = io.StringIO()
    if not rows:
        return Response(content="", media_type="text/csv")
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"content-disposition": f'attachment; filename="sentinel-{type}.csv"'})
