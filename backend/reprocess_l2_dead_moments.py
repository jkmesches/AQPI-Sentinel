"""One-shot reprocessor for Layer 2 historical dead_moments payloads.

Removes moments listed in EXPECTED_ABSENT_MOMENTS (e.g. "RhoHV" on X-band
radars, which the upstream simply doesn't publish) from each historical
row's `dead_moments` array, and rewrites the summary if it embedded the
old list. Does NOT change the run's status — `dead_moments` was
informational only, so re-running the verdict math isn't needed.

Why: the original L2 check appended every empty moment to `dead_moments`
without consulting an exclusion list. Memory file
`reference_radarca_upstream.md` notes that X-band radars publish no
RhoHV imagery at all, so on every healthy X-band run `dead_moments`
contained `["RhoHV"]`. This populates a misleading historical record
and would false-trip any future alert condition that fires on a
non-empty `dead_moments` field.

This script does NOT reprocess the SILENT_FAIL_S logic introduced in
the same commit — the historical payload doesn't preserve image
filenames, so we can't retroactively decide whether obs was "fresh"
vs "stale window." That fix is forward-only.

Usage:
    python -m backend.reprocess_l2_dead_moments              # update
    python -m backend.reprocess_l2_dead_moments --dry-run    # report only
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
from collections import Counter
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

from backend.config import SETTINGS  # noqa: E402
from backend.checks.layer2_radar import EXPECTED_ABSENT_MOMENTS  # noqa: E402

log = logging.getLogger("reprocess_l2_dead_moments")


async def main(dry_run: bool) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    conn = await asyncpg.connect(SETTINGS.db_url)

    rows = await conn.fetch(
        "SELECT id, check_id, target, status, summary, payload "
        "FROM check_runs WHERE stage = 'L2' ORDER BY id"
    )
    log.info("scanned %d L2 rows", len(rows))

    counts: Counter = Counter()
    changes: list[tuple[int, dict, str]] = []   # (row_id, new_payload, new_summary)

    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        radar = payload.get("radar")
        absent = EXPECTED_ABSENT_MOMENTS.get(radar)
        if not absent:
            counts["no_exclusions_for_radar"] += 1
            continue
        old_dead = payload.get("dead_moments") or []
        if not isinstance(old_dead, list):
            counts["unexpected_dead_moments_type"] += 1
            continue
        new_dead = [m for m in old_dead if m not in absent]
        if new_dead == old_dead:
            counts["no_change"] += 1
            continue
        new_payload = {
            **payload,
            "dead_moments": new_dead,
            "expected_absent_moments": sorted(absent),
        }
        # If the summary embedded the old dead_moments list, rebuild it.
        summary = r["summary"] or ""
        if "dead_moments=" in summary and new_dead != old_dead:
            base = summary.split("  dead_moments=")[0]
            if new_dead:
                summary = f"{base}  dead_moments={new_dead}"
            else:
                summary = base
        changes.append((r["id"], new_payload, summary))
        counts["changed"] += 1

    log.info("plan: %s", dict(counts))
    if not changes:
        log.info("nothing to do")
        return
    if dry_run:
        log.info("--dry-run: %d rows would change; sample:", len(changes))
        for row_id, p, s in changes[:5]:
            log.info("  id=%s dead_moments=%s summary=%s",
                     row_id, p["dead_moments"], s)
        return

    log.info("writing %d row updates …", len(changes))
    async with conn.transaction():
        for row_id, p, s in changes:
            await conn.execute(
                "UPDATE check_runs SET payload = $2, summary = $3 WHERE id = $1",
                row_id, json.dumps(p), s,
            )
    log.info("done — updated %d rows", len(changes))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
