"""Layer 1 — per-product availability + freshness + image existence,
plus Layer 3B filename↔API timestamp parity.

Layer 3B is folded into this check on purpose: it needs the same
``productDetail`` payload, so issuing two checks would double upstream load
for no signal gain. The parity verdict lives in ``payload['parity']``.

One instance per product in ``config.PRODUCTS``. Each emits sub-check
booleans A–H plus a parity dict, with the overall ``status`` set to the
worst of the contributing sub-checks.
"""
from __future__ import annotations
import hashlib
import statistics
from typing import Any

from ..config import PRODUCTS, SETTINGS, image_path
from ..errors import humanize_error
from .layer0_episode import note_upstream_exception, in_episode
from .. import thresholds as _thresholds
from ..registry import register
from .base import Check, CheckResult, utcnow
from .helpers import (
    derive_check_cadence,
    parse_api_ts,
    parse_filename_ts,
    parse_filename_step_idx,
    worst_of,
)


# Default tolerances. Live values come from backend.thresholds.get_global,
# which falls through to these on a fresh DB. Keeping the constants here as
# documentation of the previous behavior.
DEFAULT_STEP_COUNT_TOL = 4
DEFAULT_CADENCE_TOL    = 0.10


class Layer1ProductCheck(Check):
    """One per product in config.PRODUCTS."""

    stage = "L1"
    depends_on = ["layer0.origin.alive"]

    def __init__(self, product_id: str):
        cfg = PRODUCTS[product_id]
        self.product_id = product_id
        self.cfg = cfg
        self.id = f"layer1.product.{product_id}"
        self.target = product_id
        self.cadence_s = derive_check_cadence(cfg.get("cadence_s"))

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        cfg = self.cfg
        sub: dict[str, str] = {}        # per-sub-check status, all five-state
        payload: dict[str, Any] = {}
        metrics: dict[str, float] = {}

        # --- A. API up + B. schema ---------------------------------------
        #
        # Manifest latency is recorded deliberately. These fetches time out in
        # production during episodes, yet every direct probe of the same
        # endpoint returns in 0.03-0.33s — sequentially, at 48 concurrent, and
        # alongside 12 slow radar-status requests on a shared client. Every one
        # of those probes was a snapshot taken outside an episode, so none of
        # them could see the thing being investigated. Recording it here
        # measures the endpoint continuously, from the same process and code
        # path that actually experiences the timeouts.
        _m0 = utcnow()
        try:
            r = await ctx.http.get(
                f"{SETTINGS.base}/api/productDetail",
                params={"file": cfg["details"]},
            )
        except Exception as e:
            # Censored observation: where we gave up, not how long it needed.
            metrics["manifest_censored_ms"] = (utcnow() - _m0).total_seconds() * 1000
            if note_upstream_exception(self.id, e, t0):
                payload["reason"] = "upstream_api"
                payload["episode"] = in_episode()
            return _fail_envelope(self, t0, f"Manifest fetch failed: {humanize_error(e)}", payload, metrics)
        metrics["manifest_ms"] = (utcnow() - _m0).total_seconds() * 1000

        payload["http"] = r.status_code
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("application/json"):
            sub["A_api_up"] = "fail"
            return _final(self, t0, sub, payload, metrics,
                          summary=f"productDetail HTTP {r.status_code}")
        sub["A_api_up"] = "pass"

        try:
            doc = r.json()
        except Exception as e:
            sub["B_schema"] = "fail"
            payload["json_error"] = str(e)
            return _final(self, t0, sub, payload, metrics, summary="malformed JSON")

        steps = doc.get("steps", [])
        n = len(steps)
        payload["product_label"] = doc.get("product", "")
        payload["n_steps"] = n
        metrics["n_steps"] = float(n)

        if not steps or not all("imageName" in s and "timestamp" in s for s in steps):
            sub["B_schema"] = "fail"
            return _final(self, t0, sub, payload, metrics, summary="empty/malformed steps")
        sub["B_schema"] = "pass"

        # --- derive timestamps once ---
        try:
            ts_list = [parse_api_ts(s["timestamp"]) for s in steps]
        except Exception as e:
            sub["B_schema"] = "fail"
            payload["ts_parse_error"] = str(e)
            return _final(self, t0, sub, payload, metrics, summary="ts parse error")

        latest_step = steps[-1]
        latest_ts = ts_list[-1]
        age_s = (utcnow() - latest_ts).total_seconds()
        payload["last_ts"] = latest_ts.isoformat()
        metrics["age_s"] = float(age_s)

        # --- C. freshness ---
        # NOTE on negative max_freshness_s: nowcast/forecast products publish
        # FUTURE-dated steps (e.g. comp_now's latest_ts is ~50 min ahead of
        # wall-clock, water_depth's is ~hours ahead). For those, age_s is
        # NEGATIVE and `max_freshness_s` is set NEGATIVE too — the check
        # then verifies "the future-most timestamp is at least N seconds
        # ahead." If a forecast product regresses to delivering past
        # timestamps, age_s flips positive and the check correctly fails.
        # See config.PRODUCTS for the per-product values.
        max_fresh = _thresholds.get_product(
            self.product_id, "max_freshness_s", cfg.get("max_freshness_s"),
        )
        sub["C_freshness"] = "pass" if age_s <= max_fresh else "fail"

        # --- D. cadence ---
        scan_cad = _thresholds.get_product(
            self.product_id, "cadence_s", cfg.get("cadence_s"),
        )
        if scan_cad is None or n < 2:
            sub["D_cadence"] = "pass"
        else:
            diffs = [(ts_list[i + 1] - ts_list[i]).total_seconds() for i in range(n - 1)]
            med = statistics.median(diffs)
            metrics["median_dt_s"] = float(med)
            cad_tol = float(_thresholds.get_global("cadence_tol", DEFAULT_CADENCE_TOL))
            sub["D_cadence"] = (
                "pass" if abs(med - scan_cad) <= scan_cad * cad_tol else "warn"
            )

        # --- E. step count ---
        # `expected_steps: None` means "don't check" — for forecast products
        # whose horizon legitimately drifts hour-to-hour (e.g. fcst_temp
        # has been observed publishing 19, 74, 75, 125, 130 steps in the
        # same week as the upstream model adjusts). The ±4 tolerance is
        # meaningless when the variance is in the tens.
        expected = _thresholds.get_product(
            self.product_id, "expected_steps", cfg.get("expected_steps"),
        )
        if expected is None:
            sub["E_step_count"] = "skip"
        else:
            delta = abs(n - expected)
            metrics["step_count_delta"] = float(delta)
            step_tol = int(_thresholds.get_global("step_count_tol", DEFAULT_STEP_COUNT_TOL))
            sub["E_step_count"] = "pass" if delta <= step_tol else "warn"

        # --- F. latest image exists + G. size + H. hash ---
        img_url = f"{SETTINGS.base}/api/imageData"
        img_file = image_path(self.product_id, latest_step["imageName"])
        try:
            ir = await ctx.http.get(img_url, params={"file": img_file})
        except Exception as e:
            # str() on an httpx timeout is empty, so record the class too or
            # the only diagnostic left in the payload is a blank string.
            payload["image_error"] = str(e) or type(e).__name__
            payload["image_exception"] = type(e).__name__
            # A read timeout is a gap in OUR visibility, not evidence the
            # image is missing. Marking F_image_exists=fail rolls the whole
            # product up to `fail` — a red cell asserting the product is
            # broken — when all we actually know is that upstream did not
            # answer in time. The manifest path above already gets this right
            # via _fail_envelope; this one did not, so the same upstream
            # slowness produced `error` or `fail` depending only on which
            # fetch it happened to land on.
            if note_upstream_exception(self.id, e, t0):
                payload["reason"] = "upstream_api"
                payload["episode"] = in_episode()
                return _fail_envelope(
                    self, t0, f"Image fetch timed out: {humanize_error(e)}",
                    payload, metrics)
            sub["F_image_exists"] = "fail"
            return _final(self, t0, sub, payload, metrics,
                          summary=f"Image fetch failed: {humanize_error(e)}")

        payload["image_http"] = ir.status_code
        if ir.status_code != 200 or not ir.headers.get("content-type", "").startswith("image/png"):
            sub["F_image_exists"] = "fail"
            sub["G_image_size"] = "fail"
            sub["H_image_hash"] = "fail"
        else:
            sub["F_image_exists"] = "pass"
            size = len(ir.content)
            payload["image_bytes"] = size
            metrics["image_bytes"] = float(size)
            min_bytes = _thresholds.get_product(
                self.product_id, "min_png_bytes", cfg.get("min_png_bytes", 0),
            )
            sub["G_image_size"] = "pass" if size >= min_bytes else "warn"
            payload["image_sha256"] = hashlib.sha256(ir.content).hexdigest()
            sub["H_image_hash"] = "pass"

        # --- L3B parity (fold-in) ---
        #
        # Two parity modes depending on what the filename encodes:
        #
        #  * Observed products (radar QPE, composite, water_*) encode a
        #    timestamp — compared against the manifest's `timestamp`
        #    field via parse_filename_ts (±60 s tolerance).
        #
        #  * Forecast products (`fcst_*`) name their PNGs by HRRR step
        #    index (`C_hrrr_<prod>_step<N>.png`). The starting index
        #    varies by product (fcst_total_precip starts at step0,
        #    fcst_precip_rate at step1), so position-in-manifest isn't
        #    a useful comparison. Instead we verify the step indices
        #    are CONTIGUOUS — every step's parsed index is exactly one
        #    more than the previous. Catches gaps, duplicates, and
        #    out-of-order serving from the upstream HRRR pipeline.
        #
        # Each step lands in exactly one bucket (ts-parity, step-
        # parity, unparseable). Mode is selected per-row by which
        # parser hits — the two are mutually exclusive in practice.
        matched = unparseable = mismatches = 0
        ts_checked = step_checked = 0
        first_mismatch = None
        step_entries: list[tuple[int, Any]] = []
        for idx, s in enumerate(steps):
            name = s["imageName"]
            f_ts = parse_filename_ts(name)
            if f_ts is not None:
                ts_checked += 1
                a_ts = parse_api_ts(s["timestamp"])
                if abs((a_ts - f_ts).total_seconds()) <= 60:
                    matched += 1
                else:
                    mismatches += 1
                    if first_mismatch is None:
                        first_mismatch = {
                            "mode": "timestamp",
                            "imageName": name,
                            "filename_ts": f_ts.isoformat(),
                            "api_ts": a_ts.isoformat(),
                        }
                continue
            f_idx = parse_filename_step_idx(name)
            if f_idx is not None:
                step_checked += 1
                # Collected, not judged. A step index means nothing on its own
                # — only the shape of the whole sequence says whether the
                # forecast is intact — so the verdict is computed after the
                # scan by classify_step_sequence().
                step_entries.append((f_idx, s["timestamp"]))
                continue
            unparseable += 1

        # Step-index products are judged on the shape of the whole sequence.
        seq = classify_step_sequence(step_entries) if step_entries else None
        if seq:
            mismatches += len(seq["defects"])
            matched += seq["entries"] - len(seq["defects"])
            if seq["first_defect"] and first_mismatch is None:
                first_mismatch = {"mode": "step_index", **seq["first_defect"]}

        if matched + mismatches == 0:
            parity_verdict = "skip"           # nothing parseable to compare
        elif mismatches:
            # Upstream HRRR pipeline glitches (a dropped forecast hour, steps
            # served out of order) used to mark this as fail. They're
            # data-quality issues, not service outages — the product is still
            # serving, the manifest just has a hiccup. warn matches the v0.1.2
            # severity reshape ("requires attention", not "broken").
            parity_verdict = "warn"
        else:
            parity_verdict = "pass"

        # Mode descriptor for the drilldown. Forecast rows previously
        # rendered "unparseable=N" with no further explanation — surface
        # the actual mode + counts so a maintainer can read it cold.
        if step_checked and not ts_checked:
            parity_mode = "step_index"
        elif ts_checked and not step_checked:
            parity_mode = "timestamp"
        elif ts_checked or step_checked:
            parity_mode = "mixed"
        else:
            parity_mode = "none"

        payload["parity"] = {
            "verdict":      parity_verdict,
            "mode":         parity_mode,
            "matched":      matched,
            "mismatches":   mismatches,
            "unparseable":  unparseable,
            "ts_checked":   ts_checked,
            "step_checked": step_checked,
            "first_mismatch": first_mismatch,
        }
        if seq:
            # Reported, never alarmed — see classify_step_sequence. Kept in the
            # payload so the drilldown, /api/report/daily and any later
            # analysis can see the upstream's stitching rather than infer it
            # from a verdict that no longer mentions it.
            payload["parity"].update({
                "blocks":           seq["blocks"],
                "repeated_entries": seq["repeated_entries"],
                "steps_multi_ts":   seq["steps_multi_ts"],
                "defects":          seq["defects"],
            })

        summary = _summarize(sub, age_s, n, ir.status_code if ir else None,
                             cfg["unit"], parity=payload["parity"])
        return _final(self, t0, {**sub, "parity": parity_verdict}, payload, metrics,
                      summary=summary)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def classify_step_sequence(entries: list[tuple[int, Any]]) -> dict:
    """Judge a forecast manifest whose filenames encode a step index.

    `entries` is [(step_index, timestamp)] in manifest order.

    The upstream stitches its short-range and long-range blocks into one
    list, and re-lists the long-range block one or more times. Measured on
    2026-09-11, `temperature/details_F.json` held 129 entries for 73 distinct
    step files: steps 0-18, then 18-72, then 18-72 again byte-identically.
    Over seven days the same manifest was served at 19, 74, 129, 187 and 243
    entries, one more replay each time.

    The original rule — every index must be the previous plus one — called
    every one of those a mismatch, so the check warned on 78% of runs and
    flapped pass/warn as the upstream alternated between its short and long
    forms, opening and auto-closing 25 alarms in a week. A condition that has
    been continuously true since at least 2026-09-04 should not page anyone
    25 times; that is the boy-who-cried-wolf shape.

    So this separates two questions the old rule conflated:

      Is the forecast data intact?  Every step present with no gap, and
      timestamps advancing. A dropped or reordered step is what parity exists
      to catch, and it still WARNS.

      Is the list tidy?  Repeats and the one-index overlap where the two
      blocks meet. Real, permanent, and not something an operator can act on
      per-occurrence. COUNTED and reported, never alarmed.

    The overlap deserves a word, because it is the one judgment call here.
    At the seam a single step file carries two forecast times — on 2026-09-11,
    `step18.png` was listed at both 13:00 and 14:00 — so one of those two
    hours displays its neighbour's image. That is a genuine, if small, upstream
    defect. It is reported in `steps_multi_ts` and stated in the summary, and
    it does not set the verdict, because the index in these filenames is a
    position within its own block rather than a global identity — the same
    lesson the tilt archive taught, where a frame index names a slot and not a
    frame. Asserting across blocks asserts something the upstream never said.
    """
    out: dict[str, Any] = {
        "entries": len(entries), "blocks": 0, "repeated_entries": 0,
        "steps_multi_ts": 0, "defects": [], "first_defect": None,
    }
    if not entries:
        return out

    idxs = [e[0] for e in entries]
    tss = [e[1] for e in entries]

    # Maximal runs of +1. Each is one contiguous block as the upstream
    # published it; the boundaries are where it stitched or replayed.
    blocks: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(idxs) + 1):
        if i == len(idxs) or idxs[i] != idxs[i - 1] + 1:
            blocks.append((start, i - 1))
            start = i
    out["blocks"] = len(blocks)

    def defect(kind: str, pos: int, detail: dict) -> None:
        out["defects"].append(kind)
        if out["first_defect"] is None:
            out["first_defect"] = {"kind": kind, "manifest_pos": pos, **detail}

    # (1) Every step present. A hole means the pipeline dropped a forecast
    # hour, which no amount of re-listing can explain away.
    distinct = sorted(set(idxs))
    for a, b in zip(distinct, distinct[1:]):
        if b != a + 1:
            defect("missing_steps", idxs.index(b),
                   {"after_step": a, "next_step": b, "missing": b - a - 1})
            break

    # (2) Time advances. Checked on FIRST occurrence of each timestamp, so a
    # replay of an earlier block is not mistaken for time running backwards —
    # that is exactly the confusion the old rule made.
    seen: set = set()
    firsts: list[tuple[int, Any]] = []
    for pos, t in enumerate(tss):
        if t not in seen:
            seen.add(t)
            firsts.append((pos, t))
    for (_, a), (pos_b, b) in zip(firsts, firsts[1:]):
        if b <= a:
            defect("time_not_advancing", pos_b, {"prev_ts": str(a), "ts": str(b)})
            break

    # (3) Each block internally ordered in time as well as in index.
    for a, b in blocks:
        bad = next((p for p in range(a + 1, b + 1) if tss[p] <= tss[p - 1]), None)
        if bad is not None:
            defect("block_time_disorder", bad,
                   {"prev_ts": str(tss[bad - 1]), "ts": str(tss[bad])})
            break

    # (4) A block that re-publishes steps already published must either say
    # exactly what the first publication said, or be a continuation that
    # merely overlaps it at the join. Anything else is the upstream giving two
    # different answers for the same forecast step, and no amount of "it's
    # just a repeat" makes that benign — it is the one case the permissive
    # rule below must not swallow.
    #
    # Production's seam overlaps by exactly one index: the long-range block
    # opens on step18, which the short-range block closed on, with the next
    # hour's timestamp. Two or more re-listed indices carrying times never
    # seen before is a different model run being appended, not a join.
    pairs_seen: set = set()
    idx_seen: set = set()
    for bi, (a, b) in enumerate(blocks):
        pairs = [(idxs[p], tss[p]) for p in range(a, b + 1)]
        if bi:
            fresh = [pr for pr in pairs if pr not in pairs_seen]
            if fresh:                                   # not a verbatim replay
                clashes = [i for i, ts in fresh if i in idx_seen]
                if len(clashes) > 1:
                    defect("conflicting_republish", a,
                           {"steps": clashes[:5], "n_steps": len(clashes)})
                    break
        pairs_seen.update(pairs)
        idx_seen.update(i for i, _ in pairs)

    # Reported, not alarmed.
    out["repeated_entries"] = len(tss) - len(set(tss))
    by_idx: dict[int, set] = {}
    for i, t in entries:
        by_idx.setdefault(i, set()).add(t)
    out["steps_multi_ts"] = sum(1 for v in by_idx.values() if len(v) > 1)
    return out


