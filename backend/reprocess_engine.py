"""On-demand retroactive reprocessor.

Walks `check_runs` rows in a time window and re-classifies them under the
*current* threshold blob (see backend.thresholds). The original payloads
carry enough state — last_ts / image_bytes / tier2 stats / moment_newest —
to recompute the verdict without re-fetching anything from upstream.

Three stages are supported:
  * L1: C_freshness, E_step_count, G_image_size sub-checks. D_cadence is
    out of scope (its inputs live in metrics, not payload). Everything
    else folded by worst_of from the existing sub_status dict.
  * L2: ghost-up freshness verdict, using primary_newest_ts + current
    silent_fail_s + hysteresis. Other verdict modes (CONFIRMED_DOWN,
    STUCK_DOWN_FLAG, OBSERVED_API_ERROR) are not threshold-driven and
    are left as-is.
  * L4-T1T2: extreme + frozen sub-verdicts, lifted from
    backend/reprocess_l4.py but routed through backend.thresholds.

Job state lives in this module's `_jobs` dict — in-process only, lost on
restart. The admin UI provides a job_id back from POST → start, and polls
GET → status with that handle. Cancellation flips a per-job flag the
engine checks between rows.

Rows with `reason in {local_dns_error, transport_error}` are preserved
(they don't reflect a threshold-driven verdict). Rows whose original
status is `error` or `skip` are also preserved — those come from
transport failures, not classification.
"""
from __future__ import annotations
import asyncio
import json
import logging
import secrets
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from . import thresholds as _thresholds

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-stage re-verdict helpers
# ---------------------------------------------------------------------------

_STATUS_RANK = {"skip": 0, "pass": 1, "warn": 2, "fail": 3, "error": 4}


def _worst_of(*statuses: str) -> str:
    cur = "skip"
    for s in statuses:
        if _STATUS_RANK.get(s, 0) > _STATUS_RANK[cur]:
            cur = s
    return cur


_L4_OK_VERDICTS = {
    "OK", "N/A", "EMPTY", "TOO_SPARSE",
    "QUIET_LOW_COV", "QUIET_SLOW", "OK_LOW_COV",
    "OK_SAME_FRAME",
}


def _l4_extreme_verdict(fraction, threshold, coverage_pct, min_cov_pct):
    if fraction is None:
        return "N/A"
    if fraction == 0.0:
        return "EMPTY"
    raw = "SATURATED" if fraction > threshold else "OK"
    if raw == "SATURATED" and coverage_pct is not None and coverage_pct < min_cov_pct:
        return "OK_LOW_COV"
    return raw


def _l4_frozen_verdict(cur_phash, prev_phash, coverage_pct, skip_frozen, min_cov_pct,
                       cur_source=None, prev_source=None):
    """Mirror of the live detector in checks/layer4_image.py.

    Keep the two in step. The same-frame guard exists because the check runs
    every 120s while publish cadences vary — CBAND publishes every ~240s, so
    half its runs re-sampled a frame they had already seen and compared it
    against itself, which always matches. Measured over 24h: 366 of 366
    same-source runs flagged FROZEN, versus 0 of 352 genuinely-new ones.
    """
    if prev_source is not None and cur_source is not None and cur_source == prev_source:
        return "OK_SAME_FRAME"
    if prev_phash is None or cur_phash is None:
        return "OK"
    if cur_phash != prev_phash:
        return "OK"
    if skip_frozen:
        return "QUIET_SLOW"
    if coverage_pct is not None and coverage_pct < min_cov_pct:
        return "QUIET_LOW_COV"
    return "FROZEN"


