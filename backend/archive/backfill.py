"""Observation rows for frames archived before captures wrote them.

Until 2026-11-24 a capture wrote image_archive and image_index and nothing
else, and image_index keeps only the newest sha256 for a source, so the
archive could not say which valid time a frame drew. image_archive's
origin_url still names the file, set when its bytes were first seen, and a
CoSMoS file name is its valid time (water_depth/images/20261124_0300.png).
This module turns those into image_observations rows, one per archived image.

A backfilled row has no published_at (no mtime was kept) and takes
first_seen_at and last_seen_at from image_archive. Bytes first seen under
another name keep that name's valid time, since origin_url records only the
first.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

log = logging.getLogger(__name__)

# The CoSMoS products a capture archived before 2026-11-24. The other three
# were first captured that day, with observation rows from the start.
BACKFILL_PRODUCTS: tuple[str, ...] = ("water_depth",)
# The settings key prefix that marks a product's backfill as done.
BACKFILL_KEY = "archive.backfill_observations."

# `file=` then the product's own images folder and a UTC valid time. Anchored
# at both ends of the file part, so max_water_depth is not read as water_depth.
_NAME_RE = r"[?&]file=(?P<source>{product}/images/(?P<when>\d{{8}}_\d{{4}})\.png)$"

_INSERT_SQL = """
    INSERT INTO image_observations
        (product_id, scan_ts, sha256, source, first_seen_at, last_seen_at)
    SELECT $1, scan_ts, sha256, source, first_seen_at, last_seen_at
    FROM unnest($2::timestamptz[], $3::text[], $4::text[],
                $5::timestamptz[], $6::timestamptz[])
        AS t(scan_ts, sha256, source, first_seen_at, last_seen_at)
    ON CONFLICT (product_id, scan_ts, sha256) DO NOTHING
"""


class Pool(Protocol):
    """The two asyncpg pool methods the backfill uses."""

    async def fetch(self, query: str, *args: object) -> list[Mapping[str, object]]:
        """Run a query and return its rows."""

    async def execute(self, query: str, *args: object) -> str:
        """Run a statement and return its status line."""


@dataclass
class Batch:
    """Rows to insert, column by column, as unnest() takes them."""

    scan_ts: list[datetime] = field(default_factory=list)
    sha256: list[str] = field(default_factory=list)
    source: list[str] = field(default_factory=list)
    first_seen_at: list[datetime] = field(default_factory=list)
    last_seen_at: list[datetime] = field(default_factory=list)


def parse_origin(product_id: str, origin_url: str) -> tuple[str, datetime] | None:
    """The source path and valid time an origin_url names, for one product.

    Args:
        product_id: The product whose frames to read, e.g. water_depth.
        origin_url: An image_archive.origin_url.

    Returns:
        The source path and its UTC valid time, or None when the URL is not
        one of this product's frames or its time is not a real one.
    """
    m = re.search(_NAME_RE.format(product=re.escape(product_id)), origin_url)
    if m is None:
        return None
    try:
        when = datetime.strptime(m["when"], "%Y%m%d_%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return m["source"], when


def to_batch(product_id: str, rows: Iterable[Mapping[str, object]]) -> Batch:
    """Columns for the rows that name one of the product's frames.

    Args:
        product_id: The product.
        rows: image_archive rows with sha256, origin_url, first_seen_at and
            last_seen_at.

    Returns:
        The parsed rows, column by column. Rows that do not parse are left out.
    """
    batch = Batch()
    for row in rows:
        url, sha = row.get("origin_url"), row.get("sha256")
        first, last = row.get("first_seen_at"), row.get("last_seen_at")
        if not (isinstance(url, str) and isinstance(sha, str)
                and isinstance(first, datetime) and isinstance(last, datetime)):
            continue
        parsed = parse_origin(product_id, url)
        if parsed is None:
            continue
        batch.scan_ts.append(parsed[1])
        batch.sha256.append(sha)
        batch.source.append(parsed[0])
        batch.first_seen_at.append(first)
        batch.last_seen_at.append(last)
    return batch


async def backfill_observations(pool: Pool, product_id: str) -> int:
    """Write an observation row for each archived frame of one product, once.

    The scan of image_archive takes seconds (3.2 M rows on the test
    deployment), so a marker in `settings` records that it ran, and later
    starts skip it. Rows already present are left as they are, so running it
    twice is harmless as well.

    Args:
        pool: An asyncpg pool.
        product_id: The product, e.g. water_depth.

    Returns:
        How many rows it inserted, 0 when it had run before.

    Raises:
        ValueError: If the product id has characters a file name would not.
    """
    if not re.fullmatch(r"[a-z0-9_]+", product_id):
        raise ValueError(f"not a product id: {product_id!r}")
    marker = BACKFILL_KEY + product_id
    if await pool.fetch("SELECT 1 FROM settings WHERE key = $1", marker):
        return 0
    # LIKE treats _ as a wildcard; the regex in parse_origin is the real test.
    pattern = "%file=" + product_id.replace("_", r"\_") + "/images/%"
    rows = await pool.fetch(
        "SELECT sha256, origin_url, first_seen_at, last_seen_at "
        "FROM image_archive WHERE origin_url LIKE $1", pattern)
    batch = to_batch(product_id, rows)
    inserted = 0
    if batch.sha256:
        status = await pool.execute(
            _INSERT_SQL, product_id, batch.scan_ts, batch.sha256, batch.source,
            batch.first_seen_at, batch.last_seen_at)
        # asyncpg returns "INSERT 0 <rows>".
        inserted = int(status.rsplit(" ", 1)[-1])
    # Built in SQL rather than passed as JSON, so the jsonb codec on the
    # store's pool cannot encode it twice.
    _ = await pool.execute(
        "INSERT INTO settings (key, value, updated_at, updated_by) "
        "VALUES ($1, jsonb_build_object('rows', $2::int, 'archived', $3::int), now(), "
        "'system:backfill') ON CONFLICT (key) DO NOTHING",
        marker, inserted, len(batch.sha256))
    log.info("backfilled %d of %d archived %s frames into image_observations",
             inserted, len(batch.sha256), product_id)
    return inserted
