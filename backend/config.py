"""Runtime settings + upstream-system knowledge.

Loaded from environment with `.env` interpolation. The ``SETTINGS`` instance
is created lazily on first import (read once). All defaults match
.env.example so a forgotten env var doesn't crash the app — only the DB URL
is required.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Auto-load .env from the project root (one level above backend/). Idempotent
# and safe to call multiple times. main.py also calls load_dotenv() — this
# call is a backstop for tooling/tests that import config directly.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    db_url: str
    base: str                # upstream system under monitoring
    data_dir: Path
    api_host: str
    api_port: int
    log_level: str
    archive_enabled: bool
    archive_root: Path
    archive_retention_days: int | None    # None == permanent
    cold_root: Path                       # offloaded CSV exports land here
    db_retention_days: int | None         # None == keep all rows in the hot DB
    retention_hour_utc: int               # daily sweep runs at this UTC hour
    admin_email: str                       # bootstrap admin (only used if no users exist)
    admin_password: str
    admin_display_name: str
    public_url: str                        # dashboard URL embedded in alert emails (empty = omit)
    prewarm_enabled: bool                  # background capture of every moment + tilt
    prewarm_interval_s: int                # seconds between sweeps of all streams
    prewarm_concurrency: int               # simultaneous in-flight prewarm fetches
    # Backend (filesystem) monitoring, stages LB1/LB2. None == disabled, and those
    # checks are then never registered. The shirejoe deployment has no VPN and mounts
    # neither K2 nor trinity, so it leaves both unset and runs an identical image with
    # no new checks at all. These are CONTAINER paths, not host paths.
    profile: str                           # "aqpi" (default) or "xqpi" — see backend/profiles
    backend_root: Path | None              # K2 web-files root, read-only bind mount
    sscb_root: Path | None                 # C-band tree on trinity, read-only


def _opt_path(var: str) -> Path | None:
    """Optional filesystem root. Empty or unset -> None, so callers can test truthiness
    without worrying about the empty-string case."""
    v = os.environ.get(var, "").strip()
    return Path(v) if v else None


def _load() -> Settings:
    db = os.environ.get("SENTINEL_DB_URL")
    if not db:
        raise RuntimeError(
            "SENTINEL_DB_URL is not set. Copy .env.example to .env or export "
            "it before launching."
        )
    data_dir = Path(os.environ.get("SENTINEL_DATA_DIR", "./data")).resolve()
    retention_env = os.environ.get("SENTINEL_ARCHIVE_RETENTION_DAYS", "").strip()
    retention = int(retention_env) if retention_env else None
    db_retention_env = os.environ.get("SENTINEL_DB_RETENTION_DAYS", "").strip()
    db_retention = int(db_retention_env) if db_retention_env else None
    return Settings(
        db_url=db,
        base=os.environ.get("SENTINEL_BASE", "https://radarca.engr.colostate.edu"),
        profile=os.environ.get("SENTINEL_PROFILE", "aqpi").strip().lower() or "aqpi",
        backend_root=_opt_path("SENTINEL_BACKEND_ROOT"),
        sscb_root=_opt_path("SENTINEL_SSCB_ROOT"),
        data_dir=data_dir,
        api_host=os.environ.get("SENTINEL_API_HOST", "127.0.0.1"),
        api_port=int(os.environ.get("SENTINEL_API_PORT", "8000")),
        log_level=os.environ.get("SENTINEL_LOG_LEVEL", "INFO"),
        # Prewarm is OFF by default. It is the only thing in Sentinel that
        # generates upstream traffic no operator asked for, and the increase is
        # not marginal: measured on the reference deployment it takes the
        # archive from ~4k images/day to ~49k, and adds sustained request load
        # against two origins (one of which, radarca, is someone else's slow
        # production research system). That is a decision for whoever runs the
        # instance, not a default they inherit.
        prewarm_enabled=os.environ.get("SENTINEL_PREWARM_ENABLED", "0") in ("1", "true", "yes"),
        # One sweep visits every stream once. 300 s keeps tilts complete: their
        # rolling window is 7 frames x ~140 s (~16 min), so a sweep every 5
        # minutes cannot miss a frame even if one sweep is skipped entirely.
        prewarm_interval_s=int(os.environ.get("SENTINEL_PREWARM_INTERVAL_S", "300")),
        prewarm_concurrency=int(os.environ.get("SENTINEL_PREWARM_CONCURRENCY", "3")),
        archive_enabled=os.environ.get("SENTINEL_ARCHIVE_ENABLED", "1") not in ("0", "false", "no"),
        archive_root=Path(os.environ.get("SENTINEL_ARCHIVE_ROOT", str(data_dir / "archive"))).resolve(),
        archive_retention_days=retention,
        cold_root=Path(os.environ.get("SENTINEL_COLD_ROOT", str(data_dir / "cold"))).resolve(),
        db_retention_days=db_retention,
        retention_hour_utc=int(os.environ.get("SENTINEL_RETENTION_HOUR_UTC", "9")),
        admin_email=os.environ.get("SENTINEL_ADMIN_EMAIL", ""),
        admin_password=os.environ.get("SENTINEL_ADMIN_PASSWORD", ""),
        admin_display_name=os.environ.get("SENTINEL_ADMIN_DISPLAY_NAME", ""),
        public_url=os.environ.get("SENTINEL_PUBLIC_URL", "").rstrip("/"),
    )


SETTINGS = _load()


# --------------------------------------------------------------------------
# Upstream knowledge (radarca.engr.colostate.edu — from the characterization).
# Mirrors validation_tests/config.py but lives here so backend code doesn't
# depend on the validation tree.
# --------------------------------------------------------------------------

# Per-product expectations.
PRODUCTS = {
    "qpe_15min":              {"details": "rain15min/details_in.json",
                               "image_dir": "rain15min/images/",
                               "cadence_s": 120, "expected_steps": 31,
                               "max_freshness_s": 360, "min_png_bytes": 5_000,
                               "unit": "in", "unit_subdir": True},
    "qpe_1hr":                {"details": "rain60min/details_in.json",
                               "image_dir": "rain60min/images/",
                               "cadence_s": 120, "expected_steps": 31,
                               "max_freshness_s": 360, "min_png_bytes": 5_000,
                               "unit": "in", "unit_subdir": True},
    "precip_rate_radar":      {"details": "rainrate/details_in.json",
                               "image_dir": "rainrate/images/",
                               "cadence_s": 120, "expected_steps": 31,
                               "max_freshness_s": 360, "min_png_bytes": 5_000,
                               "unit": "in/h", "unit_subdir": True},
    "comp_ref":               {"details": "composite_ref_max/details.json",
                               "image_dir": "composite_ref_max/images/",
                               "cadence_s": 120, "expected_steps": 31,
                               "max_freshness_s": 360, "min_png_bytes": 5_000,
                               "unit": "dBZ"},
    # max_freshness_s was -3000, which is exactly where a healthy cycle ends,
    # so the threshold had approximately zero slack against the publish
    # interval and a single late cycle failed the check.
    #
    # Measured over 7 days to 2026-09-15, with the 2026-09-14 01:26-15:32
    # stall excluded so the tail is churn and not one real outage
    # (n=9,200 samples of age_s):
    #
    #     p50 -3227   p90 -3078   p99 -2750   p99.5 -2122   p99.9 -11
    #
    # A cycle publishes at age ~-3360 and drifts to ~-3000 over about six
    # minutes, then republishes. One missed publish therefore lands near
    # -2640 and two near -2280, so -2400 tolerates exactly one missed cycle
    # and catches the second. Share of samples failing: 1.99% at -3000,
    # 0.62% at -2400 — the removed two-thirds are single-cycle lateness,
    # which produced six pass/fail flips in the 00:00-01:00 UTC hour on
    # 2026-09-14 alone. That churn is the reason the real stall an hour
    # later was bulk-acknowledged at 04:00 and then ran for another eleven
    # hours unnoticed; a threshold that cries wolf is a threshold that gets
    # acked reflexively.
    #
    # Detection of a genuine stall is ~10 minutes later than before, which
    # is the whole cost, against a stall that lasted 14h 06m.
    "comp_now":               {"details": "composite_nowcast/details.json",
                               "image_dir": "composite_nowcast/images/",
                               "cadence_s": 120, "expected_steps": 31,
                               "max_freshness_s": -2400, "min_png_bytes": 5_000,
                               "unit": "dBZ"},
    # Forecast products: two non-obvious calibrations.
    # 1. `min_png_bytes` lowered to 1500 — these products legitimately
    #    produce sub-5KB PNGs when no precip is predicted (most of a clear
    #    day). 1500 catches a truly-corrupt zero-byte response without
    #    flagging the clear-sky case.
    # 2. `expected_steps: None` disables E_step_count entirely — the
    #    upstream model adjusts its forecast horizon hour-to-hour
    #    (observed range: 19-130 steps in a single week for fcst_temp).
    #    No fixed tolerance makes sense; the check is skipped per-row.
    "fcst_total_precip":      {"details": "total_precip/details_in.json",
                               "image_dir": "total_precip/images/",
                               "cadence_s": 3600, "expected_steps": None,
                               "max_freshness_s": 7200, "min_png_bytes": 1_500,
                               "unit": "in", "unit_subdir": True},
    "fcst_total_precip_cum":  {"details": "total_precip_cumulative/details_in.json",
                               "image_dir": "total_precip_cumulative/images/",
                               "cadence_s": 3600, "expected_steps": None,
                               "max_freshness_s": 7200, "min_png_bytes": 1_500,
                               "unit": "in", "unit_subdir": True},
    "fcst_precip_rate":       {"details": "precip_rate/details_in.json",
                               "image_dir": "precip_rate/images/",
                               "cadence_s": 900, "expected_steps": None,
                               "max_freshness_s": 7200, "min_png_bytes": 1_500,
                               "unit": "in/h", "unit_subdir": True},
    "fcst_temp":              {"details": "temperature/details_F.json",
                               "image_dir": "temperature/images/",
                               "cadence_s": 3600, "expected_steps": None,
                               "max_freshness_s": 7200, "min_png_bytes": 5_000,
                               "unit": "F", "unit_subdir": True},
    "water_level":            {"details": "water_level/details.json",
                               "image_dir": "water_level/images/",
                               "cadence_s": 3600, "expected_steps": 19,
                               "max_freshness_s": -3600, "min_png_bytes": 5_000,
                               "unit": "ft"},
    "water_depth":            {"details": "water_depth/details.json",
                               "image_dir": "water_depth/images/",
                               "cadence_s": 3600, "expected_steps": 19,
                               "max_freshness_s": -3600, "min_png_bytes": 5_000,
                               "unit": "ft"},
    "max_water_level":        {"details": "max_water_level/details.json",
                               "image_dir": "max_water_level/images/",
                               "cadence_s": None, "expected_steps": 1,
                               "max_freshness_s": 90_000, "min_png_bytes": 5_000,
                               "unit": "ft"},
    "max_water_depth":        {"details": "max_water_depth/details.json",
                               "image_dir": "max_water_depth/images/",
                               "cadence_s": None, "expected_steps": 1,
                               "max_freshness_s": 90_000, "min_png_bytes": 5_000,
                               "unit": "ft"},
}

# Radar id → on-disk folder slug (from the JS bundle's `C` map).
RADAR_FOLDER = {
    "XEBY":  "ebay",
    "XSCV":  "scvw",
    "XSCW":  "scwa",
    "XSCR":  "scrz",
    "XSWR":  "swyr",
    "CBAND": "sscb",
}
# Radar-status API uses these aliases for the same radars.
STATUS_TO_RADAR = {"EBAY": "XEBY", "CBand": "CBAND"}

# Per-radar GHOST_UP detection threshold: max wall-clock seconds since the
# newest image filename's timestamp before we treat the radar as silent.
# Values are ~1.5× each radar's observed worst-case gap, with a 4-min floor
# (no point firing on a single missed scan when the cadence is tight).
#
# Methodology: sample /api/xbandRadarImages/ for each radar, take the max
# observed inter-scan gap over the rolling ~1 h window, round up to the
# nearest minute. Re-tune periodically; cadences drift.
#
# Last calibrated 2026-05-17 — see PR audit, gap distributions were:
#   XEBY  median 180s, max 180s  → 300s   (very regular 3-min cadence)
#   XSCW  median 120s, max 480s  → 720s   (mostly 2-min, occasional 8-min)
#   XSCR  median 120s, max 180s  → 300s   (mostly 2-min, occasional 3-min)
#   XSWR  median 120s, max 120s  → 240s   (perfect 2-min cadence)
#   CBAND median 240s, max 300s  → 600s   (variable; bumped from 480s
#                                            after operator-observed
#                                            bouncing — 10 min is more
#                                            comfortable for this radar)
#   XSCV  (down at calibration)  → 600s   (X-band default)
#
# Default for any radar not listed: 600s.
# Recalibrated 2026-08-26. THE CHECK GATES ON PUBLISHED-IMAGE AGE, NOT SCAN
# CADENCE — that distinction is the whole reason these values changed.
#
# `age = now - newest_published_timestamp` includes upstream's PUBLICATION
# LAG, which on this fleet runs ~300-500s. The May values were derived from
# inter-scan gaps (~120s) and so were impossible to satisfy: XSWR scans every
# 120s and delivers ~27 images per poll, yet reported GHOST_UP on 96% of runs
# in the 24h to 2026-08-26 because its 240s threshold sat below the
# publication lag alone.
#
# Basis: 1.5 x the observed p99 of the `primary_age_s` metric over 24h of
# healthy operation, cross-checked against `python -m backend.tune_silent_fail`
# (which now accounts for lag) and rounded up to the minute. Where the two
# disagreed the more conservative — larger — value was taken, because a
# too-tight threshold produces constant false GHOST_UP while a slightly loose
# one only delays detection of a frozen feed.
#
#   radar   age_p99(24h)   tool rec   chosen    was
#   XSCV       425 s         600      660       600
#   XSCW       478 s         540      720       720   (tool's 540 < observed max 596)
#   XSCR       n/a*          780      780       300
#   XSWR       429 s         600      660       240
#   CBAND      717 s         780     1080       600   (tool's 780 < 1.5 x p99)
#   XEBY       no scans       —       300       300   (declared DOWN upstream)
#
#   * XSCR's age percentiles are contaminated by genuine outages (only 79 of
#     582 runs had any images), so the tool's lag-based figure is used.
#
# SAFETY: raising these cannot hide an outage. A radar publishing NO images is
# marked not-fresh regardless of threshold (`elif primary_n == 0` in
# layer2_radar), and 5,042 of XSWR's 7,950 GHOST_UPs over 14 days were exactly
# that. Thresholds only govern the "images present but stale" (frozen feed)
# case, where the cost is detection latency: XSWR 240s -> 660s, i.e. a frozen
# feed is caught in 11 minutes instead of 4.
#
# Re-tune monthly with `python -m backend.tune_silent_fail`.
RADAR_SILENT_FAIL_S = {
    "XEBY":  300,
    "XSCV":  660,
    "XSCW":  720,
    "XSCR":  780,
    "XSWR":  660,
    "CBAND": 1080,
}

# Moment name → productPrefix mapping (the JS bundle's `j` object + L's CBAND
# exception).
MOMENT_TO_PREFIX_DEFAULT = {
    "Reflectivity":              "CorrReflectivity",
    "Differential Reflectivity": "CorrDifferentialReflectivity",
    "Velocity":                  "Velocity",
    "PhiDP":                     "FilteredPhiDP",
    "RhoHV":                     "RhoHV",
}


def moment_to_prefix(radar_id: str, moment: str) -> str:
    if radar_id == "CBAND" and moment == "PhiDP":
        return "PhiDP"
    return MOMENT_TO_PREFIX_DEFAULT[moment]


X_MOMENTS = list(MOMENT_TO_PREFIX_DEFAULT.keys())

# Temperature uses "F" or "Deg" (note: NOT "DEG") for its subdir.
TEMP_UNIT_SUBDIR = {"F": "F", "DEG": "Deg"}


# --------------------------------------------------------------------------
# Layer 4 image-QC profiles — per-product knobs for the heuristics.
#
# Why this exists: the default thresholds were tuned for radar imagery,
# where a saturated frame or a frame identical to the previous one is a
# real anomaly. Several forecast/model products instead encode scalar
# fields (water depth, accumulated precip, temperature) with thresholded
# color ramps that legitimately cluster pixels at color endpoints, and
# update on a much slower cadence than our 120s poll — so the default
# verdicts fire on normal operation. Override here per product.
# --------------------------------------------------------------------------
DEFAULT_L4_PROFILE: dict[str, object] = {
    "extreme_threshold": 0.40,
    # FROZEN means the pHash matches the previous run exactly. We always
    # suppress it on a low-coverage frame (a radar staring at clutter on a
    # quiet day is going to produce identical frames — that's not a stuck
    # feed, it's a calm world). `skip_frozen=True` suppresses FROZEN even
    # when coverage is high: appropriate for forecast products whose
    # cadence is slower than the L4 check cadence.
    "skip_frozen":         False,
    # Coverage floor (percent of pixels with data) below which FROZEN
    # always demotes to QUIET regardless of profile.
    "frozen_min_cov_pct":  5.0,
    # Skip the polar range-ring artifact detector. Set True for radars
    # whose image geometry doesn't match the X-band assumptions the
    # detector was tuned against (CBAND's 1800×1800 vs X-band's
    # ~700×700, plus different field-of-view + physics). All other Tier
    # 1 + Tier 2 stats are scale-invariant and apply cleanly.
    "skip_range_ring":     False,
}

L4_PROFILES: dict[str, dict[str, object]] = {
    # Hydrology forecasts — thresholded color ramps, hourly cadence.
    "water_depth":     {"extreme_threshold": 0.85, "skip_frozen": True},
    "max_water_depth": {"extreme_threshold": 0.85, "skip_frozen": True},
    "water_level":     {"extreme_threshold": 0.85, "skip_frozen": True},
    "max_water_level": {"extreme_threshold": 0.85, "skip_frozen": True},
    # Nowcast composite — blank when no precip, identical frames between
    # ~2-min model runs are expected.
    "comp_now":        {"extreme_threshold": 0.60, "skip_frozen": True},
    # Mosaic composite reflectivity is the only L4 mosaic product where
    # FROZEN is meaningful (it doesn't skip frozen). Raise the low-coverage
    # threshold from 5% (default) to 10% — a 7-day audit showed 97 FROZEN
    # warns concentrated in a single calm 6-hour window where coverage was
    # 5-9% and the field was legitimately static. Lifting the threshold
    # silences "quiet weather" without missing real stuck-feed conditions
    # (which typically lock at much higher coverage).
    "comp_ref":        {"frozen_min_cov_pct": 10.0},
    # CBAND L4 — image geometry differs from X-band (1800×1800 vs
    # ~700×700, wider field of view, different physics). Coverage,
    # extreme, speckle, and frozen detection are scale-invariant and
    # apply cleanly. The polar range_ring detector was tuned for X-band
    # geometry and would false-fire on CBAND, so we suppress it here.
    "CBAND":           {"skip_range_ring": True},
}


def l4_profile(identifier: str) -> dict[str, object]:
    """Merge any per-product overrides over the defaults."""
    return {**DEFAULT_L4_PROFILE, **L4_PROFILES.get(identifier, {})}


def image_path(product_id: str, image_name: str) -> str:
    """Mirror the JS: unit-subdir products interpolate <images_dir>/<unit>/<name>."""
    cfg = PRODUCTS[product_id]
    if not cfg.get("unit_subdir"):
        return cfg["image_dir"] + image_name
    unit = cfg["unit"]
    if product_id == "fcst_temp":
        return cfg["image_dir"] + TEMP_UNIT_SUBDIR.get(unit, "F") + "/" + image_name
    return cfg["image_dir"] + unit.split("/")[0] + "/" + image_name


# ==========================================================================
# Deployment profile
# ==========================================================================
#
# Everything above describes AQPI. A second deployment — XQPI, monitoring the
# FLOW radar on trinity — watches a different radar and a different product
# set, so it swaps these tables wholesale rather than adding to them.
#
# Rebinding here rather than branching at every use site: `PRODUCTS`,
# `RADAR_FOLDER` and `RADAR_SILENT_FAIL_S` are imported by name across a dozen
# modules, and the helpers below (`image_path`, `l4_profile`) read the module
# globals at CALL time. So one rebind, before any check module imports this,
# reaches all of them. A fork would have to be kept in sync by hand across
# every check, label and threshold.
#
# Profile modules are pure data and must not import this module — the
# dependency runs one way only.
#
# `BACKEND_SOURCE` is the (tag, label) a backend-reading check reports when it
# has no more specific answer. AQPI publishes to K2; XQPI publishes to
# trinity. Hardcoding "K2" in the check modules was right for one profile and
# silently wrong for the other — the same class of error as labelling CBAND's
# row "K2" before v0.5.6.
BACKEND_SOURCE: tuple[str, str] = ("K2", "K2")

# Does this deployment have a radarca-style HTTP display tier in front of the
# data? AQPI does, and most of the check stack is built on scraping it. XQPI
# does not: FLOW publishes to a filesystem tree and nothing serves it over
# HTTP. Phrased as a capability rather than `profile == "xqpi"` so a third
# profile has to state its answer instead of inheriting one by omission —
# which is how a check that can only ever fail gets shipped.
HAS_RADARCA: bool = True

# Human-facing name for this deployment. The frontend had "AQPI Sentinel"
# hard-coded in four places (the page title, the FAQ heading, the mobile
# "more" page's data-source line and the alert-email from_name), so an XQPI
# instance announced itself as AQPI and its alert emails would have been
# signed as AQPI. Served over /api/version so there is one source of truth
# rather than a fifth table that knows the answer independently.
SITE_NAME: str = "AQPI Sentinel"

# Radar map geography, when the profile supplies its own. Empty means "use the
# AQPI table in api/routes/radars.py", which stays where it is because it
# carries a long provenance comment per radar and is not profile data.
#
# /api/radars/meta returned those nine hard-coded Bay Area radars on EVERY
# profile, with folder=None throughout and no FLOW at all — so the frontend
# had nothing to place, which is upstream of any map-extent work.
RADAR_META_OVERRIDE: dict[str, dict] = {}

# Where the map opens, when the profile wants somewhere other than the Bay
# Area. Empty means "keep MapView's own tuned constants", which is what every
# AQPI deployment has always used.
HOME_VIEW: dict = {}

# Regional map furniture this deployment has data for. All three AQPI layers
# are Northern California — the static files are literally named
# `watersheds-huc8-norcal.geojson` and `reservoirs-norcal.json`, and
# stream_gauges is served through radarca. On a profile watching a radar in
# Pasadena they would draw Bay Area geography 375 km off-screen, or fail, while
# still offering a toggle that appears to do nothing. A named list rather than
# a boolean so a future deployment can have some and not others.
MAP_OVERLAYS: tuple[str, ...] = ("watersheds", "reservoirs", "stream_gauges")

# Check modules that exist only because radarca does — every one of them talks
# to SETTINGS.base. Canonical here rather than in checks/__init__ because
# backend.registry needs it too and cannot import that package without a
# cycle, and because it is the same list on every profile: it describes which
# modules need an HTTP origin, not which network is being watched.
#
# Two things consume it. checks/__init__ skips importing these on a profile
# with no origin, and registry.register() REFUSES a check defined in one of
# them. The second is what actually makes it safe: the import skip only works
# while nothing else imports the module, and prewarm.py imports
# layer2_radar at module scope for EXPECTED_ABSENT_MOMENTS, which silently
# re-registered two permanently-failing L2 checks on the xqpi profile.
RADARCA_ONLY_MODULES: tuple[str, ...] = (
    "layer0_latency",          # 1 latency canary — probes SETTINGS.base directly
    "layer0_website",          # 4 L0 checks
    "layer1_product",          # 13 product checks (L1 + L3B parity inline)
    "layer1_vector",           # 3 static-asset checks
    "layer1_stream",           # 1 stream-canary check
    "layer2_radar",            # 6 per-radar checks + 1 fleet correlation
    "layer3_overlay",          # 1 Playwright overlay parity check
    "layer4_image",            # 5 X-band + 3 mosaic image checks
)

# Published-image tree layout, relative to SETTINGS.backend_root. K2 nests the
# products under realtime/product_images/; trinity's XQPI tree puts them
# directly under PRODUCT_IMAGES/. Getting this wrong does not raise -- LB1
# reports "product directory does not exist" for every product, which reads
# like an outage rather than a misconfiguration.
PRODUCT_IMAGES_PREFIX: tuple[str, ...] = ("realtime", "product_images")

# Radars whose volumes land in a date-partitioned tree under backend_root, as a
# strftime template evaluated in UTC. AQPI's are flat under PRODUCTS/DROPS and
# so declare nothing here; CBAND is dated but lives on a separate mount and is
# handled by SENTINEL_SSCB_ROOT instead. See layer2_backend_radar._radar_path.
RADAR_DATED_TREE: dict[str, str] = {}

# How a backend check decides HOW FRESH the data is. This is a property of the
# published tree, not a preference, so it is declared per profile.
#
# AQPI stays on mtime, deliberately. Verified on csu-aqpi 2026-10-03: every
# sampled K2 product directory has newest-by-mtime == newest-by-filename, no
# dotfiles and no compression pass, so mtime there does mean what it says. And
# AQPI's DROPS tree is not uniform — ebay holds flat `.drops` files while scvw
# holds a nested `2026/` directory — so the directory's own mtime is the only
# basis that works across all six radars. Changing it would be a behavior
# change on a live deployment (cira-aqpi, v0.5.6) to fix a defect that tree
# does not have.
#
# XQPI must NOT use mtime: a daily gzip sweep rewrites its archive and masks
# outages. See backend/profiles/xqpi.py for the evidence.
LB1_FRESHNESS: str = "newest_mtime"   # newest file's mtime in the images dir
LB2_FRESHNESS: str = "dir_mtime"      # the data directory's own mtime
RAW_VOLUME_TS_RE: str | None = None   # only used by LB2_FRESHNESS="filename"

if SETTINGS.profile == "xqpi":
    from .profiles import xqpi as _xqpi       # noqa: E402

    PRODUCTS = _xqpi.PRODUCTS
    RADAR_FOLDER = _xqpi.RADAR_FOLDER
    RADAR_SILENT_FAIL_S = _xqpi.RADAR_SILENT_FAIL_S
    BACKEND_SOURCE = (_xqpi.SOURCE_TAG, _xqpi.SOURCE_LABEL)
    PRODUCT_IMAGES_PREFIX = _xqpi.PRODUCT_IMAGES_PREFIX
    RADAR_DATED_TREE = _xqpi.RADAR_DATED_TREE
    LB1_FRESHNESS = _xqpi.LB1_FRESHNESS
    LB2_FRESHNESS = _xqpi.LB2_FRESHNESS
    RAW_VOLUME_TS_RE = _xqpi.RAW_VOLUME_TS_RE
    SITE_NAME = _xqpi.SITE_NAME
    RADAR_META_OVERRIDE = _xqpi.RADAR_META
    HOME_VIEW = _xqpi.HOME_VIEW
    MAP_OVERLAYS = _xqpi.MAP_OVERLAYS
    HAS_RADARCA = False
elif SETTINGS.profile != "aqpi":
    # Fail loudly. A typo here would otherwise start a Sentinel that silently
    # monitors the wrong radar network, which is worse than not starting.
    raise ValueError(
        f"unknown SENTINEL_PROFILE {SETTINGS.profile!r} — expected 'aqpi' or 'xqpi'"
    )
