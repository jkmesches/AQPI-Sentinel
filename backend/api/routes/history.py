"""History endpoints — read-only over older check_runs / alarms / metric_samples.

Range bounds are ISO-8601 strings. Filter params are all optional.
"""
from __future__ import annotations
import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response

router = APIRouter(prefix="/api/history")

# Bucket strings the timeline endpoint understands.
_BUCKETS_S: dict[str, int] = {
    "1m":  60,
    "5m":  300,
    "15m": 900,
    "30m": 1800,
    "1h":  3600,
    "6h":  21600,
    "1d":  86400,
}
# Hard caps so a malicious request can't ask for years of data.
_MAX_BUCKETS = 1000
_MAX_SPAN_S  = 90 * 86400


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


@router.get("/timeline")
async def history_timeline(
    request: Request,
    bucket: str = "5m",
    until: str | None = None,
    limit: int = 120,
    stage: str | None = None,
    target: str | None = None,
):
    """Bucketed state-timeline grid.

    Returns one row per (time_bucket × check_id × target) with the worst status
    seen in that bucket. The client pivots into a wide grid; pagination is via
    the `older_cursor` field — pass it back as `until` to get the next page of
    older buckets.
    """
    if bucket not in _BUCKETS_S:
        raise HTTPException(400, f"bucket must be one of {sorted(_BUCKETS_S)}")
    bucket_s = _BUCKETS_S[bucket]
    if limit < 1 or limit > _MAX_BUCKETS:
        raise HTTPException(400, f"limit must be 1..{_MAX_BUCKETS}")

    now = datetime.now(timezone.utc)
    until_dt = _parse_iso(until, now)
    # Snap `until` down to the bucket boundary so pages line up cleanly.
    snapped_epoch = (int(until_dt.timestamp()) // bucket_s) * bucket_s
    until_snapped = datetime.fromtimestamp(snapped_epoch, tz=timezone.utc)
    span_s = bucket_s * limit
    if span_s > _MAX_SPAN_S:
        raise HTTPException(400, "requested span exceeds server cap")
    since_dt = until_snapped - timedelta(seconds=span_s)

    pool = request.app.state.store.pool
    where = ["finished_at >= $2", "finished_at < $3"]
    args: list = [bucket_s, since_dt, until_snapped]
    if stage:
        args.append(stage); where.append(f"stage = ${len(args)}")
    if target:
        args.append(target); where.append(f"target = ${len(args)}")

    sql = (
        "SELECT "
        "  to_timestamp(floor(extract(epoch from finished_at) / $1) * $1) "
        "    AT TIME ZONE 'UTC' AS bucket_ts, "
        "  check_id, target, stage, "
        "  MAX(CASE status "
        "        WHEN 'fail'  THEN 4 "
        "        WHEN 'error' THEN 4 "
        "        WHEN 'warn'  THEN 3 "
        "        WHEN 'pass'  THEN 2 "
        "        WHEN 'skip'  THEN 1 "
        "        ELSE 0 END) AS worst_rank, "
        "  bool_or(status = 'fail')  AS has_fail, "
        "  bool_or(status = 'error') AS has_error, "
        "  COUNT(*) AS n "
        "FROM check_runs "
        "WHERE " + " AND ".join(where) + " "
        "GROUP BY bucket_ts, check_id, target, stage "
        "ORDER BY bucket_ts DESC"
    )
    rows = await pool.fetch(sql, *args)

    # Dense bucket grid even when nothing ran — empty buckets are themselves
    # information, and the client's row count stays predictable.
    buckets: list[dict] = []
    ts_index: dict[int, int] = {}
    for i in range(limit):
        ts = until_snapped - timedelta(seconds=(i + 1) * bucket_s)
        buckets.append({"ts": ts.isoformat(), "cells": {}})
        ts_index[int(ts.timestamp())] = i

    rank_to_status = {3: "warn", 2: "pass", 1: "skip", 0: "unknown"}
    for r in rows:
        bts = r["bucket_ts"]
        if bts.tzinfo is None:
            bts = bts.replace(tzinfo=timezone.utc)
        idx = ts_index.get(int(bts.timestamp()))
        if idx is None:
            continue
        rank = r["worst_rank"] or 0
        if rank == 4:
            # Prefer 'fail' label when both happened — it conveys "the check
            # reported a real-world failure" rather than crashed plumbing.
            status = "fail" if r["has_fail"] else "error"
        else:
            status = rank_to_status[rank]
        key = f"{r['check_id']}|{r['target']}"
        buckets[idx]["cells"][key] = {"status": status, "n": int(r["n"])}

    older_cursor = (until_snapped - timedelta(seconds=span_s)).isoformat()
    return {
        "bucket":        bucket,
        "bucket_s":      bucket_s,
        "until":         until_snapped.isoformat(),
        "since":         since_dt.isoformat(),
        "buckets":       buckets,
        "older_cursor":  older_cursor,
    }


@router.get("/runs")
async def history_runs(
    request: Request,
    check_id: str, target: str,
    since: str, until: str,
    limit: int = 50,
):
    """Return the raw check_runs (with full payload + artifacts) for one
    (check_id, target) inside a time window. Used by the timeline cell
    detail panel."""
    since_dt = _parse_iso(since, datetime.now(timezone.utc) - timedelta(hours=1))
    until_dt = _parse_iso(until, datetime.now(timezone.utc))
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit must be 1..500")
    pool = request.app.state.store.pool
    rows = await pool.fetch(
        "SELECT id, check_id, target, stage, status, "
        "       started_at, finished_at, summary, payload, artifacts "
        "FROM check_runs "
        "WHERE check_id = $1 AND target = $2 "
        "  AND finished_at >= $3 AND finished_at < $4 "
        "ORDER BY finished_at DESC "
        "LIMIT $5",
        check_id, target, since_dt, until_dt, limit,
    )
    return [
        {
            "id":          r["id"],
            "check_id":    r["check_id"],
            "target":      r["target"],
            "stage":       r["stage"],
            "status":      r["status"],
            "started_at":  r["started_at"].isoformat(),
            "finished_at": r["finished_at"].isoformat(),
            "summary":     r["summary"],
            "payload":     r["payload"],
            "artifacts":   r["artifacts"] or [],
        }
        for r in rows
    ]


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
