"""One-shot reprocessor for L1 forecast-product verdicts.

Re-evaluates the `G_image_size` and `E_step_count` sub-checks for the four
forecast products whose config was recalibrated:

  fcst_precip_rate         min_png_bytes  5000  →  1500
  fcst_total_precip        min_png_bytes  5000  →  1500
  fcst_total_precip_cum    min_png_bytes  5000  →  1500
  fcst_temp                expected_steps  75   →  130

Walks every layer1.product.<id> check_run for those products in
chronological order, recomputes the relevant verdicts from the saved
payload (image_bytes and n_steps), rebuilds the overall status via
worst_of(sub_status.values()), and updates status + summary + payload in
place. Sub-checks we don't touch (parity, A_api_up, etc.) are preserved.

The image bytes / n_steps are already in the payload from when the check
ran, so this is a pure verdict-recompute — no upstream fetches.

Usage:
    python -m backend.reprocess_l1_forecasts              # update everything
    python -m backend.reprocess_l1_forecasts --dry-run    # show counts, no writes
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

from backend.config import PRODUCTS, SETTINGS  # noqa: E402

log = logging.getLogger("reprocess_l1_forecasts")

# Mirrors backend/checks/layer1_product.py STEP_COUNT_TOL
STEP_COUNT_TOL = 4

AFFECTED_PRODUCTS = (
    "fcst_precip_rate",
    "fcst_total_precip",
    "fcst_total_precip_cum",
    "fcst_temp",
)


def worst_of(*verdicts: str) -> str:
    # Mirrors backend.checks.helpers._STATUS_RANK exactly. Skip ranks
    # BELOW pass — an aggregate with some passing + some skipped
    # sub-checks rolls up to pass (forecasts intentionally skip
    # step_count + parity; the overall should still be green).
    rank = {"skip": 0, "pass": 1, "warn": 2, "fail": 3, "error": 4}
    cur = "skip"
    for v in verdicts:
        if rank.get(v, 0) > rank[cur]:
            cur = v
    return cur


def _fmt_age(seconds: float) -> str:
    """Reproduces backend.checks.layer1_product._fmt_age."""
    sign = "-" if seconds < 0 else ""
    t = abs(int(seconds))
    if t < 60:
        return f"{sign}{t}s"
    if t < 3600:
        m = t // 60
        s = t % 60
        return f"{sign}{m}m" + (f"{s}s" if s else "")
    d = t // 86400
    h = (t % 86400) // 3600
    m = (t % 3600) // 60
    out = f"{d}d" if d else ""
    out += f"{h}h" if (h or d) else ""
    out += f"{m}m"
    return f"{sign}{out}"


def _summarize(sub: dict[str, str], age_s: float, n: int, image_http) -> str:
    failures = [k for k, v in sub.items() if v in ("fail", "error")]
    warnings = [k for k, v in sub.items() if v == "warn"]
    base = f"n={n}  age={_fmt_age(age_s)}  img={image_http}"
    if failures:
        return f"{base}  fail={failures}"
    if warnings:
        return f"{base}  warn={warnings}"
    return base


def recompute(payload: dict, product_id: str) -> tuple[dict, str, str] | None:
    """Return (new_payload, new_status, new_summary) or None if we can't recompute."""
    cfg = PRODUCTS.get(product_id)
    if cfg is None:
        return None
    sub = dict(payload.get("sub_status") or {})
    if not sub:
        return None

    # G_image_size — re-verdict against the new min_png_bytes.
    image_bytes = payload.get("image_bytes")
    image_http  = payload.get("image_http")
    if (image_http == 200 and image_bytes is not None
            and "G_image_size" in sub
            and sub["G_image_size"] not in ("fail",)):  # leave fail alone, that's HTTP-level
        sub["G_image_size"] = "pass" if image_bytes >= cfg["min_png_bytes"] else "warn"

    # E_step_count — re-verdict against the new expected_steps.
    # `expected_steps: None` means skip (forecast horizons drift).
    n = payload.get("n_steps")
    if n is not None and "E_step_count" in sub:
        expected = cfg.get("expected_steps")
        if expected is None:
            sub["E_step_count"] = "skip"
        else:
            delta = abs(n - expected)
            sub["E_step_count"] = "pass" if delta <= STEP_COUNT_TOL else "warn"

    # Overall status from the (possibly updated) sub-status verdicts.
    new_status = worst_of(*sub.values()) if sub else "pass"

    # Rebuild the summary. `age` is derived from last_ts vs payload-time;
    # since we don't have the original now() for this run, we approximate
    # via last_ts - run.finished_at (caller passes finished_at into payload).
    age_s = payload.get("_age_s_at_run", 0.0)

    new_payload = {**payload, "sub_status": sub}
    new_payload.pop("_age_s_at_run", None)
    new_summary = _summarize(sub, age_s, n if n is not None else 0, image_http)
    return new_payload, new_status, new_summary


