"""Backup freshness — the check that makes a silent cron failure loud.

Run directly (no DB, no network):

    python validation_tests/test_backup_check.py

A cron backup fails silently by construction: it writes to a log nobody reads,
and the first sign that backups stopped is needing one. ops/backup.sh records
every run to status.json and this check reads it.

Two distinctions carry the whole value, and both are easy to collapse by
accident:

  - "not configured" must not look like "broken". A deployment that never
    mounted the backup directory should be `skip`, not `fail` — a check that
    fails on an unconfigured optional feature trains operators to ignore it,
    and then it is worthless on the day it means something.
  - "the last run failed" must outrank "the last run is old". A failure is
    happening now; staleness is only overdue. Reporting a failed run as merely
    stale understates it.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import backend.checks as _all                              # noqa: E402,F401
import backend.checks.layer0_selfhost as SH                # noqa: E402
from backend.checks.base import utcnow                     # noqa: E402
from backend.registry import CHECKS                        # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


async def run_with(status_file: Path | None, **fields):
    """Point the check at a temp status file containing `fields`."""
    original = SH.BACKUP_STATUS_PATH
    try:
        if status_file is not None and fields:
            status_file.write_text(json.dumps(fields))
        SH.BACKUP_STATUS_PATH = status_file if status_file else Path("/nonexistent/status.json")
        return await CHECKS["layer0.self.backup"].run(None)
    finally:
        SH.BACKUP_STATUS_PATH = original


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "status.json"
        ago = lambda h: (utcnow() - timedelta(hours=h)).isoformat()

        # --- not configured is NOT a failure ------------------------------
        r = await run_with(None)
        check("a missing status file is `skip`, not a failure", r.status == "skip", r.status)
        check("...and says how to configure it", "hint" in r.payload)
        check("...and is distinguishable from a real failure",
              r.payload.get("reason") == "no_status_file")

        # A corrupt file is also 'not usable', but must not crash the check.
        sf.write_text("{not json")
        SH_orig = SH.BACKUP_STATUS_PATH
        SH.BACKUP_STATUS_PATH = sf
        try:
            r = await CHECKS["layer0.self.backup"].run(None)
        finally:
            SH.BACKUP_STATUS_PATH = SH_orig
        check("a corrupt status file does not crash the check", r.status == "skip", r.status)

        # --- a fresh successful backup ------------------------------------
        r = await run_with(sf, status="ok", detail="wrote x", finished_at=ago(2), bytes=6255)
        check("a 2h-old successful backup passes", r.status == "pass", r.status)
        check("...and reports its age", r.metrics.get("backup_age_hours", 99) < 3,
              str(r.metrics.get("backup_age_hours")))

        # --- staleness escalates ------------------------------------------
        r = await run_with(sf, status="ok", detail="wrote x", finished_at=ago(40), bytes=6255)
        check("40h old warns (a daily run has been missed)", r.status == "warn", r.status)
        r = await run_with(sf, status="ok", detail="wrote x", finished_at=ago(80), bytes=6255)
        check("80h old fails", r.status == "fail", r.status)
        # The boundary itself must not be a coin flip.
        r = await run_with(sf, status="ok", finished_at=ago(SH.BACKUP_WARN_AGE_H - 1), bytes=1)
        check("just under the warn threshold still passes", r.status == "pass", r.status)

        # --- an outright failed run outranks staleness --------------------
        r = await run_with(sf, status="failed", detail="pg_dump failed",
                           finished_at=ago(0.1), bytes=0)
        check("a FAILED run fails immediately even when it just ran",
              r.status == "fail", r.status)
        check("...and surfaces why, not just that", "pg_dump failed" in r.summary, r.summary)

        # --- a malformed timestamp must not read as 'fresh' ---------------
        # Treating an unparseable date as age 0 would report broken backups
        # as healthy, which is the worst possible direction to fail in.
        r = await run_with(sf, status="ok", detail="x", finished_at="not-a-date", bytes=1)
        check("an unreadable timestamp warns rather than passing",
              r.status == "warn", r.status)

        # --- wiring -------------------------------------------------------
        c = CHECKS["layer0.self.backup"]
        check("does not depend on upstream (our backup, our problem)", not c.depends_on)
        check("cadence is cheap (reads one small file)", c.cadence_s >= 300, f"{c.cadence_s}s")
        check("warn threshold is below fail threshold",
              SH.BACKUP_WARN_AGE_H < SH.BACKUP_FAIL_AGE_H)

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall backup-check assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
