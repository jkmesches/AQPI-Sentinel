"""image_observations in Postgres: the reshape, the upsert rules and the backfill.

    SENTINEL_TEST_DB_URL=postgresql://... python validation_tests/test_frame_observations.py

Without the variable it prints that it skipped and exits 0. It works in
throwaway schemas it creates and drops, so the database may hold other data.

What it holds the code to, from plan 80 §1:

  1. schema.sql brings a fresh schema, and one with the table's first shape,
     to the same columns and keys, and running it again changes nothing.
  2. A repeat capture of the same frame moves only last_seen_at.
  3. A later cycle that republishes a valid time with new bytes adds a row,
     so both forecasts stay findable.
  4. One that republishes identical bytes keeps the first cycle's
     published_at.
  5. The backfill writes one row per archived frame of the product, none for
     another product's frames, and nothing more when run again.
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import asyncpg  # noqa: E402
from PIL import Image  # noqa: E402

from backend import retention  # noqa: E402
from backend.archive import (Observation, lookup_by_source, save_image,  # noqa: E402
                             touch_observation)
from backend.archive.backfill import backfill_observations  # noqa: E402
from backend.db.store import Store  # noqa: E402
from backend.retention import prune_archive  # noqa: E402

SCHEMA_SQL = (ROOT / "backend" / "db" / "schema.sql").read_text()
# The table as it was until 2026-11-24, which a deployed database still has.
OLD_SHAPE = """
CREATE TABLE image_archive (
  sha256 TEXT PRIMARY KEY, ext TEXT NOT NULL, size_bytes INTEGER NOT NULL,
  width INTEGER, height INTEGER, first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at TIMESTAMPTZ NOT NULL, origin_url TEXT);
CREATE TABLE image_observations (
  ts TIMESTAMPTZ NOT NULL, product_id TEXT NOT NULL, scan_ts TIMESTAMPTZ NOT NULL,
  sha256 TEXT NOT NULL REFERENCES image_archive(sha256),
  PRIMARY KEY (product_id, scan_ts));
CREATE INDEX idx_obs_recent ON image_observations(product_id, ts DESC);
"""
TIMEOUT_S = 60
failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    """Print one assertion and record a failure."""
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def png(seed: int) -> bytes:
    """Draw a small PNG whose pixels depend on a seed.

    Args:
        seed: Any small integer; two seeds give two different files.

    Returns:
        The PNG's bytes.
    """
    im = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    im.putpixel((seed % 32, 1), (200, 10, 10, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


async def shape(conn: asyncpg.Connection) -> dict[str, Any]:
    """Read the table's columns, primary key and unique indexes in the current schema.

    Args:
        conn: A connection whose search_path is the schema to read.

    Returns:
        The columns with their nullability, the count of old primary keys,
        and the unique index definitions.
    """
    cols = await conn.fetch(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'image_observations' "
        "ORDER BY column_name")
    pkey = await conn.fetchval(
        "SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE n.nspname = current_schema() AND c.conname = 'image_observations_pkey'")
    uniq = await conn.fetch(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
        "AND tablename = 'image_observations' AND indexdef LIKE 'CREATE UNIQUE%'")
    return {"cols": [(r["column_name"], r["is_nullable"]) for r in cols],
            "pkey": pkey, "unique": sorted(r["indexdef"].split(" ON ")[1] for r in uniq)}


async def fresh_schema(conn: asyncpg.Connection, name: str, *, old: bool) -> None:
    """Create and enter a throwaway schema, optionally with the table's first shape."""
    await conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE; CREATE SCHEMA {name}; "
                       f"SET search_path TO {name}")
    if old:
        await conn.execute(OLD_SHAPE)


async def test_reshape(conn: asyncpg.Connection) -> None:
    """Fresh and upgraded schemas end alike, and a second run changes nothing."""
    print("schema:")
    await fresh_schema(conn, "obs_fresh", old=False)
    await conn.execute(SCHEMA_SQL)
    fresh = await shape(conn)
    await conn.execute(SCHEMA_SQL)
    check("a second run on a fresh schema changes nothing", await shape(conn) == fresh)
    await fresh_schema(conn, "obs_upgrade", old=True)
    await conn.execute(SCHEMA_SQL)
    upgraded = await shape(conn)
    await conn.execute(SCHEMA_SQL)
    check("a second run on an upgraded schema changes nothing", await shape(conn) == upgraded)
    names = {c for c, _ in fresh["cols"]}
    check("the columns are the planned ones", names == {
        "first_seen_at", "last_seen_at", "product_id", "scan_ts", "sha256", "source",
        "published_at"}, str(sorted(names)))
    check("an upgraded schema has the same columns as a fresh one",
          {c for c, _ in upgraded["cols"]} == names, str(upgraded["cols"]))
    check("neither keeps the old primary key", fresh["pkey"] == 0 and upgraded["pkey"] == 0)
    want = ["public.image_observations USING btree (product_id, scan_ts, sha256)"]
    check("both are unique on (product_id, scan_ts, sha256)",
          [u.replace("obs_fresh.", "public.") for u in fresh["unique"]] == want
          and [u.replace("obs_upgrade.", "public.") for u in upgraded["unique"]] == want,
          f"{fresh['unique']} / {upgraded['unique']}")


