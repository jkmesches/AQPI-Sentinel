"""Layer 4 Tier 1 + Tier 2 — image-based health on a single product/radar.

One parameterized Check class registered for:
  * each X-band radar (kind="xband") — fetches latest CorrReflectivity
  * each mosaic product (kind="mosaic") — fetches latest scan PNG

Each cycle:
  1. Resolve "latest scan" via the appropriate radarca endpoint
  2. Fetch the PNG bytes
  3. Compute Tier 1 stats
  4. Compute Tier 2 heuristics
  5. Compare pHash to previous run (frozen-frame across-run detection)
  6. Emit one CheckResult — status is OK unless a heuristic verdict trips
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Any

from ..archive import save_image as _archive_save
from ..config import PRODUCTS, RADAR_FOLDER, SETTINGS, image_path, l4_profile
from ..registry import register
from .base import Check, CheckResult, utcnow
from .helpers import worst_of
from .imaging import tier1_stats, tier2_heuristics


# Module-level in-memory store of last pHash per (check_id, target). Survives
# the process; will be moved to image_archive in a later milestone (P4.0).
_LAST_PHASH: dict[str, str] = {}


def _parse_step_ts(s: dict) -> datetime | None:
    """Raw upstream mosaic step dicts carry `timestamp`, not `ts` (the latter
    is what our /api/upstream/product_steps proxy emits to the frontend, but
    we hit productDetail directly here). Treat as UTC, parse, return None on
    missing / unparseable."""
    ts = s.get("timestamp") or s.get("ts")
    if not ts:
        return None
    try:
        # Normalize trailing Z + missing tz to UTC.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _step_closest_to_now(steps: list[dict]) -> dict | None:
    """Pick the step whose timestamp is nearest to wall-clock now. Falls back
    to `steps[-1]` if no step has a parseable timestamp (observed products
    don't always include `ts`)."""
    if not steps:
        return None
    now = datetime.now(timezone.utc)
    best: tuple[float, dict] | None = None
    for s in steps:
        dt = _parse_step_ts(s)
        if dt is None:
            continue
        delta = abs((dt - now).total_seconds())
        if best is None or delta < best[0]:
            best = (delta, s)
    if best is not None:
        return best[1]
    return steps[-1]


class Layer4ImageCheck(Check):
    """Either an X-band scan check (kind=xband) or a mosaic-product check."""

    stage = "L4-T1T2"
    cadence_s = 120
    depends_on: list[str] = []

    def __init__(self, *, kind: str, identifier: str):
        assert kind in ("xband", "mosaic"), kind
        self.kind = kind
        self.id = f"layer4.{kind}.{identifier}"
        self.target = identifier
        self.identifier = identifier
        if kind == "xband":
            self.depends_on = [f"layer2.radar.{identifier}"]
            self.folder = RADAR_FOLDER[identifier]
        else:
            self.depends_on = [f"layer1.product.{identifier}"]

    # ------------------------------------------------------------------
    async def _fetch_image(self, ctx) -> tuple[bytes, str] | tuple[None, str]:
        """Return (png_bytes, source_label) or (None, error_message)."""
        if self.kind == "xband":
            r = await ctx.http.get(
                f"{SETTINGS.base}/api/xbandRadarImages/",
                params={"radarFolder": self.folder, "productPrefix": "CorrReflectivity"},
            )
            if r.status_code != 200:
                return None, f"xbandRadarImages HTTP {r.status_code}"
            doc = r.json()
            imgs = doc.get("images") or []
            if not imgs:
                return None, "no images in 1h window"
            latest_file = imgs[-1]
            ir = await ctx.http.get(f"{SETTINGS.base}/api/imageData",
                                    params={"file": latest_file})
            if ir.status_code != 200 or not ir.headers.get("content-type", "").startswith("image/png"):
                return None, f"imageData HTTP {ir.status_code}"
            return ir.content, latest_file
        # mosaic
        cfg = PRODUCTS[self.identifier]
        r = await ctx.http.get(
            f"{SETTINGS.base}/api/productDetail", params={"file": cfg["details"]},
        )
        if r.status_code != 200:
            return None, f"productDetail HTTP {r.status_code}"
        steps = r.json().get("steps", [])
        if not steps:
            return None, "no steps"
        # Pick the step closest to wall-clock now (in either direction) rather
        # than steps[-1]. For observed products (comp_ref, qpe_*) `steps[-1]`
        # IS roughly "now" so this is a no-op. For forecast products
        # (water_depth, comp_now, max_water_depth, water_level), `steps[-1]`
        # is the furthest-out forecast horizon — e.g. water_depth's `-1` is
        # tomorrow 06:00, not the current depth. QC-ing tomorrow's forecast
        # as if it were the current state is what made these tickers warn.
        target_step = _step_closest_to_now(steps)
        if target_step is None:
            return None, "no parseable step timestamps"
        latest = target_step["imageName"]
        file_path = image_path(self.identifier, latest)
        ir = await ctx.http.get(f"{SETTINGS.base}/api/imageData",
                                params={"file": file_path})
        if ir.status_code != 200 or not ir.headers.get("content-type", "").startswith("image/png"):
            return None, f"imageData HTTP {ir.status_code}"
        return ir.content, file_path

    # ------------------------------------------------------------------
    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        png_bytes, src = await self._fetch_image(ctx)
        if png_bytes is None:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="skip",
                started_at=t0, finished_at=utcnow(),
                summary=f"no image: {src}",
                payload={"reason": src},
            )

        # Archive the captured PNG so the timeline detail panel can still
        # show this scan after the upstream rotates the file out (radarca
        # keeps only ~2 h of composites / ~1 h of X-band scans). Saved
        # off-thread so it doesn't add to the check's wall-clock time.
        if SETTINGS.archive_enabled and ctx.pool is not None:
            asyncio.create_task(
                _archive_save(
                    ctx.pool, SETTINGS.archive_root,
                    source=src, content=png_bytes,
                    content_type="image/png",
                    origin_url=f"{SETTINGS.base}/api/imageData?file={src}",
                )
            )

        # Per-product QC profile knobs. Forecast products and color-ramp
        # scalar fields get relaxed thresholds; see config.L4_PROFILES.
        prof = l4_profile(self.identifier)
        extreme_threshold  = float(prof["extreme_threshold"])
        skip_frozen        = bool(prof["skip_frozen"])
        frozen_min_cov_pct = float(prof["frozen_min_cov_pct"])

        # Offload CPU work to a thread so the asyncio loop stays responsive.
        t1 = await asyncio.to_thread(tier1_stats, png_bytes)
        t2 = await asyncio.to_thread(
            tier2_heuristics, png_bytes, self.kind, extreme_threshold
        )

        # Demote SATURATED to OK_LOW_COV on sparse scenes: when only a few
        # percent of pixels are active, "40% of them are in one color bin"
        # is statistical noise rather than a real anomaly. Same coverage
        # floor as the frozen-frame gate.
        if (t2["extreme"]["verdict"] == "SATURATED"
                and t1["coverage_pct"] < frozen_min_cov_pct):
            t2["extreme"]["verdict"] = "OK_LOW_COV"

        # Frozen-frame detection — gated by coverage and by profile.
        # A pHash match on a near-empty frame is expected ("calm world,"
        # not "stuck feed"); a pHash match on a slow-cadence forecast
        # product is also expected. Both demote to QUIET, which counts as
        # pass in the rollup.
        prev_phash = _LAST_PHASH.get(self.id)
        if prev_phash is not None and prev_phash == t1["phash"]:
            if skip_frozen:
                frozen_verdict = "QUIET_SLOW"      # slow-cadence product
            elif t1["coverage_pct"] < frozen_min_cov_pct:
                frozen_verdict = "QUIET_LOW_COV"   # quiet sky, just clutter
            else:
                frozen_verdict = "FROZEN"
        else:
            frozen_verdict = "OK"
        _LAST_PHASH[self.id] = t1["phash"]

        sub_verdicts: list[str] = []
        # Verdicts we treat as "pass" (no anomaly worth alarming on).
        OK_VERDICTS = (
            "OK", "N/A", "EMPTY", "TOO_SPARSE",
            "QUIET_LOW_COV", "QUIET_SLOW", "OK_LOW_COV",
        )
        def _v(name: str, verdict: str):
            sub_verdicts.append("pass" if verdict in OK_VERDICTS else "warn")

        _v("extreme",    t2["extreme"]["verdict"])
        _v("speckle",    t2["speckle"]["verdict"])
        _v("range_ring", t2["range_ring"]["verdict"])
        _v("frozen",     frozen_verdict)

        overall = worst_of(*sub_verdicts) if sub_verdicts else "pass"

        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status=overall,
            started_at=t0, finished_at=utcnow(),
            summary=(
                f"cov={t1['coverage_pct']}% autocorr={t1['autocorr']}  "
                f"ext={t2['extreme']['verdict']}  spk={t2['speckle']['verdict']}  "
                f"ring={t2['range_ring']['verdict']}  frozen={frozen_verdict}"
            ),
            payload={
                "source": src,
                "profile": prof,
                "tier1": t1,
                "tier2": {
                    **t2,
                    "frozen": {"verdict": frozen_verdict, "prev_phash": prev_phash},
                },
            },
            metrics={
                "coverage_pct": float(t1["coverage_pct"]),
                "mean_value":   float(t1["mean_value"]),
                "std_value":    float(t1["std_value"]),
                "autocorr":     float(t1["autocorr"]),
            },
        )


# --------------------------------------------------------------------------
# Register: 5 X-band radars (skip CBAND for now — different image geometry)
# and 3 mosaic products that produce real (non-placeholder) imagery.
# --------------------------------------------------------------------------
for r in ("XSCV", "XSCW", "XSCR", "XSWR", "XEBY"):
    register(Layer4ImageCheck(kind="xband", identifier=r))

for p in ("comp_ref", "comp_now", "water_depth"):
    register(Layer4ImageCheck(kind="mosaic", identifier=p))
