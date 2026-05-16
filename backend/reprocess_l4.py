"""One-shot reprocessor for Layer 4 image-QC verdicts.

Walks every `layer4.*` row in `check_runs` and re-evaluates the tier-2
verdicts (extreme / frozen) under the *current* per-product profile in
`config.L4_PROFILES`. The image bytes are long gone, but each saved
payload already carries everything we need: pHash for frozen detection,
coverage_pct for the low-coverage gate, and the extreme `fraction` for
the saturation verdict. Speckle and range_ring verdicts don't depend on
profile knobs so we leave them as-is.

Runs in chronological order per (check_id, target) so the frozen
detector sees the same prev_phash trail it would have seen live.

Usage:
    python -m backend.reprocess_l4              # update everything
    python -m backend.reprocess_l4 --dry-run    # show counts, don't write
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

# Load .env from the project root before importing config.
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")

from backend.config import SETTINGS, l4_profile  # noqa: E402

log = logging.getLogger("reprocess_l4")

OK_VERDICTS = {
    "OK", "N/A", "EMPTY", "TOO_SPARSE",
    "QUIET_LOW_COV", "QUIET_SLOW", "OK_LOW_COV",
}


def worst_of(*verdicts: str) -> str:
    """Mirrors backend.checks.helpers.worst_of for our small status set."""
    rank = {"pass": 0, "warn": 1, "fail": 2, "error": 3}
    cur = "pass"
    for v in verdicts:
        if rank.get(v, 0) > rank[cur]:
            cur = v
    return cur


def reverdict_extreme(
    fraction: float | None,
    threshold: float,
    coverage_pct: float | None,
    min_cov_pct: float,
) -> str:
    if fraction is None:
        return "N/A"
    if fraction == 0.0:
        return "EMPTY"
    raw = "SATURATED" if fraction > threshold else "OK"
    if raw == "SATURATED" and coverage_pct is not None and coverage_pct < min_cov_pct:
        return "OK_LOW_COV"
    return raw


def reverdict_frozen(
    cur_phash: str | None,
    prev_phash: str | None,
    coverage_pct: float | None,
    skip_frozen: bool,
    min_cov_pct: float,
) -> str:
    if prev_phash is None or cur_phash is None:
        return "OK"
    if cur_phash != prev_phash:
        return "OK"
    if skip_frozen:
        return "QUIET_SLOW"
    if coverage_pct is not None and coverage_pct < min_cov_pct:
        return "QUIET_LOW_COV"
    return "FROZEN"


def recompute_row(payload: dict, prev_phash: str | None) -> tuple[dict, str, str]:
    """Returns (new_payload, new_status, new_summary)."""
    identifier = payload.get("source", "").split("/", 1)[0]  # rough fallback
    # Prefer pulling identifier from the check_id when we have it.
    return payload, "pass", ""


async def main(dry_run: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = await asyncpg.connect(SETTINGS.db_url)

    total_rows = await conn.fetchval(
        "SELECT count(*) FROM check_runs WHERE stage = 'L4-T1T2'"
    )
    log.info("total L4-T1T2 rows: %d", total_rows)

    # Pull rows ordered by (check_id, target, started_at) so we can walk
    # phash history per check.
    rows = await conn.fetch(
        "SELECT id, check_id, target, status, summary, payload "
        "FROM check_runs WHERE stage = 'L4-T1T2' "
        "ORDER BY check_id, target, started_at ASC"
    )

    # Track previous phash per (check_id, target).
    prev_phash: dict[tuple[str, str], str | None] = defaultdict(lambda: None)
    counts = Counter()
    changed: list[tuple[int, str, str, dict]] = []

    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            # In case the DB driver returned a stringified JSON.
            payload = json.loads(payload)
        tier1 = payload.get("tier1") or {}
        tier2 = payload.get("tier2") or {}
        if not tier1 or not tier2:
            counts["skipped_no_payload"] += 1
            continue
        # Resolve the L4 identifier: check_id like "layer4.xband.XEBY" → "XEBY"
        # or "layer4.mosaic.comp_now" → "comp_now".
        parts = r["check_id"].split(".")
        identifier = parts[-1] if len(parts) >= 3 else r["target"]
        prof = l4_profile(identifier)
        extreme_threshold = float(prof["extreme_threshold"])
        skip_frozen      = bool(prof["skip_frozen"])
        frozen_min_cov   = float(prof["frozen_min_cov_pct"])

        coverage = tier1.get("coverage_pct")
        cur_phash = tier1.get("phash")
        prev = prev_phash[(r["check_id"], r["target"])]

        old_ext = (tier2.get("extreme") or {}).get("verdict")
        old_frz = (tier2.get("frozen")  or {}).get("verdict")

        new_ext = reverdict_extreme(
            (tier2.get("extreme") or {}).get("fraction"),
            extreme_threshold,
            coverage,
            frozen_min_cov,
        )
        new_frz = reverdict_frozen(
            cur_phash, prev, coverage, skip_frozen, frozen_min_cov,
        )

        spk = (tier2.get("speckle")    or {}).get("verdict", "OK")
        ring = (tier2.get("range_ring") or {}).get("verdict", "N/A")

        def _status_of(v: str) -> str:
            return "pass" if v in OK_VERDICTS else "warn"

        new_status = worst_of(
            _status_of(new_ext),
            _status_of(spk),
            _status_of(ring),
            _status_of(new_frz),
        )
        # When the original was 'skip' or 'error', leave it alone — those
        # come from upstream fetch failures, not heuristics.
        if r["status"] in ("skip", "error"):
            prev_phash[(r["check_id"], r["target"])] = cur_phash
            counts["preserved_skip_error"] += 1
            continue

        # Build the new payload + summary.
        new_tier2 = dict(tier2)
        new_tier2["extreme"] = {**(tier2.get("extreme") or {}), "verdict": new_ext}
        new_tier2["frozen"]  = {**(tier2.get("frozen")  or {}), "verdict": new_frz}
        new_payload = {
            **payload,
            "profile": prof,
            "tier2":   new_tier2,
            "reprocessed_at": "2026-05-16",  # marker
        }
        new_summary = (
            f"cov={coverage}% autocorr={tier1.get('autocorr')}  "
            f"ext={new_ext}  spk={spk}  ring={ring}  frozen={new_frz}"
        )

        if new_status != r["status"] or new_ext != old_ext or new_frz != old_frz:
            counts[f"changed_{r['status']}_to_{new_status}"] += 1
            changed.append((r["id"], new_status, new_summary, new_payload))
        else:
            counts["unchanged"] += 1

        prev_phash[(r["check_id"], r["target"])] = cur_phash

    log.info("verdict diff: %s", dict(counts))
    log.info("rows to update: %d", len(changed))

    if dry_run:
        log.info("--dry-run, no writes.")
        await conn.close()
        return

    # Batched UPDATE — chunked to keep transactions small.
    CHUNK = 500
    async with conn.transaction():
        for i in range(0, len(changed), CHUNK):
            batch = changed[i : i + CHUNK]
            await conn.executemany(
                "UPDATE check_runs SET status = $1, summary = $2, payload = $3 WHERE id = $4",
                [(s, sm, json.dumps(p), rid) for (rid, s, sm, p) in batch],
            )
            log.info("updated %d / %d", min(i + CHUNK, len(changed)), len(changed))
    log.info("done.")
    await conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    asyncio.run(main(args.dry_run))
