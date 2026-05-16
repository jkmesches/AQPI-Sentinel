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
import hashlib
import io
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)


def _sha_path(root: Path, sha: str, ext: str) -> Path:
    return root / sha[:2] / f"{sha}.{ext}"


def _detect_ext(content_type: str | None) -> str:
    if not content_type:
        return "png"
    if "png" in content_type: return "png"
    if "jpeg" in content_type or "jpg" in content_type: return "jpg"
    if "gif"  in content_type: return "gif"
    if "webp" in content_type: return "webp"
    return "bin"


async def save_image(pool, archive_root: Path, *, source: str, content: bytes,
                     content_type: str | None = None, origin_url: str | None = None) -> str | None:
    """Best-effort persist. Returns the sha256 on success, None on failure.

    Failures are swallowed (the caller is a live check that must keep
    running). Errors are logged at WARNING.
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
        path.parent.mkdir(parents=True, exist_ok=True)
        # Idempotent write — many checks will hash to the same sha and
        # we don't want to keep rewriting the file (also avoids partial
        # writes if two checks fire concurrently for the same content).
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(content)
            os.replace(tmp, path)

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
        if not path.exists():
            log.warning("image_index row exists but file missing: %s", path)
            return None
        ct = {"png": "image/png", "jpg": "image/jpeg",
              "gif": "image/gif", "webp": "image/webp"}.get(ext, "application/octet-stream")
        return path.read_bytes(), ct
    except Exception:
        log.exception("archive.lookup_by_source failed for %r", source)
        return None
