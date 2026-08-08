"""Retention — offload aged rows to cold storage, then reclaim the hot DB.

Why this exists: `check_runs` and `metric_samples` are append-only and grow
~28k and ~72k rows/day respectively. Between 2026-05-16 and 2026-08-08 they
reached 2.2M and 6.0M rows (3.5GB combined). That is survivable on its own,
but it is the same growth curve that turned an unindexed sort in
`latest_per_check()` into a disk-full outage, and the LXC's local disk is the
scarce resource. See docs/MAINTENANCE.md "Disk space".

**Offload, not delete.** Nothing leaves the system without first being written
to `SETTINGS.cold_root` (production: the NFS share on Erebor, 2.5TB) as
gzipped CSV, with the file fsync'd and its COPY row count verified against
what we are about to remove. If the export fails or the counts disagree, the
delete does not run. Restoring is a plain `COPY ... FROM` — see the runbook.

Sweeps are bounded by a snapshot taken before the export (`id <= max_id` for
check_runs), so rows written while a sweep is in flight are never inside its
delete predicate. Deletes are chunked to keep transactions short.

Note on disk: deleting rows returns space to Postgres' free space map, not to
the filesystem — steady-state size stops growing rather than shrinking. That
is the intent. Reclaiming to the OS needs VACUUM FULL (exclusive lock, needs
free space equal to the table); the runbook covers it as a rare operation.
"""
from __future__ import annotations
import asyncio
import gzip
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Rows per DELETE statement. Matches reprocess_engine's CHUNK for the same
# reason: short transactions, no long-held locks on a live table.
CHUNK = 5_000


class OffloadError(RuntimeError):
    """Export failed or did not verify. The delete phase must not run."""


def _cutoff(days: int, now: datetime) -> datetime:
    return now - timedelta(days=days)


