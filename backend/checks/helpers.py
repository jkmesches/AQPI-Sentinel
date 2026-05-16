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
_STATUS_RANK: dict[Status, int] = {
    "pass":  0,
    "skip":  1,
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


def parse_filename_ts(name: str) -> datetime | None:
    """Pull the timestamp encoded in a product PNG filename, or None if the
    filename doesn't carry one (e.g. forecast step-indexed names)."""
    for rx, fmt in _FILENAME_PATTERNS:
        m = rx.search(name)
        if m:
            try:
                return datetime.strptime(m.group(1), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
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