async def test_upsert(pool: asyncpg.Pool, root: Path) -> None:
    """The three capture rules, through save_image."""
    print("\ncapture rows:")
    t = datetime(2026, 11, 24, 3, tzinfo=timezone.utc)
    p1, p2 = t - timedelta(hours=2), t - timedelta(hours=1)
    src = "water_level/images/20261124_0300.png"

    async def capture(content: bytes, published: datetime | None) -> None:
        _ = await save_image(pool, root, source=src, content=content, marker=None,
                             content_type="image/png", origin_url="https://x/?file=" + src,
                             observation=Observation("water_level", t, published))

    async def rows() -> list[asyncpg.Record]:
        got: list[asyncpg.Record] = await pool.fetch(
            "SELECT sha256, published_at, first_seen_at, last_seen_at FROM image_observations "
            "WHERE product_id = 'water_level' AND scan_ts = $1 ORDER BY first_seen_at", t)
        return got

    await capture(png(1), p1)
    first = await rows()
    check("a capture writes one row", len(first) == 1, str(first))
    check("...with first and last seen equal",
          bool(first) and first[0]["first_seen_at"] == first[0]["last_seen_at"])
    await asyncio.sleep(0.01)
    await capture(png(1), p1)
    again = await rows()
    check("the same frame again adds no row", len(again) == 1, str(again))
    check("...and moves only last_seen_at", len(again) == 1
          and again[0]["first_seen_at"] == first[0]["first_seen_at"]
          and again[0]["last_seen_at"] > first[0]["last_seen_at"]
          and again[0]["published_at"] == p1)
    await capture(png(1), p2)
    same = await rows()
    check("identical bytes from a later cycle keep the first published_at",
          len(same) == 1 and same[0]["published_at"] == p1, str(same))
    await capture(png(2), p2)
    both = await rows()
    check("new bytes for the same valid time add a row", len(both) == 2, str(both))
    check("...with their own published_at", len(both) == 2 and both[1]["published_at"] == p2)
    before = await pool.fetchval("SELECT count(*) FROM image_observations")
    _ = await save_image(pool, root, source="comp_ref/x.png", content=png(3), marker=None,
                         content_type="image/png")
    check("a capture without an observation writes none",
          await pool.fetchval("SELECT count(*) FROM image_observations") == before)


async def test_backfill(pool: asyncpg.Pool) -> None:
    """One row per archived frame of the product, once."""
    print("\nbackfill:")
    now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
    names = {"wd1": "water_depth/images/20261124_0300.png",
             "wd2": "water_depth/images/20261124_0400.png",
             "mx": "max_water_depth/images/20261124_0000.png",
             "bad": "water_depth/images/20261199_0300.png"}
    for sha, name in names.items():
        await pool.execute(
            "INSERT INTO image_archive VALUES ($1, 'png', 1, 1, 1, $2, $3, $4)",
            sha, now, now + timedelta(hours=1), "https://x/api/imageData?file=" + name)
    n = await backfill_observations(pool, "water_depth")
    check("it inserts the product's two good frames", n == 2, str(n))
    got = await pool.fetch("SELECT sha256, scan_ts, published_at, last_seen_at "
                           "FROM image_observations WHERE product_id = 'water_depth' "
                           "ORDER BY scan_ts")
    check("...at their valid times", [r["scan_ts"].hour for r in got] == [3, 4], str(got))
    check("...with no published_at and the archive's last_seen_at",
          all(r["published_at"] is None and r["last_seen_at"] == now + timedelta(hours=1)
              for r in got))
    check("max_water_depth's frame is not water_depth's", all(r["sha256"] != "mx" for r in got))
    check("a second run inserts nothing", await backfill_observations(pool, "water_depth") == 0)
    await pool.execute(
        "INSERT INTO image_archive VALUES ('wd3', 'png', 1, 1, 1, $1, $1, $2)",
        now, "https://x/api/imageData?file=water_depth/images/20261124_0500.png")
    check("a later start skips the scan, by its marker",
          await backfill_observations(pool, "water_depth") == 0)
    check("...which is in settings", await pool.fetchval(
        "SELECT value->>'rows' FROM settings "
        "WHERE key = 'archive.backfill_observations.water_depth'") == "2")
    try:
        _ = await backfill_observations(pool, "water_depth/../x")
        check("a product id with a path in it is refused", False)
    except ValueError:
        check("a product id with a path in it is refused", True)