async def main(dry_run: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = await asyncpg.connect(SETTINGS.db_url)

    targets = [f"layer1.product.{p}" for p in AFFECTED_PRODUCTS]
    total = await conn.fetchval(
        "SELECT count(*) FROM check_runs WHERE check_id = ANY($1)", targets,
    )
    log.info("rows to consider: %d (check_ids=%s)", total, targets)

    rows = await conn.fetch(
        "SELECT id, check_id, target, status, summary, payload, finished_at "
        "FROM check_runs WHERE check_id = ANY($1) "
        "ORDER BY check_id, started_at ASC",
        targets,
    )

    counts = Counter()
    updates: list[tuple[int, str, str, dict]] = []
    for r in rows:
        # Only preserve "error" (transport failure — nothing to recompute).
        # "skip" can come from worst_of rollup when sub-checks include skip,
        # which IS recomputable — and is exactly what we want to flip after
        # the rank change (skip < pass means a mix now rolls up to pass).
        if r["status"] == "error":
            counts["preserved_error"] += 1
            continue
        raw_payload = r["payload"]
        if isinstance(raw_payload, str):
            raw_payload = json.loads(raw_payload)
        if not raw_payload:
            counts["skipped_no_payload"] += 1
            continue
        payload = dict(raw_payload)

        # Best-effort age reconstruction so the rewritten summary's age column
        # stays approximately right. last_ts is recorded in the payload at run
        # time; finished_at is when the check completed (close enough to "now"
        # at that point).
        last_ts = payload.get("last_ts")
        age_s = 0.0
        if last_ts:
            try:
                from datetime import datetime
                last = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                age_s = (r["finished_at"] - last).total_seconds()
            except Exception:
                pass
        payload["_age_s_at_run"] = age_s

        out = recompute(payload, r["target"])
        if out is None:
            counts["unrecoverable"] += 1
            continue
        new_payload, new_status, new_summary = out
        if (new_status == r["status"]
                and new_summary == r["summary"]
                and new_payload.get("sub_status") == (raw_payload or {}).get("sub_status")):
            counts["unchanged"] += 1
            continue
        counts[f"changed_{r['status']}_to_{new_status}"] += 1
        updates.append((r["id"], new_status, new_summary, new_payload))

    log.info("verdict diff: %s", dict(counts))
    log.info("rows to update: %d", len(updates))

    if dry_run:
        log.info("--dry-run, no writes.")
        await conn.close()
        return

    CHUNK = 500
    async with conn.transaction():
        for i in range(0, len(updates), CHUNK):
            batch = updates[i : i + CHUNK]
            await conn.executemany(
                "UPDATE check_runs SET status=$1, summary=$2, payload=$3 WHERE id=$4",
                [(s, sm, json.dumps(p), rid) for (rid, s, sm, p) in batch],
            )
            log.info("updated %d / %d", min(i + CHUNK, len(updates)), len(updates))
    log.info("done.")
    await conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    asyncio.run(main(args.dry_run))
