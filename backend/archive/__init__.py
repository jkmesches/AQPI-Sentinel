"""Image archive — persist L4 captures on disk + index in Postgres.

Filesystem layout: <SETTINGS.archive_root>/<sha[:2]>/<sha>.<ext>
DB indexes:
  image_archive       sha256 → metadata (size, dims, first/last seen, origin)
  image_index         source → sha256 (lets the frontend re-fetch by the same
                      `source` string the check captured at run time)

Dedup is automatic via sha256 PK. The same upstream PNG re-fetched many
times (forecast products idle for an hour between updates, X-band radars
sit on a steady clutter pattern, etc.) ends up as one file on disk plus
one image_archive row, with potentially many image_index rows pointing
at that sha.
"""
from __future__ import annotations
import contextlib
import hashlib
import io
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from PIL import Image

from backend.storage_io import (STORAGE_IO, StorageBusyError, StorageNotMountedError,
                                require_marker)

log = logging.getLogger(__name__)

# A frame is about 100 kB, so a write that takes 30 s is a stalled share, and
# the capture goes on without it. A read serves a page, which falls back to
# radarca sooner.
WRITE_TIMEOUT_S = 30.0
READ_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Observation:
    """What a captured frame shows: its product and the valid time it draws.

    Attributes:
        product_id: The product, as config.PRODUCTS names it.
        scan_ts: The valid time of the step the frame draws, UTC.
        published_at: The frame file's mtime on the mount, or None when the
            frame came over HTTP.
    """

    product_id: str
    scan_ts: datetime
    published_at: datetime | None = None


# A repeat capture of the same frame moves last_seen_at and fills in a
# published_at or source the row lacks, which a backfilled row does. A later
# cycle that republishes identical bytes for the same valid time therefore
# keeps the first cycle's published_at, which is right because identical
# bytes are the same forecast.
_OBSERVATION_SQL = """
    INSERT INTO image_observations
        (product_id, scan_ts, sha256, source, published_at, first_seen_at, last_seen_at)
    VALUES ($1, $2, $3, $4, $5, $6, $6)
    ON CONFLICT (product_id, scan_ts, sha256) DO UPDATE
        SET last_seen_at = EXCLUDED.last_seen_at,
            published_at = COALESCE(image_observations.published_at,
                                    EXCLUDED.published_at),
            source       = COALESCE(image_observations.source, EXCLUDED.source)
"""


async def touch_observation(pool: asyncpg.Pool, obs: Observation, sha256: str) -> None:
    """Move a frame's last_seen_at to now, for a run that saw it unchanged.

    Moves image_archive's as well, which a read would have, since archive
    retention prunes on it. Best effort, like save_image: a failure is logged
    and the run goes on.

    Args:
        pool: The store's pool.
        obs: The frame's product and valid time.
        sha256: Its bytes' hash, from the read that archived it.
    """
    try:
        await pool.execute(
            "WITH archived AS (UPDATE image_archive SET last_seen_at = now() "
            "WHERE sha256 = $3) "
            "UPDATE image_observations SET last_seen_at = now() "
            "WHERE product_id = $1 AND scan_ts = $2 AND sha256 = $3",
            obs.product_id, obs.scan_ts, sha256)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError) as e:
        log.warning("archive: last_seen_at not moved for %s %s: %s",
                    obs.product_id, obs.scan_ts, e)


def _sha_path(root: Path, sha: str, ext: str) -> Path:
    return root / sha[:2] / f"{sha}.{ext}"


def _write_blob(root: Path, path: Path, content: bytes, marker: str | None) -> None:
    """Write one content-addressed blob unless it is already whole. Runs on the stores' pool.

    Many checks hash to the same sha, so a file of the right size is left
    alone, and one of another size (cut short by a crash) is written again.
    Each call writes a temporary file of its own and renames it into place,
    so the file at `path` is always complete. The marker test comes first,
    so nothing is created on a share that is not attached.

    Args:
        root: The archive's root.
        path: Where the blob goes, under `root`.
        content: The blob.
        marker: SETTINGS.storage_marker, or None for no test.

    Raises:
        StorageNotMountedError: When `root` lacks the marker.
        OSError: When the store refuses the write.
    """
    require_marker(root, marker)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size == len(content):
            return
    except FileNotFoundError:
        pass
    # A shared name let a second writer (another check, or a retry while a
    # timed-out call still ran on a stalled share) truncate the file the first
    # was about to rename, which published an empty frame for good.
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_bytes(content)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):  # the write's own error is re-raised below
            tmp.unlink(missing_ok=True)
        raise


