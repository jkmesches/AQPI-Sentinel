"""Retention offload — safety tests for the one code path that deletes data.

Unlike its neighbours in this directory, this test probes our own database
rather than radarca. It needs a Postgres it is allowed to DROP TABLE in, so
it refuses to run against anything but an explicitly-named scratch database.

Run against the `make dev` Postgres:

    createdb -h 127.0.0.1 -U sentinel sentinel_test
    SENTINEL_TEST_DSN=postgresql://sentinel:sentinel-dev@127.0.0.1:5432/sentinel_test \\
      python validation_tests/test_retention_offload.py

What it pins down, in order of how much it would hurt to get wrong:

  1. A short/failed export ABORTS before the delete. Retention exports rows to
     cold storage and then removes them; if the verification ever stops
     working, this is the test that catches it before we lose history.
  2. jsonb + text[] survive the CSV round-trip, including payloads containing
     commas, quotes and newlines — i.e. the export is genuinely restorable.
  3. Rows newer than the cutoff are untouched.
  4. An empty sweep is a clean no-op.
"""
from __future__ import annotations
import asyncio
import gzip
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import asyncpg                                        # noqa: E402
from backend import retention                         # noqa: E402

DSN = os.environ.get("SENTINEL_TEST_DSN")
if not DSN:
    sys.exit("SENTINEL_TEST_DSN is required — see this file's docstring. "
             "Point it at a scratch database; this test drops tables.")
if "sentinel_test" not in DSN:
    sys.exit(f"refusing to run against {DSN!r}: database name must contain "
             "'sentinel_test' so we can never drop a real one.")

SCHEMA = """
DROP TABLE IF EXISTS check_runs, metric_samples;
CREATE TABLE check_runs (
  id BIGSERIAL PRIMARY KEY, check_id TEXT NOT NULL, target TEXT NOT NULL,
  stage TEXT NOT NULL, status TEXT NOT NULL, started_at TIMESTAMPTZ NOT NULL,
  finished_at TIMESTAMPTZ NOT NULL, summary TEXT, payload JSONB, artifacts TEXT[]
);
CREATE TABLE metric_samples (
  ts TIMESTAMPTZ NOT NULL, check_id TEXT NOT NULL, target TEXT NOT NULL,
  metric TEXT NOT NULL, value DOUBLE PRECISION NOT NULL
);
"""

OLD_N, NEW_N = 500, 120
# Deliberately hostile to CSV: comma, double quote, embedded newline.
NASTY = {"note": 'has "quotes", commas\nand a newline', "n": 1}


async def _codecs(conn):
    await conn.set_type_codec(
        "jsonb", encoder=lambda v: json.dumps(v, default=str),
        decoder=json.loads, schema="pg_catalog",
    )


async def _seed(pool, now):
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
        for i in range(OLD_N):
            ts = now - timedelta(days=90, seconds=i)
            await c.execute(
                "INSERT INTO check_runs (check_id,target,stage,status,started_at,"
                "finished_at,summary,payload,artifacts) VALUES ($1,$2,$3,$4,$5,$5,$6,$7,$8)",
                f"check.{i % 7}", "t", "L1", "pass", ts, "old", NASTY, ["a", "b"])
            await c.execute("INSERT INTO metric_samples VALUES ($1,$2,$3,$4,$5)",
                            ts, f"check.{i % 7}", "t", "latency_ms", float(i))
        for i in range(NEW_N):
            ts = now - timedelta(days=3, seconds=i)
            await c.execute(
                "INSERT INTO check_runs (check_id,target,stage,status,started_at,"
                "finished_at,summary,payload,artifacts) VALUES ($1,$2,$3,$4,$5,$5,$6,$7,$8)",
                f"check.{i % 7}", "t", "L1", "pass", ts, "new", {"fresh": True}, None)
            await c.execute("INSERT INTO metric_samples VALUES ($1,$2,$3,$4,$5)",
                            ts, f"check.{i % 7}", "t", "latency_ms", float(i))


async def main() -> int:
    cold = Path(tempfile.mkdtemp(prefix="sentinel-cold-test-"))
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=5, init=_codecs)
    failures = 0
    try:
        now = datetime.now(timezone.utc)
        await _seed(pool, now)
        cutoff = now - timedelta(days=60)
        print(f"seeded {OLD_N} old + {NEW_N} recent rows; cutoff {cutoff:%Y-%m-%d}")

        r1 = await retention.offload_check_runs(pool, cold, cutoff)
        r2 = await retention.offload_metric_samples(pool, cold, cutoff)
        async with pool.acquire() as c:
            left_cr = await c.fetchval("SELECT count(*) FROM check_runs")
            left_ms = await c.fetchval("SELECT count(*) FROM metric_samples")
        assert r1["rows"] == OLD_N and r2["rows"] == OLD_N, (r1, r2)
        assert left_cr == NEW_N and left_ms == NEW_N, (left_cr, left_ms)
        print(f"  ok  offloaded {OLD_N}, preserved {NEW_N} recent rows")

        # The export must be restorable, not merely present.
        f = Path(r1["file"])
        with gzip.open(f, "rt") as fh:
            assert fh.readline().startswith("id,check_id"), "missing CSV header"
        async with pool.acquire() as c:
            await c.execute("CREATE TEMP TABLE restore_test (LIKE check_runs INCLUDING ALL)")
            with gzip.open(f, "rb") as fh:
                await c.copy_to_table("restore_test", source=fh, format="csv", header=True)
            n = await c.fetchval("SELECT count(*) FROM restore_test")
            row = await c.fetchrow("SELECT payload, artifacts FROM restore_test LIMIT 1")
        assert n == OLD_N, n
        assert row["payload"] == NASTY, row["payload"]
        assert row["artifacts"] == ["a", "b"], row["artifacts"]
        print(f"  ok  round-tripped {n} rows; jsonb + text[] intact through CSV")

        # The one that matters: verification failure must not delete.
        async with pool.acquire() as c:
            await c.execute(
                "UPDATE check_runs SET finished_at = $1 WHERE id IN "
                "(SELECT id FROM check_runs LIMIT 10)", now - timedelta(days=70))
            before = await c.fetchval("SELECT count(*) FROM check_runs")
        real = retention._export_query

        async def short_export(pool_, query, args, dest):
            await real(pool_, query, args, dest)
            return 999_999                      # COPY count disagrees

        retention._export_query = short_export
        try:
            await retention.offload_check_runs(pool, cold, cutoff)
            print("  FAIL  short export did not raise OffloadError")
            failures += 1
        except retention.OffloadError:
            async with pool.acquire() as c:
                after = await c.fetchval("SELECT count(*) FROM check_runs")
            assert after == before, f"rows deleted despite failed export: {before} → {after}"
            print(f"  ok  count mismatch aborted the delete; {after} rows intact")
        finally:
            retention._export_query = real

        res = await retention.offload_check_runs(pool, cold, now - timedelta(days=3650))
        assert res["rows"] == 0, res
        print("  ok  no-op when nothing is old enough")
    except AssertionError as e:
        print(f"  FAIL  {e}")
        failures += 1
    finally:
        await pool.close()
        shutil.rmtree(cold, ignore_errors=True)

    print("PASS" if not failures else f"FAILED ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
