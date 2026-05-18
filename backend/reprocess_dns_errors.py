"""One-shot reprocessor for historical local-DNS errors.

Walks every check_runs row where status='error' and re-classifies it
to status='skip' (with reason=local_dns_error) when the stored
payload's exception message matches the same DNS markers the live
scheduler now uses. Keeps the historical timeline self-consistent
with the new classification rule landed in backend/scheduler.py.

Skips rows for the network control check itself — local DNS errors
there are MEANINGFUL signal, not noise.

Usage:
    python -m backend.reprocess_dns_errors              # apply
    python -m backend.reprocess_dns_errors --dry-run    # report only
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

from backend.config import SETTINGS              # noqa: E402
from backend.scheduler import _DNS_ERROR_MARKERS  # noqa: E402

log = logging.getLogger("reprocess_dns_errors")


def _looks_like_dns(message: str, exc_name: str) -> bool:
    """String-only equivalent of scheduler._is_local_dns_error."""
    msg = (message or "").lower()
    if any(m in msg for m in _DNS_ERROR_MARKERS):
        return True
    # `gaierror` may appear as exception type without the marker text
    if (exc_name or "").lower() == "gaierror":
        return True
    return False


async def main(dry_run: bool) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    conn = await asyncpg.connect(SETTINGS.db_url)

    rows = await conn.fetch(
        "SELECT id, check_id, status, summary, payload, finished_at "
        "FROM check_runs WHERE status = 'error' "
        "ORDER BY finished_at ASC"
    )
    log.info("scanned %d error rows", len(rows))

    counts: Counter = Counter()
    updates: list[tuple[int, str, dict]] = []
    for r in rows:
        # The control check itself MUST keep showing DNS errors as errors —
        # that's load-bearing for the offline-gate logic.
        if r["check_id"].startswith("layer0.net."):
            counts["skipped_net_control"] += 1
            continue
        payload = r["payload"] or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            counts["skipped_no_payload"] += 1
            continue
        exc_name = payload.get("exception") or ""
        message = payload.get("message") or r["summary"] or ""
        if not _looks_like_dns(message, exc_name):
            counts["preserved_real_error"] += 1
            continue
        new_payload = {
            **payload,
            "reason":    "local_dns_error",
            # Preserve the original error fields for forensic value.
            "original_status":  "error",
            "original_summary": r["summary"],
        }
        new_summary = f"local DNS unavailable: {exc_name}: {message}"
        updates.append((r["id"], new_summary, new_payload))
        counts["demoted_to_skip"] += 1

    log.info("plan: %s", dict(counts))
    if dry_run or not updates:
        log.info("dry-run / nothing to do — exiting")
        return

    log.info("writing %d row updates …", len(updates))
    async with conn.transaction():
        for row_id, summary, payload in updates:
            await conn.execute(
                "UPDATE check_runs "
                "SET status = 'skip', summary = $2, payload = $3 "
                "WHERE id = $1",
                row_id, summary, json.dumps(payload),
            )
    log.info("done — demoted %d historical DNS-error rows to skip", len(updates))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    asyncio.run(main(args.dry_run))
