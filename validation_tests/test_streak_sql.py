"""The two hold-down streak queries, run against a real Postgres.

Run with a database that has the Sentinel schema:

    SENTINEL_TEST_DB_URL=postgresql://... python validation_tests/test_streak_sql.py

Without the variable it prints that it skipped and exits 0, because the other
alarm tests run against fake stores and cannot reach SQL at all. That gap is
how the first version of `broken_streak_start` shipped counting inconclusive
skips toward a reopen's hold-down, found by the red-team review on
2026-11-23: warn, then a long run of inconclusive skips, then one fail, and
the reopen paged on that first fail.

The rows go in under a check id no real check uses and are deleted in a
`finally`, so the script can run against the test deployment's database.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db.store import Store

failures: list[str] = []

# Each case: the runs, oldest first, as (status, inconclusive reason or None,
# minutes ago), and the minutes ago each query should start its run at.
CASES: tuple[tuple[str, list[tuple[str, str | None, int]], int | None, int | None], ...] = (
    ("warn, two inconclusive skips, fail",
     [("warn", None, 60), ("skip", "upstream_unhealthy", 50),
      ("skip", "source_out_of_window", 40), ("fail", None, 0)], 0, 60),
    ("warn, fail, inconclusive skip, fail",
     [("warn", None, 60), ("fail", None, 30), ("skip", "upstream_unhealthy", 20),
      ("fail", None, 0)], 30, 60),
    ("fail, pass, fail, error",
     [("fail", None, 60), ("pass", None, 50), ("fail", None, 10), ("error", None, 0)],
     10, 10),
    ("fail, then a warn", [("fail", None, 10), ("warn", None, 0)], None, 10),
    ("fail, intrinsic skip, fail",
     [("fail", None, 30), ("skip", None, 20), ("fail", None, 0)], 0, 0),
    ("nothing recorded", [], None, None),
)


def check(label: str, cond: bool, detail: str = "") -> None:
    """Print one assertion and record a failure."""
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def minutes_ago(started: datetime | None, now: datetime) -> int | None:
    """A run's start as whole minutes before `now`, or None."""
    return None if started is None else round((now - started).total_seconds() / 60)


async def run_cases(store: Store, prefix: str) -> None:
    """Insert each case's runs, ask both queries, and compare.

    Args:
        store: A connected store.
        prefix: The check id prefix the rows go in under.
    """
    if store.pool is None:
        raise RuntimeError("store is not connected")
    now = datetime.now(timezone.utc)
    for i, (label, runs, want_broken, want_non_pass) in enumerate(CASES):
        cid = f"{prefix}.{i}"
        for status, reason, ago in runs:
            # A dict, as the scheduler writes it. The store's jsonb codec
            # json.dumps whatever it is given, so a JSON string here would
            # land as a string literal with no reason key in it.
            payload = {"reason": reason} if reason else {}
            at = now - timedelta(minutes=ago)
            _ = await store.pool.execute(
                "INSERT INTO check_runs (check_id, target, stage, status, started_at,"
                " finished_at, payload) VALUES ($1, 't', 'LB1', $2, $3, $3, $4)",
                cid, status, at, payload)
        broken = minutes_ago(await store.broken_streak_start(cid, "t"), now)
        non_pass = minutes_ago(await store.non_pass_streak_start(cid, "t"), now)
        check(f"{label}: fail run starts {want_broken} min ago", broken == want_broken,
              str(broken))
        check(f"{label}: non-pass run starts {want_non_pass} min ago",
              non_pass == want_non_pass, str(non_pass))


async def main() -> int:
    """Run every case against the database, or skip without one.

    Returns:
        The process exit status, 1 when any assertion failed.
    """
    dsn = os.environ.get("SENTINEL_TEST_DB_URL")
    if not dsn:
        print("skipped: set SENTINEL_TEST_DB_URL to a Postgres with the Sentinel schema")
        return 0
    store = Store(dsn)
    await asyncio.wait_for(store.connect(), 30)
    prefix = f"zz.streak-test.{uuid.uuid4().hex[:8]}"
    try:
        await run_cases(store, prefix)
    finally:
        if store.pool is not None:
            _ = await store.pool.execute(
                "DELETE FROM check_runs WHERE check_id LIKE $1", f"{prefix}.%")
            await store.pool.close()
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall streak-query assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
