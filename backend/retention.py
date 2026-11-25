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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncpg

from backend.storage_io import (STORAGE_IO, StorageBusyError, StorageNotMountedError,
                                require_marker)

log = logging.getLogger(__name__)

# Rows per DELETE statement. Matches reprocess_engine's CHUNK for the same
# reason: short transactions, no long-held locks on a live table.
CHUNK = 5_000

# COPY output is gathered to this size before each write to the cold store, so
# a sweep of millions of rows makes a few hundred calls to the store's pool.
FLUSH_BYTES = 4 * 1024 * 1024
# A 4 MB write or a mkdir that takes a minute is a stalled share. The whole
# COPY of a 60-day backlog took under a minute on the reference deployment,
# so half an hour means it is stuck. Either way the sweep exports nothing and
# deletes nothing, and the next day's sweep tries again.
FILE_TIMEOUT_S = 60.0
EXPORT_TIMEOUT_S = 1800.0


class OffloadError(RuntimeError):
    """Export failed or did not verify. The delete phase must not run."""


def _cutoff(days: int, now: datetime) -> datetime:
    return now - timedelta(days=days)


def _finish(fh: gzip.GzipFile, part: Path, dest: Path) -> None:
    """Close the gzip stream, fsync it, and rename it into place. Runs on the pool.

    Raises:
        OffloadError: When the file came out empty.
    """
    fh.close()
    with open(part, "rb") as f:
        os.fsync(f.fileno())
    if part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        raise OffloadError(f"export to {dest.name} produced an empty file")
    os.replace(part, dest)


def _discard(fh: gzip.GzipFile | None, part: Path) -> None:
    """Close and remove an export that will not be published. Runs on the pool."""
    if fh is not None:
        fh.close()
    part.unlink(missing_ok=True)


class _GzipSink:
    """Takes COPY output and writes it gzipped to `part` through the stores' pool.

    Args:
        part: The file to write, beside its final name.
    """

    def __init__(self, part: Path) -> None:
        self._part = part
        self._fh: gzip.GzipFile | None = None
        self._buf = bytearray()

    async def open(self) -> None:
        """Create the parent folder and open the gzip stream."""
        await STORAGE_IO.run(_mkdir, self._part.parent, timeout_s=FILE_TIMEOUT_S)
        self._fh = await STORAGE_IO.run(gzip.open, self._part, "wb", 6,
                                        timeout_s=FILE_TIMEOUT_S)

    async def write(self, data: bytes) -> None:
        """Take one chunk of COPY output. asyncpg awaits this per message."""
        self._buf += data
        if len(self._buf) >= FLUSH_BYTES:
            await self._flush()

    async def publish(self, dest: Path) -> None:
        """Write what is left, fsync, and rename to `dest`."""
        await self._flush()
        if self._fh is None:
            raise OffloadError(f"export to {dest.name} was never opened")
        await STORAGE_IO.run(_finish, self._fh, self._part, dest, timeout_s=FILE_TIMEOUT_S)

    async def discard(self) -> None:
        """Close and remove the part file, as far as the store answers."""
        try:
            await STORAGE_IO.run(_discard, self._fh, self._part, timeout_s=FILE_TIMEOUT_S)
        except (StorageBusyError, TimeoutError, OSError) as e:
            log.warning("retention: could not remove %s (%s)", self._part,
                        str(e) or "timed out")

    async def _flush(self) -> None:
        if not self._buf or self._fh is None:
            return
        chunk, self._buf = bytes(self._buf), bytearray()
        await STORAGE_IO.run(self._fh.write, chunk, timeout_s=FILE_TIMEOUT_S)


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


async def _require_store(root: Path, marker: str | None) -> None:
    """Raise OffloadError unless the store at `root` holds its marker file.

    Args:
        root: The cold or archive store's root.
        marker: SETTINGS.storage_marker, or None for no test.

    Raises:
        OffloadError: When the marker is missing or the store gives no answer.
    """
    if not marker:
        return
    try:
        await STORAGE_IO.run(require_marker, root, marker, timeout_s=FILE_TIMEOUT_S)
    except StorageNotMountedError as e:
        raise OffloadError(str(e)) from e
    except (StorageBusyError, TimeoutError) as e:
        raise OffloadError(f"{root} gave no answer: {str(e) or 'timed out'}") from e


async def _export_query(pool: asyncpg.Pool, query: str, args: list[Any], dest: Path) -> int:
    """COPY the results of `query` to a gzipped CSV at `dest`. Returns rows.

    Writes to `<dest>.part` and renames only after fsync, so an interrupted
    sweep can never leave a truncated file that looks complete to the
    verification step (or to a human reading the cold store later). The COPY
    streams into the gzip writer, and every call on the cold store goes
    through the stores' pool (backend/storage_io.py), since the store may be a
    `hard` NFS mount that blocks while its server is down.

    Raises:
        OffloadError: When the export fails, stalls or does not verify.
    """
    sink = _GzipSink(dest.with_suffix(dest.suffix + ".part"))
    try:
        await sink.open()
        async with pool.acquire() as conn:
            status = await asyncio.wait_for(
                conn.copy_from_query(query, *args, output=sink.write,
                                     format="csv", header=True),
                EXPORT_TIMEOUT_S)
        # asyncpg returns the COPY command tag, e.g. "COPY 12345".
        try:
            n = int(str(status).split()[-1])
        except (ValueError, IndexError) as e:
            raise OffloadError(f"could not parse COPY status {status!r}") from e
        await sink.publish(dest)
    except (StorageBusyError, TimeoutError) as e:
        await sink.discard()
        raise OffloadError(f"the cold store gave no answer exporting {dest.name}: "
                           f"{str(e) or 'timed out'}") from e
    except Exception:
        # Not on cancellation: a shutdown should not wait on a stalled share,
        # and a .part left behind is never read as an export.
        await sink.discard()
        raise
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


