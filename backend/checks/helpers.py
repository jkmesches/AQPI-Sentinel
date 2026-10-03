"""Small utilities shared across check modules.

Kept dependency-free of other backend modules so it can be imported anywhere
without circularity.
"""
from __future__ import annotations
import re
from datetime import datetime, timezone

from .base import Status


# --------------------------------------------------------------------------
# Status math
# --------------------------------------------------------------------------

# Higher rank = worse. Used to fold per-sub-check statuses into a single
# overall status (worst-of).
#
# Note on `pass` vs `skip`: pass outranks skip so that an aggregate with
# *some* passing sub-checks + some skipped sub-checks rolls up to pass,
# not skip. Skip is reserved for "we didn't actually assess anything" —
# without that, forecast products (which intentionally skip step-count
# and parity checks because those fields aren't meaningful for them)
# would always show gray even though every applicable sub-check is fine.
_STATUS_RANK: dict[Status, int] = {
    "skip":  0,
    "pass":  1,
    "warn":  2,
    "fail":  3,
    "error": 4,
}


def worst_of(*statuses: Status) -> Status:
    """Return the most-severe status from the args."""
    if not statuses:
        return "pass"
    return max(statuses, key=lambda s: _STATUS_RANK[s])


# --------------------------------------------------------------------------
# Timestamp parsers (radarca-flavored)
# --------------------------------------------------------------------------

def parse_api_ts(s: str) -> datetime:
    """Accept ``2026-05-15T23:54:00`` or ``...000000000``  ('Z' suffix tolerated).

    The radarca API mixes two formats inside the same JSON document so we
    strip subsecond noise + 'Z' before fromisoformat.
    """
    s = s.replace("Z", "").split(".")[0]
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# (regex, strftime fmt) tried in order; first hit wins.
_FILENAME_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # comp_ref / water_* / max_water_*: "20260516_0028.png"
    (re.compile(r"^(\d{8}_\d{4})\.png$"),                "%Y%m%d_%H%M"),
    # qpe_15min / qpe_1hr / precip_rate_radar: "..._YYYYMMDD_HHMMSS_rainfall.nc.png"
    (re.compile(r"_(\d{8}_\d{6})_rainfall\.nc\.png$"),   "%Y%m%d_%H%M%S"),
    # X-band scans: "scwa_CorrReflectivity_20260515-2336.png"
    (re.compile(r"_(\d{8}-\d{4})\.png$"),                "%Y%m%d-%H%M"),
)

# Forecast products (`fcst_*`) name their PNGs by HRRR step index, not by
# timestamp — `C_hrrr_accum_step5.png`, `C_hrrr_temp_step0.png`, etc.
# parse_filename_step_idx pulls the integer for step-index parity checks
# (Layer 3B fold-in inside layer1_product). The two parsers are
# mutually exclusive in practice — observed products encode a time,
# forecast products encode a step index.
_FILENAME_STEP_RX = re.compile(r"_step(\d+)\.png$")


def parse_filename_ts(name: str) -> datetime | None:
    """Pull the timestamp encoded in a product PNG filename, or None if the
    filename doesn't carry one (e.g. forecast step-indexed names — try
    parse_filename_step_idx for those)."""
    for rx, fmt in _FILENAME_PATTERNS:
        m = rx.search(name)
        if m:
            try:
                return datetime.strptime(m.group(1), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def parse_filename_step_idx(name: str) -> int | None:
    """For forecast products: pull the integer step index from
    `..._step<N>.png`. None when the filename doesn't carry one (observed
    products with timestamp names go through parse_filename_ts instead)."""
    m = _FILENAME_STEP_RX.search(name)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Cadence policy
# --------------------------------------------------------------------------

def derive_check_cadence(scan_cadence_s: int | None) -> int:
    """Half the upstream scan cadence, floored to 60 s. ``None`` (no native
    cadence) → 1 h."""
    if scan_cadence_s is None:
        return 3600
    return max(60, scan_cadence_s // 2)


# --------------------------------------------------------------------------
# Freshness headroom
# --------------------------------------------------------------------------

def headroom(age_s: float, limit_s: float) -> float:
    """Fraction of a freshness budget still unspent, clamped to 0..1.

    The dashboard sparklines plot this rather than raw ``age_s`` because the
    limits are not comparable across checks: the per-radar silence thresholds
    alone span 300 s (XEBY) to 1080 s (CBAND), and the product limits run 360 s
    to 90 000 s. Dividing by the budget puts every check on one axis where 1.0
    is "just arrived" and 0.0 is "out of budget", which is what lets the
    sparkline use a FIXED y range.

    That fixed range is the point. Autoscaling each trace to its own observed
    min/max -- what Sparkline.svelte did before -- rescales every series to fill
    the box, so a 2% wobble and a 6x swing draw the same picture, and a
    perfectly steady series collapses onto the baseline where a dead one
    already sits. Those two were pixel-identical.

    ``limit_s`` is negative for the nowcast/forecast products, which publish
    timestamps AHEAD of wall clock (see layer1_product's note on negative
    max_freshness_s). Both signs work here because this measures distance from
    the limit, not from zero; dividing by ``abs(limit_s)`` then reads as
    "fraction of the required lead time still in hand". A healthy nowcast
    therefore sits lower in the box than a healthy radar -- it is a different
    budget -- but it is still flat when steady and still falls to 0 exactly
    where the verdict flips.
    """
    span = abs(float(limit_s))
    if span == 0.0:
        return 0.0
    return max(0.0, min(1.0, (float(limit_s) - float(age_s)) / span))
