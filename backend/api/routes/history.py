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


def _csv_list(s: str | None) -> list[str] | None:
    """Filter params accept either a single value (`stage=L2`) or a comma-
    separated list (`stage=L1,L2,L4-T1T2`). Returns the canonical list, or
    None if nothing was supplied (caller drops the filter)."""
    if not s:
        return None
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return parts or None


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
    check_id: str | None = None,
    severity: str | None = None, limit: int = 500,
):
    now = datetime.now(timezone.utc)
    since_dt = _parse_iso(since, now - timedelta(days=7))
    until_dt = _parse_iso(until, now)
    pool = request.app.state.store.pool
    where = ["opened_at >= $1", "opened_at <= $2"]
    args: list = [since_dt, until_dt]
    # All three of stage/target/severity accept a comma-separated list so the
    # history page's multi-select chips can serialize directly. Single value
    # remains valid for back-compat / curl users.
    stages = _csv_list(stage)
    if stages:
        args.append(stages); where.append(f"stage = ANY(${len(args)}::text[])")
    targets = _csv_list(target)
    if targets:
        args.append(targets); where.append(f"target = ANY(${len(args)}::text[])")
    if check_id:
        args.append(check_id); where.append(f"check_id = ${len(args)}")
    sevs = _csv_list(severity)
    if sevs:
        args.append(sevs); where.append(f"severity = ANY(${len(args)}::text[])")
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
    check_id: str | None = None,
    status: str | None = None, limit: int = 1000,
):
    now = datetime.now(timezone.utc)
    since_dt = _parse_iso(since, now - timedelta(hours=1))
    until_dt = _parse_iso(until, now)
    pool = request.app.state.store.pool
    where = ["finished_at >= $1", "finished_at <= $2"]
    args: list = [since_dt, until_dt]
    stages = _csv_list(stage)
    if stages:
        args.append(stages); where.append(f"stage = ANY(${len(args)}::text[])")
    targets = _csv_list(target)
    if targets:
        args.append(targets); where.append(f"target = ANY(${len(args)}::text[])")
    if check_id:
        args.append(check_id); where.append(f"check_id = ${len(args)}")
    statuses = _csv_list(status)
    if statuses:
        args.append(statuses); where.append(f"status = ANY(${len(args)}::text[])")
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
    # Snap `until` UP to the next bucket boundary so the partial in-flight
    # bucket containing wall-clock-now is INCLUDED in the grid. Previously
    # we snapped DOWN, which truncated 0..(bucket_s) seconds of fresh data
    # — visible at fine grains (5 m → no truncation when wall-clock hits a
    # 5-min mark) but up to 14 min of fresh data dropped at 15 m grain.
    # Failures within that window appeared on the 5 m view and vanished
    # on a switch to 15 m. The dense-bucket loop below already tolerates
    # an in-flight bucket — empty cells are normal during a partial fill.
    epoch = int(until_dt.timestamp())
    if epoch % bucket_s == 0:
        snapped_epoch = epoch
    else:
        snapped_epoch = ((epoch // bucket_s) + 1) * bucket_s
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
        # fail outranks error: "the monitored thing is broken" is a stronger
        # statement than "our probe could not determine its state". They were
        # equal until 2026-08-25, which is why upstream API timeouts rendered
        # as radar outages on the timeline.
        "        WHEN 'fail'  THEN 5 "
        "        WHEN 'error' THEN 4 "
        "        WHEN 'warn'  THEN 3 "
        "        WHEN 'pass'  THEN 2 "
        "        WHEN 'skip'  THEN 1 "
        "        ELSE 0 END) AS worst_rank, "
        "  bool_or(status = 'fail')  AS has_fail, "
        "  bool_or(status = 'error') AS has_error, "
        # Surface payload.reason aggregations so the client can render a
        # small badge on cells whose runs were demoted because an upstream
        # was unhealthy (vs. an intrinsic skip like a forecast product
        # skipping its step-count sub-check). We pick the first non-pass
        # reason seen — 'upstream_unhealthy' wins when it appears.
        "  bool_or(payload->>'reason' = 'upstream_unhealthy') AS any_upstream, "
        "  bool_or(payload->>'reason' = 'upstream_api')       AS any_upstream_api, "
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

    rank_to_status = {4: "error", 3: "warn", 2: "pass", 1: "skip", 0: "unknown"}
    for r in rows:
        bts = r["bucket_ts"]
        if bts.tzinfo is None:
            bts = bts.replace(tzinfo=timezone.utc)
        idx = ts_index.get(int(bts.timestamp()))
        if idx is None:
            continue
        rank = r["worst_rank"] or 0
        # rank 5 == fail. A bucket containing both a fail and an error still
        # reads 'fail': a real-world failure is the more important fact.
        status = "fail" if rank == 5 else rank_to_status[rank]
        key = f"{r['check_id']}|{r['target']}"
        cell: dict = {"status": status, "n": int(r["n"])}
        # Only emit `reason` when it's load-bearing — the field is omitted
        # for vanilla skips so the JSON stays small over the wire.
        if r.get("any_upstream"):
            cell["reason"] = "upstream_unhealthy"
        elif r.get("any_upstream_api"):
            cell["reason"] = "upstream_api"
        buckets[idx]["cells"][key] = cell

    # `older_cursor` is purely arithmetic (until - span), so on its own it is
    # never null and the client would offer "load older" forever, paging into
    # empty grids well past the beginning of recorded history. Report the
    # oldest row we actually hold so the client can stop at the real boundary
    # and say something true about why it stopped — "older data has been
    # offloaded to cold storage" reads very differently from "end of data".
    oldest_row = await pool.fetchval("SELECT min(finished_at) FROM check_runs")
    older_cursor = (until_snapped - timedelta(seconds=span_s)).isoformat()
    has_older = oldest_row is not None and since_dt > oldest_row
    return {
        "bucket":        bucket,
        "bucket_s":      bucket_s,
        "until":         until_snapped.isoformat(),
        "since":         since_dt.isoformat(),
        "buckets":       buckets,
        "older_cursor":  older_cursor if has_older else None,
        "oldest_available": oldest_row.isoformat() if oldest_row else None,
    }


@router.get("/report.csv")
async def history_report_csv(
    request: Request,
    since: str | None = None, until: str | None = None,
    bucket: str = "5m",
    stage: str | None = None, target: str | None = None,
    check_id: str | None = None,
):
    """Long-format bucketed CSV report. One row per (time_bucket × check × target),
    with the worst status seen in that bucket plus the run count.

    Used by the Timeline page's Export Report dialog. The bucketing logic
    mirrors /api/history/timeline so the CSV matches what's on screen.
    """
    if bucket not in _BUCKETS_S:
        raise HTTPException(400, f"bucket must be one of {sorted(_BUCKETS_S)}")
    bucket_s = _BUCKETS_S[bucket]
    now = datetime.now(timezone.utc)
    since_dt = _parse_iso(since, now - timedelta(hours=1))
    until_dt = _parse_iso(until, now)
    # Snap to bucket boundaries so the report aligns with the on-screen grid.
    until_snapped = datetime.fromtimestamp(
        (int(until_dt.timestamp()) // bucket_s) * bucket_s, tz=timezone.utc,
    )
    since_snapped = datetime.fromtimestamp(
        (int(since_dt.timestamp()) // bucket_s) * bucket_s, tz=timezone.utc,
    )
    span_s = int((until_snapped - since_snapped).total_seconds())
    if span_s <= 0:
        raise HTTPException(400, "since must be before until")
    if span_s > _MAX_SPAN_S:
        raise HTTPException(400, "requested span exceeds server cap (90 days)")

    pool = request.app.state.store.pool
    where = ["finished_at >= $2", "finished_at < $3"]
    args: list = [bucket_s, since_snapped, until_snapped]
    stages = _csv_list(stage)
    if stages:
        args.append(stages); where.append(f"stage = ANY(${len(args)}::text[])")
    targets = _csv_list(target)
    if targets:
        args.append(targets); where.append(f"target = ANY(${len(args)}::text[])")
    ids = _csv_list(check_id)
    if ids:
        args.append(ids); where.append(f"check_id = ANY(${len(args)}::text[])")

    sql = (
        "SELECT "
        "  to_timestamp(floor(extract(epoch from finished_at) / $1) * $1) "
        "    AT TIME ZONE 'UTC' AS bucket_ts, "
        "  check_id, target, stage, "
        "  MAX(CASE status "
        # fail outranks error: "the monitored thing is broken" is a stronger
        # statement than "our probe could not determine its state". They were
        # equal until 2026-08-25, which is why upstream API timeouts rendered
        # as radar outages on the timeline.
        "        WHEN 'fail'  THEN 5 "
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
        "ORDER BY bucket_ts ASC, stage, check_id, target"
    )
    rows = await pool.fetch(sql, *args)
    rank_to_status = {4: "error", 3: "warn", 2: "pass", 1: "skip", 0: "unknown"}

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["bucket_ts", "stage", "check_id", "target", "status", "n_runs"])
    for r in rows:
        bts = r["bucket_ts"]
        if bts.tzinfo is None:
            bts = bts.replace(tzinfo=timezone.utc)
        rank = r["worst_rank"] or 0
        # rank 5 == fail (see the CASE above); everything else maps directly.
        status = "fail" if rank == 5 else rank_to_status[rank]
        w.writerow([
            bts.isoformat(),
            r["stage"],
            r["check_id"],
            r["target"] or "",
            status,
            int(r["n"]),
        ])

    fname = f"sentinel-report-{bucket}-{since_snapped.strftime('%Y%m%dT%H%M')}-{until_snapped.strftime('%Y%m%dT%H%M')}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"content-disposition": f'attachment; filename="{fname}"'},
    )


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
    check_id: str | None = None,
    severity: str | None = None, status: str | None = None,
):
    """CSV download that honors every filter the corresponding table view does.
    Severity is only meaningful for alarms, status only for checks; the other
    is silently ignored for the chosen `type`."""
    if type not in ("alarms", "checks"):
        raise HTTPException(400, "type must be alarms|checks")
    if type == "alarms":
        rows = await history_alarms(
            request, since=since, until=until,
            stage=stage, target=target, check_id=check_id,
            severity=severity, limit=10000,
        )
    else:
        rows = await history_checks(
            request, since=since, until=until,
            stage=stage, target=target, check_id=check_id,
            status=status, limit=10000,
        )
    buf = io.StringIO()
    if not rows:
        return Response(content="", media_type="text/csv")
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"content-disposition": f'attachment; filename="sentinel-{type}.csv"'})