def _reverdict_l4(row: dict, prev_phash_map: dict[tuple[str, str], str | None],
                  prev_source_map: dict[tuple[str, str], str | None] | None = None) -> tuple[str, dict] | None:
    payload = row["payload"] or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    tier1 = payload.get("tier1") or {}
    tier2 = payload.get("tier2") or {}
    if not tier1 or not tier2:
        return None
    # Resolve identifier from check_id (layer4.xband.XEBY → XEBY).
    parts = row["check_id"].split(".")
    identifier = parts[-1] if len(parts) >= 3 else row["target"]

    extreme_threshold = float(_thresholds.get_l4(identifier, "extreme_threshold", 0.40))
    skip_frozen       = bool(_thresholds.get_l4(identifier, "skip_frozen", False))
    frozen_min_cov    = float(_thresholds.get_l4(identifier, "frozen_min_cov_pct", 5.0))

    coverage = tier1.get("coverage_pct")
    cur_phash = tier1.get("phash")
    key = (row["check_id"], row["target"])
    prev = prev_phash_map.get(key)
    cur_source = payload.get("source")
    prev_source = prev_source_map.get(key) if prev_source_map is not None else None

    new_ext = _l4_extreme_verdict(
        (tier2.get("extreme") or {}).get("fraction"),
        extreme_threshold, coverage, frozen_min_cov,
    )
    new_frz = _l4_frozen_verdict(
        cur_phash, prev, coverage, skip_frozen, frozen_min_cov,
        cur_source=cur_source, prev_source=prev_source,
    )
    prev_phash_map[key] = cur_phash
    if prev_source_map is not None and cur_source:
        prev_source_map[key] = cur_source

    spk  = (tier2.get("speckle")    or {}).get("verdict", "OK")
    ring = (tier2.get("range_ring") or {}).get("verdict", "N/A")

    def _s(v): return "pass" if v in _L4_OK_VERDICTS else "warn"
    new_status = _worst_of(_s(new_ext), _s(spk), _s(ring), _s(new_frz))

    new_tier2 = dict(tier2)
    new_tier2["extreme"] = {**(tier2.get("extreme") or {}), "verdict": new_ext}
    new_tier2["frozen"]  = {**(tier2.get("frozen")  or {}), "verdict": new_frz}
    new_payload = {
        **payload, "tier2": new_tier2,
        "reprocessed_at": datetime.now(timezone.utc).isoformat(),
        "original": payload.get("original") or {
            "status":  row["status"],
            "extreme": (tier2.get("extreme") or {}).get("verdict"),
            "frozen":  (tier2.get("frozen")  or {}).get("verdict"),
        },
    }
    return new_status, new_payload


# humanize_error()'s exact phrase for httpx.ReadTimeout, on the image-fetch
# path. Matched exactly rather than by prefix: a ConnectTimeout on the same
# fetch reads "Connection timed out" and means upstream is DOWN, which is a
# different event and must keep its own verdict.
_IMAGE_TIMEOUT_SUMMARY = "Image fetch failed: Server took too long to respond"


def _reverdict_l1_image_timeout(row: dict, payload: dict) -> tuple[str, dict] | None:
    """Reclassify an image-fetch read timeout from `fail` to `error`.

    Historically the image fetch recorded F_image_exists=fail on ANY exception,
    which rolls the product up to `fail` — a red cell asserting the product
    image is missing when all we actually knew was that upstream did not answer
    within the timeout. The manifest fetch on the same check already returned
    `error` for the identical cause, so the verdict depended only on which of
    the two fetches the slowness happened to land on. The live path was fixed
    on 2026-08-31; this brings the 121 historical rows into line.

    Deliberately narrow, because the cost of over-reaching is erasing real
    product failures from the record:
      - the summary must name a read timeout exactly, and
      - the row must carry no `image_http`. The timeout path returns before
        that key is set, so its presence means we DID get a response and the
        fail verdict is about the response itself. Those stay `fail`.
    """
    if row.get("summary") != _IMAGE_TIMEOUT_SUMMARY:
        return None
    if "image_http" in payload:
        return None

    sub = dict(payload.get("sub_status") or {})
    if sub.get("F_image_exists") != "fail":
        return None
    # Drop the key rather than downgrading it, so the row matches exactly what
    # the fixed live path now writes: F was never measured, so it says nothing.
    sub.pop("F_image_exists", None)
    sub.pop("G_image_size", None)
    sub.pop("H_image_hash", None)

    new_payload = {
        **payload,
        "sub_status": sub,
        "reason": "upstream_api",
        "image_exception": "ReadTimeout",
        "reprocessed_at": datetime.now(timezone.utc).isoformat(),
        # First original wins, as in _reverdict_l2 — re-running must never
        # overwrite the true as-observed verdict with an already-reprocessed one.
        "original": payload.get("original") or {
            "status":      row["status"],
            "sub_status":  payload.get("sub_status"),
            "summary":     row.get("summary"),
        },
    }
    return "error", new_payload