def _fmt_age(total: float) -> str:
    """Compact compound: 47s | 12m | 1h23m | 6d12h37m. Signed."""
    sign = "+" if total >= 0 else "-"
    t = int(abs(total))
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


def _summarize(sub: dict[str, str], age_s: float, n: int, image_http: int | None,
               unit: str, parity: dict | None = None) -> str:
    failures = [k for k, v in sub.items() if v in ("fail", "error")]
    warnings = [k for k, v in sub.items() if v == "warn"]
    base = f"n={n}  age={_fmt_age(age_s)}  img={image_http}"
    # A manifest that lists the same forecast time more than once is no longer
    # a warn (see classify_step_sequence), so the summary has to say it —
    # otherwise the condition becomes invisible the moment it stops alarming,
    # which is how a silenced problem turns into a forgotten one.
    if parity:
        rep, multi = parity.get("repeated_entries", 0), parity.get("steps_multi_ts", 0)
        if rep:
            base += f"  rep={rep}"
        if multi:
            base += f"  seam={multi}"
    if failures:
        return f"{base}  fail={failures}"
    if warnings:
        return f"{base}  warn={warnings}"
    return base


def _final(check: Check, t0, sub: dict[str, str], payload: dict, metrics: dict,
           summary: str) -> CheckResult:
    overall = worst_of(*sub.values()) if sub else "pass"
    payload["sub_status"] = sub
    return CheckResult(
        check_id=check.id, target=check.target, stage=check.stage,
        status=overall,
        started_at=t0, finished_at=utcnow(),
        summary=summary,
        payload=payload, metrics=metrics,
    )


def _fail_envelope(check: Check, t0, msg, payload, metrics) -> CheckResult:
    return CheckResult(
        check_id=check.id, target=check.target, stage=check.stage,
        status="error",
        started_at=t0, finished_at=utcnow(),
        summary=msg, payload=payload, metrics=metrics,
    )


# --------------------------------------------------------------------------
# Register one instance per product
# --------------------------------------------------------------------------

for _pid in PRODUCTS:
    register(Layer1ProductCheck(product_id=_pid))