async def _export_query(pool, query: str, args: list[Any], dest: Path) -> int:
    """COPY the results of `query` to a gzipped CSV at `dest`. Returns rows.

    Writes to `<dest>.part` and renames only after fsync, so an interrupted
    sweep can never leave a truncated file that looks complete to the
    verification step (or to a human reading the cold store later).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    raw = dest.with_suffix(dest.suffix + ".raw")

    async with pool.acquire() as conn:
        status = await conn.copy_from_query(
            query, *args, output=str(raw), format="csv", header=True
        )
    # asyncpg returns the COPY command tag, e.g. "COPY 12345".
    try:
        n = int(str(status).split()[-1])
    except (ValueError, IndexError) as e:
        raw.unlink(missing_ok=True)
        raise OffloadError(f"could not parse COPY status {status!r}") from e

    try:
        with open(raw, "rb") as fsrc, gzip.open(part, "wb", compresslevel=6) as fdst:
            shutil.copyfileobj(fsrc, fdst, length=1024 * 1024)
        with open(part, "rb") as fh:
            os.fsync(fh.fileno())
    finally:
        raw.unlink(missing_ok=True)

    if part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        raise OffloadError(f"export to {dest.name} produced an empty file")
    os.replace(part, dest)
    return n


async def _delete_chunked(pool, table: str, where: str, args: list[Any]) -> int:
    """DELETE ... WHERE <where>, CHUNK rows at a time. Returns rows removed.

    ctid-based so it works on metric_samples, which has no primary key.
    """
    removed = 0
    while True:
        status = await pool.execute(
            f"""
            DELETE FROM {table} WHERE ctid IN (
                SELECT ctid FROM {table} WHERE {where} LIMIT {CHUNK}
            )
            """,
            *args,
        )
        n = int(str(status).split()[-1])
        removed += n
        if n == 0:
            return removed
        # Yield so a sweep never starves the scheduler's write path.
        await asyncio.sleep(0)


async def offload_check_runs(pool, cold_root: Path, cutoff: datetime) -> dict:
    """Export + drop check_runs older than `cutoff`."""
    row = await pool.fetchrow(
        "SELECT max(id) AS max_id, count(*) AS n FROM check_runs WHERE finished_at < $1",
        cutoff,
    )
    if not row or not row["n"]:
        return {"table": "check_runs", "rows": 0}
    max_id, expect = row["max_id"], row["n"]

    span = await pool.fetchrow(
        "SELECT min(finished_at) AS lo, max(finished_at) AS hi FROM check_runs "
        "WHERE id <= $1 AND finished_at < $2",
        max_id, cutoff,
    )
    stamp = f"{span['lo']:%Y%m%dT%H%M%S}_{span['hi']:%Y%m%dT%H%M%S}"
    dest = cold_root / "check_runs" / f"check_runs_{stamp}.csv.gz"

    exported = await _export_query(
        pool,
        """
        SELECT id, check_id, target, stage, status, started_at,
               finished_at, summary, payload, artifacts
        FROM check_runs
        WHERE id <= $1 AND finished_at < $2
        ORDER BY id
        """,
        [max_id, cutoff],
        dest,
    )
    if exported != expect:
        raise OffloadError(
            f"check_runs export wrote {exported} rows, expected {expect} — "
            f"keeping {dest} and skipping the delete"
        )

    removed = await _delete_chunked(
        pool, "check_runs", "id <= $1 AND finished_at < $2", [max_id, cutoff]
    )
    log.info("retention: offloaded %d check_runs to %s", removed, dest.name)
    return {"table": "check_runs", "rows": removed, "file": str(dest)}


async def offload_metric_samples(pool, cold_root: Path, cutoff: datetime) -> dict:
    """Export + drop metric_samples older than `cutoff`.

    No id column here, so the sweep is bounded by `ts` alone — safe because
    every insert stamps ts at write time, so nothing lands behind the cutoff
    while the sweep runs.
    """
    row = await pool.fetchrow(
        "SELECT count(*) AS n, min(ts) AS lo, max(ts) AS hi FROM metric_samples WHERE ts < $1",
        cutoff,
    )
    if not row or not row["n"]:
        return {"table": "metric_samples", "rows": 0}
    expect = row["n"]

    stamp = f"{row['lo']:%Y%m%dT%H%M%S}_{row['hi']:%Y%m%dT%H%M%S}"
    dest = cold_root / "metric_samples" / f"metric_samples_{stamp}.csv.gz"

    exported = await _export_query(
        pool,
        """
        SELECT ts, check_id, target, metric, value
        FROM metric_samples
        WHERE ts < $1
        ORDER BY ts
        """,
        [cutoff],
        dest,
    )
    if exported != expect:
        raise OffloadError(
            f"metric_samples export wrote {exported} rows, expected {expect} — "
            f"keeping {dest} and skipping the delete"
        )

    removed = await _delete_chunked(pool, "metric_samples", "ts < $1", [cutoff])
    log.info("retention: offloaded %d metric_samples to %s", removed, dest.name)
    return {"table": "metric_samples", "rows": removed, "file": str(dest)}


async def prune_archive(pool, archive_root: Path, cutoff: datetime) -> dict:
    """Drop archived images not seen since `cutoff`.

    Opt-in via SENTINEL_ARCHIVE_RETENTION_DAYS, which was parsed into config
    but read by nothing until 2026-08-08 — a documented knob that silently did
    nothing. Production leaves it unset (permanent) because the archive now
    lives on the NFS share, where 18GB is not worth pruning.

    Keyed on `last_seen_at`, not `first_seen_at`: images are content-addressed
    and deduped, so a frame that keeps recurring upstream stays live no matter
    how long ago it first appeared.
    """
    rows = await pool.fetch(
        "SELECT sha256, ext FROM image_archive WHERE last_seen_at < $1", cutoff
    )
    if not rows:
        return {"table": "image_archive", "rows": 0, "bytes": 0}

    freed = 0
    for r in rows:
        path = archive_root / r["sha256"][:2] / f"{r['sha256']}.{r['ext']}"
        try:
            freed += path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("retention: could not unlink %s", path, exc_info=True)

    shas = [r["sha256"] for r in rows]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM image_index   WHERE sha256 = ANY($1::text[])", shas)
        await conn.execute("DELETE FROM image_archive WHERE sha256 = ANY($1::text[])", shas)
    log.info("retention: pruned %d archived images (%.1f MB)", len(shas), freed / 1e6)
    return {"table": "image_archive", "rows": len(shas), "bytes": freed}


async def run_once(pool, *, settings, now: datetime | None = None) -> list[dict]:
    """One full sweep. Safe to call by hand; that's what the admin path does."""
    now = now or datetime.now(timezone.utc)
    results: list[dict] = []

    if settings.db_retention_days:
        cutoff = _cutoff(settings.db_retention_days, now)
        for fn in (offload_check_runs, offload_metric_samples):
            try:
                results.append(await fn(pool, settings.cold_root, cutoff))
            except Exception as e:
                # One table failing must not block the other, and must not
                # take the backend down — this runs unattended.
                log.exception("retention: %s failed", fn.__name__)
                results.append({"table": fn.__name__, "error": str(e)})

    if settings.archive_retention_days:
        cutoff = _cutoff(settings.archive_retention_days, now)
        try:
            results.append(await prune_archive(pool, settings.archive_root, cutoff))
        except Exception as e:
            log.exception("retention: prune_archive failed")
            results.append({"table": "image_archive", "error": str(e)})

    return results


class RetentionTask:
    """Daily sweep at `settings.retention_hour_utc`.

    Deliberately a wall-clock hour rather than an interval: a restart-driven
    interval timer would re-run the sweep on every deploy, and the sweep is
    the one background job that deletes things.
    """

    def __init__(self, pool, settings):
        self._pool = pool
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._last_run_date = None

    async def start(self) -> None:
        if not (self._settings.db_retention_days or self._settings.archive_retention_days):
            log.info("retention: no retention configured — sweep disabled")
            return
        self._task = asyncio.create_task(self._loop())
        log.info(
            "retention: daily sweep armed for %02d:00 UTC (db=%s days, archive=%s days)",
            self._settings.retention_hour_utc,
            self._settings.db_retention_days or "off",
            self._settings.archive_retention_days or "off",
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                now = datetime.now(timezone.utc)
                if (now.hour == self._settings.retention_hour_utc
                        and self._last_run_date != now.date()):
                    self._last_run_date = now.date()
                    await run_once(self._pool, settings=self._settings, now=now)
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("retention: sweep loop error")
                await asyncio.sleep(300)