def _reverdict_l1(row: dict) -> tuple[str, dict] | None:
    """Re-evaluate freshness + step_count + image_size sub-checks under
    current thresholds. D_cadence is left at the recorded value (its
    median_dt_s input is in metrics, not payload).
    """
    payload = row["payload"] or {}
    if isinstance(payload, str):
        payload = json.loads(payload)

    # Checked before the sub_status recompute: this row's F verdict is not a
    # threshold call at all, it is a transport failure misfiled as one, and
    # recomputing thresholds over it would leave the wrong status in place.
    timeout_fix = _reverdict_l1_image_timeout(row, payload)
    if timeout_fix is not None:
        return timeout_fix

    sub = payload.get("sub_status")
    if not isinstance(sub, dict):
        return None
    product_id = row["target"]
    finished_at = row["finished_at"]

    # C_freshness — recompute from last_ts (if present) + max_freshness_s.
    last_ts_str = payload.get("last_ts")
    if "C_freshness" in sub and last_ts_str:
        try:
            last_ts = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
            age_s = (finished_at - last_ts).total_seconds()
            max_fresh = _thresholds.get_product(product_id, "max_freshness_s")
            if max_fresh is not None:
                sub["C_freshness"] = "pass" if age_s <= max_fresh else "fail"
        except Exception:
            pass

    # E_step_count — recompute from n_steps + expected_steps.
    n_steps = payload.get("n_steps")
    if "E_step_count" in sub and n_steps is not None:
        expected = _thresholds.get_product(product_id, "expected_steps")
        if expected is None:
            sub["E_step_count"] = "skip"
        else:
            tol = int(_thresholds.get_global("step_count_tol", 4))
            sub["E_step_count"] = "pass" if abs(n_steps - expected) <= tol else "warn"

    # G_image_size — recompute from image_bytes + min_png_bytes.
    img_bytes = payload.get("image_bytes")
    if "G_image_size" in sub and img_bytes is not None:
        min_bytes = _thresholds.get_product(product_id, "min_png_bytes")
        if min_bytes is not None:
            sub["G_image_size"] = "pass" if img_bytes >= min_bytes else "warn"

    new_status = _worst_of(*sub.values()) if sub else "pass"
    new_payload = {**payload, "sub_status": sub,
                   "reprocessed_at": datetime.now(timezone.utc).isoformat()}
    return new_status, new_payload