def _read_blob(path: Path) -> bytes | None:
    """Read one blob, or None if it isn't there. Runs in a thread."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _detect_ext(content_type: str | None) -> str:
    if not content_type:
        return "png"
    if "png" in content_type: return "png"
    if "jpeg" in content_type or "jpg" in content_type: return "jpg"
    if "gif"  in content_type: return "gif"
    if "webp" in content_type: return "webp"
    return "bin"


async def save_image(pool, archive_root: Path, *, source: str, content: bytes,
                     marker: str | None,
                     content_type: str | None = None, origin_url: str | None = None,
                     observation: Observation | None = None) -> str | None:
    """Best-effort persist. Returns the sha256 on success, None on failure.

    Failures are swallowed (the caller is a live check that must keep
    running). Errors are logged at WARNING. With an observation, the frame's
    image_observations row is written in the same transaction as its
    image_archive row, which the row's foreign key needs. `marker` is
    SETTINGS.storage_marker, required of every caller so none can skip the
    test that the archive's share is attached.
    """
    try:
        sha = hashlib.sha256(content).hexdigest()
        ext = _detect_ext(content_type)
        # Dimensions are cheap to extract from PNG headers and useful for
        # later querying ("show me only large mosaic captures, not the
        # 5KB blank ones").
        width = height = None
        try:
            with Image.open(io.BytesIO(content)) as im:
                width, height = im.size
        except Exception:
            pass

        path = _sha_path(archive_root, sha, ext)
        # File I/O goes through the stores' own pool. The archive root may be
        # an NFS bind mount mounted `hard`, which blocks I/O while the server
        # is unreachable instead of returning an error. Inline on the event
        # loop, one NAS reboot froze every check, the API and the WebSocket.
        # On asyncio's default pool it held threads the disk check and the
        # K2 readers need. See backend/storage_io.py.
        try:
            await STORAGE_IO.run(_write_blob, archive_root, path, content, marker,
                                 timeout_s=WRITE_TIMEOUT_S)
        except (StorageBusyError, TimeoutError) as e:
            log.warning("archive: not saved, the archive store gave no answer (%s): %r",
                        str(e) or "timed out", source)
            return None
        except StorageNotMountedError as e:
            log.warning("archive: not saved: %s", e)
            return None

        now = datetime.now(timezone.utc)
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO image_archive
                        (sha256, ext, size_bytes, width, height,
                         first_seen_at, last_seen_at, origin_url)
                    VALUES ($1,$2,$3,$4,$5,$6,$6,$7)
                    ON CONFLICT (sha256) DO UPDATE
                        SET last_seen_at = EXCLUDED.last_seen_at
                    """,
                    sha, ext, len(content), width, height, now, origin_url,
                )
                await conn.execute(
                    """
                    INSERT INTO image_index (source, sha256, first_seen_at, last_seen_at)
                    VALUES ($1, $2, $3, $3)
                    ON CONFLICT (source) DO UPDATE
                        SET last_seen_at = EXCLUDED.last_seen_at,
                            sha256       = EXCLUDED.sha256
                    """,
                    source, sha, now,
                )
                if observation is not None:
                    await conn.execute(
                        _OBSERVATION_SQL, observation.product_id, observation.scan_ts,
                        sha, source, observation.published_at, now)
        return sha
    except Exception:
        log.exception("archive.save_image failed for source=%r", source)
        return None


async def lookup_by_source(pool, archive_root: Path, source: str) -> tuple[bytes, str] | None:
    """Return (bytes, content_type) if we've previously archived this
    source path; else None."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT i.sha256, a.ext FROM image_index i "
                "JOIN image_archive a USING (sha256) WHERE i.source = $1",
                source,
            )
        if row is None:
            return None
        sha, ext = row["sha256"], row["ext"]
        path = _sha_path(archive_root, sha, ext)
        # On the stores' pool for the same reason as the write path. This
        # serves the History detail modal's images straight off the mount, and
        # a read that gives no answer falls back to radarca.
        try:
            data = await STORAGE_IO.run(_read_blob, path, timeout_s=READ_TIMEOUT_S)
        except (StorageBusyError, TimeoutError) as e:
            log.warning("archive: read of %s gave no answer (%s)", path, str(e) or "timed out")
            return None
        if data is None:
            log.warning("image_index row exists but file missing: %s", path)
            return None
        ct = {"png": "image/png", "jpg": "image/jpeg",
              "gif": "image/gif", "webp": "image/webp"}.get(ext, "application/octet-stream")
        return data, ct
    except Exception:
        log.exception("archive.lookup_by_source failed for %r", source)
        return None
