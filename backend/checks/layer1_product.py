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
        try:
            r = await ctx.http.get(
                f"{SETTINGS.base}/api/productDetail",
                params={"file": cfg["details"]},
            )
        except Exception as e:
            return _fail_envelope(self, t0, f"Manifest fetch failed: {humanize_error(e)}", payload, metrics)

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
            sub["F_image_exists"] = "fail"
            payload["image_error"] = str(e)
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
        prev_step_idx: int | None = None
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
                # First step parsed: nothing to compare to yet. Subsequent
                # steps must be prev + 1 — gaps / duplicates / out-of-order
                # all surface as mismatches.
                if prev_step_idx is None:
                    matched += 1
                elif f_idx == prev_step_idx + 1:
                    matched += 1
                else:
                    mismatches += 1
                    if first_mismatch is None:
                        first_mismatch = {
                            "mode": "step_index",
                            "imageName": name,
                            "filename_step": f_idx,
                            "expected_step": prev_step_idx + 1,
                            "manifest_pos":  idx,
                        }
                prev_step_idx = f_idx
                continue
            unparseable += 1

        if matched + mismatches == 0:
            parity_verdict = "skip"           # nothing parseable to compare
        elif mismatches:
            # Upstream HRRR pipeline glitches (duplicate or out-of-order
            # step files) used to mark this as fail. They're data-quality
            # issues, not service outages — the product is still serving,
            # the manifest just has a hiccup. warn matches the v0.1.2
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

        summary = _summarize(sub, age_s, n, ir.status_code if ir else None,
                             cfg["unit"])
        return _final(self, t0, {**sub, "parity": parity_verdict}, payload, metrics,
                      summary=summary)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

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
               unit: str) -> str:
    failures = [k for k, v in sub.items() if v in ("fail", "error")]
    warnings = [k for k, v in sub.items() if v == "warn"]
    base = f"n={n}  age={_fmt_age(age_s)}  img={image_http}"
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