def _reverdict_l2(row: dict) -> tuple[str, dict] | None:
    """Re-evaluate the GHOST_UP freshness verdict under current thresholds.

    Other verdicts (CONFIRMED_DOWN / STUCK_DOWN_FLAG / OBSERVED_API_ERROR)
    derive from declared status + transport state, not threshold knobs, and
    are preserved untouched.

    === This function silently did nothing until 2026-08-26 ===

    It read ``payload["reconcile"]``, a key layer2_radar has never emitted —
    the fields live under ``payload["observed"]`` with ``verdict`` at the top
    level. Every row therefore returned None, so L2 reprocessing was a no-op
    and the belief that "historical L2 can't be reprocessed" grew up around
    it. Verified against production: 0 of 231,356 L2 rows carry `reconcile`,
    212,572 carry `observed`. The data needed was always present.

    === Load-bearing: zero-image runs are NEVER reclassified ===

    A radar publishing no images at all is not-fresh regardless of any
    threshold (see layer2_radar: ``elif primary_n == 0: fresh = False``).
    Those GHOST_UPs are real stoppages — 5,042 of XSWR's 7,950 over the 14
    days to 2026-08-26, including the 79-hour fleet episode — and must
    survive any threshold change untouched. Only "images present but stale"
    is threshold-sensitive. Raising a threshold can therefore never erase a
    genuine outage from history; it can only relabel runs where data WAS
    flowing and we called it stale too eagerly.
    """
    payload = row["payload"] or {}
    if isinstance(payload, str):
        payload = json.loads(payload)

    obs = payload.get("observed")
    if not isinstance(obs, dict):
        # Tolerate the legacy shape this function used to assume, in case any
        # deployment somewhere still writes it.
        legacy = payload.get("reconcile")
        obs = legacy if isinstance(legacy, dict) else None
    if obs is None:
        return None

    old_verdict = payload.get("verdict") or obs.get("verdict")
    if old_verdict not in ("HEALTHY", "GHOST_UP"):
        return None

    primary_n = obs.get("primary")
    if not isinstance(primary_n, int) or primary_n <= 0:
        return None                      # real stoppage — leave it alone

    primary_ts_iso = obs.get("primary_newest_ts")
    if not primary_ts_iso:
        return None
    try:
        primary_ts = datetime.fromisoformat(str(primary_ts_iso).replace("Z", "+00:00"))
    except Exception:
        return None

    radar_id = row["target"]
    silent_fail_s = float(_thresholds.get_radar(radar_id, "silent_fail_s", 600))
    hyst = float(_thresholds.get_global("hysteresis", 0.10))
    upper = silent_fail_s * (1 + hyst)
    age_s = (row["finished_at"] - primary_ts).total_seconds()
    # Without prior-state across rows we use the upper bound only — a
    # conservative "stale = age > upper". Hysteresis governs in-stream
    # transitions, not historical reclassification.
    fresh = age_s <= upper

    new_verdict = "HEALTHY" if fresh else "GHOST_UP"
    new_status = "pass" if fresh else "fail"

    new_obs = {
        **obs,
        "fresh": fresh,
        "primary_age_s": int(age_s),
        "silent_fail_s": int(silent_fail_s),
        "silent_fail_band": [int(silent_fail_s * (1 - hyst)), int(upper)],
    }
    new_payload = {
        **payload,
        "observed": new_obs,
        "verdict": new_verdict,
        "reprocessed_at": datetime.now(timezone.utc).isoformat(),
        # Keep the FIRST original across repeated reprocesses, so re-running
        # never overwrites the true as-observed verdict with an already
        # reprocessed one. Nothing is lost: the raw observation
        # (primary count, newest timestamp) is untouched either way.
        "original": payload.get("original") or {
            "verdict":       old_verdict,
            "status":        row["status"],
            "silent_fail_s": obs.get("silent_fail_s"),
        },
    }
    return new_status, new_payload


# ---------------------------------------------------------------------------
# Job machinery
# ---------------------------------------------------------------------------

class ReprocessJob:
    def __init__(self, job_id: str, since: datetime, until: datetime, only_stages: list[str] | None):
        self.job_id = job_id
        self.since = since
        self.until = until
        self.only_stages = only_stages
        self.state: str = "pending"        # pending | running | cancelled | completed | error
        self.n_total: int = 0
        self.n_evaluated: int = 0
        self.n_changed: int = 0
        self.n_preserved: int = 0
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.error: str | None = None
        self.cancel_requested: bool = False

    def to_dict(self) -> dict:
        return {
            "job_id":       self.job_id,
            "state":        self.state,
            "since":        self.since.isoformat(),
            "until":        self.until.isoformat(),
            "only_stages":  self.only_stages,
            "n_total":      self.n_total,
            "n_evaluated":  self.n_evaluated,
            "n_changed":    self.n_changed,
            "n_preserved":  self.n_preserved,
            "started_at":   self.started_at.isoformat() if self.started_at else None,
            "finished_at":  self.finished_at.isoformat() if self.finished_at else None,
            "error":        self.error,
        }


_jobs: dict[str, ReprocessJob] = {}