async def offload_check_runs(pool: asyncpg.Pool, cold_root: Path, cutoff: datetime, *,
                             marker: str | None) -> dict[str, Any]:
    """Export + drop check_runs older than `cutoff`.

    Args:
        pool: The store's pool.
        cold_root: Where exports go.
        cutoff: Rows that finished before this are offloaded.
        marker: SETTINGS.storage_marker, which must be in `cold_root` first.

    Returns:
        The table, the rows removed and the export's path.

    Raises:
        OffloadError: When the cold store is not attached, gives no answer, or
            the export does not verify. Nothing is deleted then.
    """
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

    await _require_store(cold_root, marker)
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


async def offload_metric_samples(pool: asyncpg.Pool, cold_root: Path, cutoff: datetime, *,
                                 marker: str | None) -> dict[str, Any]:
    """Export + drop metric_samples older than `cutoff`.

    No id column here, so the sweep is bounded by `ts` alone. That holds
    because every insert stamps ts at write time, so nothing lands behind the
    cutoff while the sweep runs.

    Args:
        pool: The store's pool.
        cold_root: Where exports go.
        cutoff: Samples taken before this are offloaded.
        marker: SETTINGS.storage_marker, which must be in `cold_root` first.

    Returns:
        The table, the rows removed and the export's path.

    Raises:
        OffloadError: When the cold store is not attached, gives no answer, or
            the export does not verify. Nothing is deleted then.
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

    await _require_store(cold_root, marker)
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


async def prune_archive(pool: asyncpg.Pool, archive_root: Path, cutoff: datetime, *,
                        marker: str | None) -> dict[str, Any]:
    """Drop archived images not seen since `cutoff`.

    Opt-in via SENTINEL_ARCHIVE_RETENTION_DAYS, which was parsed into config
    but read by nothing until 2026-08-08, a documented knob that silently did
    nothing. Production leaves it unset (permanent) because the archive now
    lives on the NFS share, where 18GB is not worth pruning.

    Keyed on `last_seen_at`, not `first_seen_at`: images are content-addressed
    and deduped, so a frame that keeps recurring upstream stays live no matter
    how long ago it first appeared.

    Args:
        pool: The store's pool.
        archive_root: The archive's root.
        cutoff: Images not seen since this are dropped.
        marker: SETTINGS.storage_marker, which must be in `archive_root` before
            any row is deleted, so rows never go while their files stay put on a
            share that is not attached.

    Returns:
        The table, the rows dropped and the bytes freed.

    Raises:
        OffloadError: When the archive store is not attached or gives no answer.
    """
    rows = await pool.fetch(
        "SELECT sha256, ext FROM image_archive WHERE last_seen_at < $1", cutoff
    )
    if not rows:
        return {"table": "image_archive", "rows": 0, "bytes": 0}
    await _require_store(archive_root, marker)

    # Rows first, files after the commit. The other way round, a delete that
    # failed (a reference added since, or the database down) left rows whose
    # files were gone, and every sweep repeated it; a file left after a
    # committed delete only costs disk.
    shas = [r["sha256"] for r in rows]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM image_observations WHERE sha256 = ANY($1::text[])",
                           shas)
        await conn.execute("DELETE FROM image_index   WHERE sha256 = ANY($1::text[])", shas)
        await conn.execute("DELETE FROM image_archive WHERE sha256 = ANY($1::text[])", shas)

    paths = [archive_root / r["sha256"][:2] / f"{r['sha256']}.{r['ext']}" for r in rows]
    try:
        freed = await STORAGE_IO.run(_unlink_all, paths, timeout_s=EXPORT_TIMEOUT_S)
    except (StorageBusyError, TimeoutError):
        # The rows are gone, so the files are only disk, and an orphan sweep
        # can remove them once the store answers.
        log.warning("retention: the archive store gave no answer; %d image files "
                    "outlive their rows", len(paths))
        freed = 0
    log.info("retention: pruned %d archived images (%.1f MB)", len(shas), freed / 1e6)
    return {"table": "image_archive", "rows": len(shas), "bytes": freed}


def _unlink_all(paths: list[Path]) -> int:
    """Remove each file, and return the bytes freed. Runs on the stores' pool."""
    freed = 0
    for path in paths:
        try:
            freed += path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("retention: could not unlink %s", path, exc_info=True)
    return freed


async def run_once(pool, *, settings, now: datetime | None = None) -> list[dict]:
    """One full sweep. Safe to call by hand; that's what the admin path does."""
    now = now or datetime.now(timezone.utc)
    results: list[dict] = []

    if settings.db_retention_days:
        cutoff = _cutoff(settings.db_retention_days, now)
        for fn in (offload_check_runs, offload_metric_samples):
            try:
                results.append(await fn(pool, settings.cold_root, cutoff,
                                        marker=settings.storage_marker))
            except Exception as e:
                # One table failing must not block the other, and must not
                # take the backend down — this runs unattended.
                log.exception("retention: %s failed", fn.__name__)
                results.append({"table": fn.__name__, "error": str(e)})

    if settings.archive_retention_days:
        cutoff = _cutoff(settings.archive_retention_days, now)
        try:
            results.append(await prune_archive(pool, settings.archive_root, cutoff,
                                               marker=settings.storage_marker))
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