async def test_after_backfill(pool: asyncpg.Pool, root: Path) -> None:
    """A capture fills in a backfilled row; a touch and a prune behave."""
    print("\nafter the backfill:")
    t = datetime(2026, 11, 24, 3, tzinfo=timezone.utc)
    content = png(7)
    sha = await save_image(pool, root, source="water_depth/images/20261124_0300.png",
                           content=content, marker=None, content_type="image/png")
    await pool.execute(
        "INSERT INTO image_observations (product_id, scan_ts, sha256, first_seen_at, "
        "last_seen_at) VALUES ('water_depth', $1, $2, now(), now())", t, sha)
    published = t - timedelta(hours=3)
    _ = await save_image(pool, root, source="water_depth/images/20261124_0300.png",
                         content=content, marker=None, content_type="image/png",
                         observation=Observation("water_depth", t, published))
    row = await pool.fetchrow("SELECT published_at, source, last_seen_at FROM "
                              "image_observations WHERE sha256 = $1", sha)
    check("a capture fills in a backfilled row's published_at and source",
          row is not None and row["published_at"] == published
          and row["source"] == "water_depth/images/20261124_0300.png", str(row))
    await pool.execute("UPDATE image_observations SET last_seen_at = $1 WHERE sha256 = $2; ",
                       t, sha)
    await pool.execute("UPDATE image_archive SET last_seen_at = $1 WHERE sha256 = $2", t, sha)
    await touch_observation(pool, Observation("water_depth", t), str(sha))
    seen = await pool.fetchrow(
        "SELECT o.last_seen_at AS obs, a.last_seen_at AS arc FROM image_observations o "
        "JOIN image_archive a USING (sha256) WHERE o.sha256 = $1", sha)
    check("a touch moves both last_seen_at columns", seen is not None
          and seen["obs"] > t and seen["arc"] > t, str(seen))
    before = await pool.fetchval("SELECT count(*) FROM image_observations")
    result = await prune_archive(pool, root, datetime.now(timezone.utc) + timedelta(days=1),
                                 marker=None)
    check("retention prunes archive rows that observations point at", result["rows"] > 0
          and await pool.fetchval("SELECT count(*) FROM image_observations") == 0,
          f"{result}, {before} observation rows before")


async def test_prune_races(pool: asyncpg.Pool, root: Path) -> None:
    """A prune keeps a frame seen again during it, and its late unlinks stop.

    Args:
        pool: The pool, in a schema whose archive tables the prune may empty.
        root: The archive's root.
    """
    print("\nprune races:")
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    async def stale_frame(seed: int, source: str) -> str:
        sha = await save_image(pool, root, source=source, content=png(seed), marker=None,
                               content_type="image/png")
        await pool.execute("UPDATE image_archive SET last_seen_at = $1 WHERE sha256 = $2",
                           cutoff - timedelta(days=30), sha)
        return str(sha)

    def frame(sha: str) -> Path:
        return root / sha[:2] / f"{sha}.png"

    sha = await stale_frame(21, "races/a.png")

    async def seen_again(store: Path, marker: str | None) -> None:
        await pool.execute("UPDATE image_archive SET last_seen_at = now() WHERE sha256 = $1",
                           sha)

    with mock.patch.object(retention, "_require_store", seen_again):
        result = await prune_archive(pool, root, cutoff, marker=None)
    kept = await pool.fetchval("SELECT count(*) FROM image_archive WHERE sha256 = $1", sha)
    check("a frame seen while the prune tests its store keeps its row and file",
          kept == 1 and frame(sha).exists(), f"{result}, {kept} rows")

    # The share answers only after the prune's unlink call has timed out, and
    # the frame is saved again in between.
    sha = await stale_frame(22, "races/b.png")
    answer, done = threading.Event(), threading.Event()
    real_unlink = retention._unlink_all

    def late(paths: list[Path], stop: threading.Event) -> int:
        _ = answer.wait(5)
        try:
            return real_unlink(paths, stop)
        finally:
            done.set()

    with mock.patch.object(retention, "_unlink_all", late), \
            mock.patch.object(retention, "FILE_TIMEOUT_S", 0.5):
        result = await prune_archive(pool, root, cutoff, marker=None)
    again = await save_image(pool, root, source="races/b.png", content=png(22), marker=None,
                             content_type="image/png")
    answer.set()
    finished = await asyncio.to_thread(done.wait, 5)
    found = await lookup_by_source(pool, root, "races/b.png")
    check("a frame saved again after a prune's unlinks timed out keeps its file",
          result["rows"] == 1 and again == sha and finished and frame(sha).exists()
          and found is not None, f"{result}, finished {finished}")

    sha = await stale_frame(23, "races/c.png")
    rows = await pool.fetch("SELECT sha256, ext FROM image_archive WHERE sha256 = $1", sha)
    freed = await retention._unlink_unclaimed(pool, root, list(rows))
    check("a prune's unlinks skip a frame whose row is back",
          freed == 0 and frame(sha).exists(), f"freed {freed}")
    await pool.execute("DELETE FROM image_index WHERE sha256 = $1", sha)
    await pool.execute("DELETE FROM image_archive WHERE sha256 = $1", sha)
    freed = await retention._unlink_unclaimed(pool, root, list(rows))
    check("...and remove one whose row is gone", freed > 0 and not frame(sha).exists(),
          f"freed {freed}")