def new_job_id() -> str:
    return secrets.token_hex(8)


def get_job(job_id: str) -> ReprocessJob | None:
    return _jobs.get(job_id)


def list_jobs() -> list[dict]:
    return [j.to_dict() for j in _jobs.values()]


_PRESERVE_REASONS = {"local_dns_error", "transport_error"}


async def run_reprocess(pool, job: ReprocessJob) -> None:
    """Background task — walks check_runs in window and re-classifies.

    Idempotent within a single threshold blob — re-running with no
    threshold changes results in 0 updates (every recomputed verdict
    matches the stored one).
    """
    job.started_at = datetime.now(timezone.utc)
    job.state = "running"
    _jobs[job.job_id] = job

    try:
        where = ["finished_at >= $1", "finished_at < $2"]
        args: list = [job.since, job.until]
        if job.only_stages:
            args.append(job.only_stages)
            where.append(f"stage = ANY(${len(args)}::text[])")
        total = await pool.fetchval(
            "SELECT count(*) FROM check_runs WHERE " + " AND ".join(where), *args,
        )
        job.n_total = int(total or 0)

        # Walk per (check_id, target) so the L4 phash trail is correct.
        rows = await pool.fetch(
            "SELECT id, check_id, target, stage, status, summary, payload, "
            "       started_at, finished_at "
            "FROM check_runs WHERE " + " AND ".join(where) + " "
            "ORDER BY check_id, target, started_at ASC",
            *args,
        )

        l4_phash: dict[tuple[str, str], str | None] = defaultdict(lambda: None)
        l4_source: dict[tuple[str, str], str | None] = defaultdict(lambda: None)
        updates: list[tuple[int, str, dict]] = []

        for r in rows:
            if job.cancel_requested:
                job.state = "cancelled"
                break
            job.n_evaluated += 1
            row = dict(r)

            # Preserve hand-marked transport/DNS skips.
            payload = row["payload"] or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            if payload.get("reason") in _PRESERVE_REASONS:
                job.n_preserved += 1
                continue
            if row["status"] in ("skip", "error"):
                # Per-row preservation rule from the existing scripts —
                # transport failures shouldn't be reclassified as
                # heuristic verdicts.
                job.n_preserved += 1
                # L4 still needs to advance the phash trail for the
                # next sibling row's frozen detection.
                if row["stage"] == "L4-T1T2":
                    tier1 = payload.get("tier1") or {}
                    if tier1.get("phash"):
                        l4_phash[(row["check_id"], row["target"])] = tier1["phash"]
                    if payload.get("source"):
                        l4_source[(row["check_id"], row["target"])] = payload["source"]
                continue

            new = None
            if row["stage"] == "L1":
                new = _reverdict_l1(row)
            elif row["stage"] == "L2":
                new = _reverdict_l2(row)
            elif row["stage"] == "L4-T1T2":
                new = _reverdict_l4(row, l4_phash, l4_source)

            if new is None:
                continue
            new_status, new_payload = new
            if new_status != row["status"]:
                updates.append((row["id"], new_status, new_payload))

            # Yield every ~50 rows so the asyncio loop services other
            # tasks (the API stays responsive during a long job).
            if job.n_evaluated % 50 == 0:
                await asyncio.sleep(0)

        # Batched UPDATE — keeps each transaction small.
        if updates and not job.cancel_requested:
            CHUNK = 200
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for i in range(0, len(updates), CHUNK):
                        batch = updates[i : i + CHUNK]
                        await conn.executemany(
                            "UPDATE check_runs SET status = $1, payload = $2 "
                            "WHERE id = $3",
                            [(s, p, rid) for (rid, s, p) in batch],
                        )
                        job.n_changed = i + len(batch)

        if job.state != "cancelled":
            job.state = "completed"
    except Exception as e:
        log.exception("reprocess job %s failed", job.job_id)
        job.state = "error"
        job.error = f"{type(e).__name__}: {e}"
    finally:
        job.finished_at = datetime.now(timezone.utc)
