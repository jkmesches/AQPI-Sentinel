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
from ..registry import register
from .base import Check, CheckResult, utcnow
from .helpers import (
    derive_check_cadence,
    parse_api_ts,
    parse_filename_ts,
    worst_of,
)


# Tolerance on step-count drift (the 1-h rolling window legitimately
# gains/loses a couple steps as it slides).
STEP_COUNT_TOL = 4
# Cadence tolerance: ±10% of expected median Δt
CADENCE_TOL = 0.10


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
            return _fail_envelope(self, t0, f"A_api transport: {e}", payload, metrics)

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
        sub["C_freshness"] = "pass" if age_s <= cfg["max_freshness_s"] else "fail"

        # --- D. cadence ---
        scan_cad = cfg.get("cadence_s")
        if scan_cad is None or n < 2:
            sub["D_cadence"] = "pass"
        else:
            diffs = [(ts_list[i + 1] - ts_list[i]).total_seconds() for i in range(n - 1)]
            med = statistics.median(diffs)
            metrics["median_dt_s"] = float(med)
            sub["D_cadence"] = (
                "pass" if abs(med - scan_cad) <= scan_cad * CADENCE_TOL else "warn"
            )

        # --- E. step count ---
        expected = cfg["expected_steps"]
        delta = abs(n - expected)
        metrics["step_count_delta"] = float(delta)
        sub["E_step_count"] = "pass" if delta <= STEP_COUNT_TOL else "warn"

        # --- F. latest image exists + G. size + H. hash ---
        img_url = f"{SETTINGS.base}/api/imageData"
        img_file = image_path(self.product_id, latest_step["imageName"])
        try:
            ir = await ctx.http.get(img_url, params={"file": img_file})
        except Exception as e:
            sub["F_image_exists"] = "fail"
            payload["image_error"] = str(e)
            return _final(self, t0, sub, payload, metrics,
                          summary=f"image transport: {e}")

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
            sub["G_image_size"] = "pass" if size >= cfg["min_png_bytes"] else "warn"
            payload["image_sha256"] = hashlib.sha256(ir.content).hexdigest()
            sub["H_image_hash"] = "pass"

        # --- L3B parity (fold-in) ---
        matched = unparseable = mismatches = 0
        first_mismatch = None
        for s in steps:
            f_ts = parse_filename_ts(s["imageName"])
            a_ts = parse_api_ts(s["timestamp"])
            if f_ts is None:
                unparseable += 1
                continue
            if abs((a_ts - f_ts).total_seconds()) <= 60:
                matched += 1
            else:
                mismatches += 1
                if first_mismatch is None:
                    first_mismatch = {
                        "imageName": s["imageName"],
                        "filename_ts": f_ts.isoformat(),
                        "api_ts": a_ts.isoformat(),
                    }

        if unparseable == n:
            parity_verdict = "skip"           # filename encodes no time (forecasts)
        elif mismatches:
            parity_verdict = "fail"
        elif matched == 0:
            parity_verdict = "fail"
        else:
            parity_verdict = "pass"

        payload["parity"] = {
            "verdict": parity_verdict,
            "matched": matched, "unparseable": unparseable,
            "mismatches": mismatches,
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