async def test_trial_starts(pool: asyncpg.Pool) -> None:
    """A stored trial start wins over check_runs, and is never replaced."""
    print("\ntrial starts:")
    st = Store("postgresql://unused/unused")
    st.pool = pool
    t0 = datetime(2026, 11, 24, 3, tzinfo=timezone.utc)
    for cid, at in (("chk.a", t0), ("chk.a", t0 + timedelta(hours=1)),
                    ("chk.b", t0 + timedelta(days=2))):
        await pool.execute(
            "INSERT INTO check_runs (check_id, target, stage, status, started_at, finished_at, "
            "summary) VALUES ($1, 'x', 'L4-T1T2', 'pass', $2, $2, '')", cid, at)
    got = await st.first_runs({"chk.a", "chk.b", "chk.none"})
    check("with nothing stored, the earliest run is the start",
          got == {"chk.a": t0, "chk.b": t0 + timedelta(days=2)}, str(got))
    await st.save_first_run("chk.b", t0 - timedelta(days=30))
    await st.save_first_run("chk.b", t0)
    got = await st.first_runs({"chk.b"})
    check("a stored start wins over check_runs, and the first one stored stays",
          got == {"chk.b": t0 - timedelta(days=30)}, str(got))
    await pool.execute("DELETE FROM check_runs WHERE check_id = 'chk.b'")
    check("...and survives retention trimming check_runs",
          await st.first_runs({"chk.b"}) == {"chk.b": t0 - timedelta(days=30)})


async def run(dsn: str) -> None:
    """Run every section, in throwaway schemas.

    Args:
        dsn: The Postgres to run in.
    """
    conn = await asyncio.wait_for(asyncpg.connect(dsn), TIMEOUT_S)
    try:
        await test_reshape(conn)
    finally:
        await conn.close()
    pool = await asyncio.wait_for(asyncpg.create_pool(
        dsn, min_size=1, max_size=2,
        server_settings={"search_path": "obs_upgrade"}), TIMEOUT_S)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            await asyncio.wait_for(test_upsert(pool, Path(tmp)), TIMEOUT_S)
            await asyncio.wait_for(test_backfill(pool), TIMEOUT_S)
            await asyncio.wait_for(test_after_backfill(pool, Path(tmp)), TIMEOUT_S)
            await asyncio.wait_for(test_prune_races(pool, Path(tmp)), TIMEOUT_S)
        await asyncio.wait_for(test_trial_starts(pool), TIMEOUT_S)
        await pool.execute("DROP SCHEMA obs_fresh CASCADE; DROP SCHEMA obs_upgrade CASCADE")
    finally:
        await pool.close()


def main() -> int:
    """Run every section against the database, or skip without one.

    Returns:
        The process exit status, 1 when any assertion failed.
    """
    dsn = os.environ.get("SENTINEL_TEST_DB_URL")
    if not dsn:
        print("skipped: set SENTINEL_TEST_DB_URL to a Postgres this test may create schemas in")
        return 0
    asyncio.run(run(dsn))
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall observation assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
