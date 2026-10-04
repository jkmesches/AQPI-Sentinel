"""XQPI — the FLOW radar (JPL), published to trinity.

Surveyed by the laptop session on 2026-10-03 and recorded in
``COSMOS/Documentation/16-xqpi-backend-survey.md`` §6; every value below comes
from that survey rather than from the AQPI tables, because almost none of the
AQPI numbers transfer.

What makes this a different shape from AQPI, not just different values:

  * There is no radarca. Nothing serves FLOW over HTTP, so the whole
    radarca-derived half of Sentinel (L0 site checks, L1 product scraping, L2
    radar status, L3 overlays) has nothing to talk to. ``checks/__init__``
    skips those modules under this profile.
  * Both unit variants are LIVE here. AQPI's ``mm`` trees stalled on
    2026-09-30 and are treated as discontinued; XQPI publishes ``in`` and
    ``mm`` together and both are current.
  * One radar, one published elevation. FLOW sweeps 2.5/3.5/4.5/5.5 under
    ScanPattern 21 but only 3.5 is ever imaged, so there is no tilt axis to
    render and no elevation selector to offer.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Radar
# --------------------------------------------------------------------------
# Pasadena, 0.39 km from JPL's campus — ~375 km outside the AQPI map extents,
# which is why XQPI needs its own georeferencing rather than inheriting any.
FLOW_LAT = 34.2048
FLOW_LON = -118.17081
FLOW_GATES = 675

RADAR_FOLDER = {"FLOW": "flow"}

# Elevations the radar sweeps vs the one it publishes. The gap is deliberate
# to record: a resolver takes elevation as a parameter with a single legal
# value, so the axis can appear later if publishing 1-of-4 turns out to be an
# oversight — but the UI must not offer a selector for a choice that does not
# exist. See 16-§5.2.
ELEVATIONS_SWEPT = (2.5, 3.5, 4.5, 5.5)
ELEVATION_PUBLISHED = 3.5

MOMENTS = (
    "Reflectivity",
    "Velocity",
    "DifferentialReflectivity",
    "DifferentialPhase",
)

# Raw volume arrival: 120/hour, p50 24 s, p90 50 s (16-§2). 1800 s gives the
# LB2 banding pass <=24 min / warn <=30 min / fail >30 min.
RADAR_SILENT_FAIL_S = {"FLOW": 1_800}

# --------------------------------------------------------------------------
# Published product families
# --------------------------------------------------------------------------
# Measured cadence is one 2-minute publisher cycle across every family, with a
# 14-frame rolling window: p50 2 min, p90 8 min, max 12 min (16-§2).
#
# `backend_max_age_s` is 1500 (25 min) — the FAIL threshold from 16-§7. The
# survey also proposes a 15-minute WARN, which LB1 cannot express: its
# B_freshness is a two-way pass/fail against one limit, unlike LB2's three-way
# banding. Raised with the laptop session; until LB1 grows a warn band the
# 25-minute fail is the whole of it.
_CYCLE_S = 120
_FAIL_AGE_S = 1_500

# `expected_steps` is NOT a window size. Measured 2026-10-04 00:00 UTC, all
# five manifests read 8 steps spanning 22:54 -> 23:54, with inter-step gaps of
# 14, 2, 10, 2, 2, 28, 2 minutes. The 2-minute cadence is real; the rest are
# frames that were never produced. The on-disk PNG count equalled the step
# count exactly, so nothing had been evicted — the deficit is missing
# production, not a rolling window turning over.
#
# So the step count measures PRODUCTION COMPLETENESS over whatever span the
# manifest covers, and the observed range is 8-15 (the earlier "14-15" was two
# samples that both happened to catch healthy operation). A count check keyed
# on `expected_steps` with a ±tolerance would therefore fire on exactly the
# gappy periods it would be worth reporting, but report them as a count error
# rather than as the gap they are. If an L1-shaped check is ever pointed here,
# the useful form is "largest gap in the declared span", not "step count
# within ±N" — and it is a different check from freshness, which only reads
# the newest step.
#
# It is also inert on this profile today: `expected_steps` is read by
# layer1_product (manifest step count), which does not register here. Kept so
# the value is right if an L1-shaped check is ever pointed at XQPI.
#
# Do NOT derive a frame count from the directory. qpe_15min/images/in/ holds
# 26 entries against a manifest of 14-15 — eleven orphans from 2026-09-03 that
# the window never reclaimed. The manifest is the truth; the directory is not.
_WINDOW_FRAMES = 14

# Each moment directory also holds one FLOW_<Moment>_colorbar.png, which is a
# legend and not a frame. It is written once at the day's first publish and
# never touched, so it can never be the newest file while frames arrive — but
# a freshness scan over those directories must still exclude it. The composite
# families below contain no stray entries at all, so they need no exclusion.
# See 16-§9.
COLORBAR_GLOB = "*colorbar*"

# Raw volumes arrive both compressed and not.
RAW_GLOBS = ("*.netcdf", "*.netcdf.gz")

# Published composites sit directly under PRODUCT_IMAGES/, undated, as a
# rolling window — NOT under K2's realtime/product_images/. Different case,
# different depth, no date partition. See 16-§1.
PRODUCT_IMAGES_PREFIX = ("PRODUCT_IMAGES",)

# FLOW's raw volumes are date-partitioned, the same shape as AQPI's CBAND tree
# but rooted inside the profile's own mount rather than a separate one.
# strftime template, relative to SETTINGS.backend_root, evaluated in UTC.
RADAR_DATED_TREE = {"FLOW": "flow/%Y/%m/%d"}

# --------------------------------------------------------------------------
# Freshness basis — DO NOT use mtime on this tree
# --------------------------------------------------------------------------
# A root gzip sweep walks the archive daily around 07:25 UTC and rewrites
# files, so mtime does not mean "when this observation happened". Verified
# directly on csu-aqpi 2026-10-03: the last file of 2026/09/25 and of
# 2026/09/30 both carry mtime 2026-10-03 01:25 local, the same instant five
# days apart, and the extension mix is 2,880 .gz against 4-9 .netcdf per day.
# The sweep touches the CURRENT day's directory too.
#
# The live directory also accumulates gzip's temporary files -- 14 of them on
# inspection, including three different suffixes for one source volume
# (`.flow-20261003-194148_..._PPI.netcdf.gz.4u4pxw`). They are dot-prefixed,
# they are the newest thing in the directory by mtime, and their names DO
# contain a parseable timestamp, so they must be excluded by the dot rather
# than by failing to parse.
#
# The asymmetry is what settles it: a sweep inflating mtime MASKS an outage,
# silently, in the one direction monitoring must never fail. A stale declared
# time at worst delays recovery and keeps alarming until current data lands.
# mtime is still fine for detecting change; it is not sound for asserting
# recency. See 16-§10 for the measurement that exposed it — mtime-derived
# intervals showed eight multi-hour outages whose magnitudes rose by exactly
# 1440 min/day going back, all artifacts of the single sweep instant.
LB1_FRESHNESS = "manifest"      # the per-step timestamp the manifest declares
LB2_FRESHNESS = "filename"      # flow-<YYYYMMDD>-<HHMMSS>_...

# Anchored at the start and excluding dotfiles upstream, so a gzip temp file
# cannot match even though its name embeds the same timestamp.
RAW_VOLUME_TS_RE = r"^flow-(\d{8})-(\d{6})_"

# The dated tree is partitioned by UTC date, not local: the first file of each
# day directory lands at 00:00:1x UTC (verified across 09/26, 09/30, 10/02).
# _radar_path formats in UTC, which is therefore correct — getting this wrong
# would look in the wrong directory for the six hours of MDT offset.

PRODUCTS = {
    # One manifest, no unit subdirectory.
    "composite_ref":     {"details": "composite_ref/details.json",
                          "image_dir": "composite_ref/images/",
                          "cadence_s": _CYCLE_S, "expected_steps": _WINDOW_FRAMES,
                          "max_freshness_s": _FAIL_AGE_S, "min_png_bytes": 5_000,
                          "backend_max_age_s": _FAIL_AGE_S,
                          "unit": "dBZ"},
    # details_in.json + details_mm.json over images/in/ and images/mm/.
    "qpe_15min":         {"details": "qpe_15min/details_in.json",
                          "image_dir": "qpe_15min/images/",
                          "cadence_s": _CYCLE_S, "expected_steps": _WINDOW_FRAMES,
                          "max_freshness_s": _FAIL_AGE_S, "min_png_bytes": 5_000,
                          "backend_max_age_s": _FAIL_AGE_S,
                          "unit": "in", "unit_subdir": True},
    "qpe_1hr":           {"details": "qpe_1hr/details_in.json",
                          "image_dir": "qpe_1hr/images/",
                          "cadence_s": _CYCLE_S, "expected_steps": _WINDOW_FRAMES,
                          "max_freshness_s": _FAIL_AGE_S, "min_png_bytes": 5_000,
                          "backend_max_age_s": _FAIL_AGE_S,
                          "unit": "in", "unit_subdir": True},
    "radar_precip_rate": {"details": "radar_precip_rate/details_in.json",
                          "image_dir": "radar_precip_rate/images/",
                          "cadence_s": _CYCLE_S, "expected_steps": _WINDOW_FRAMES,
                          "max_freshness_s": _FAIL_AGE_S, "min_png_bytes": 5_000,
                          "backend_max_age_s": _FAIL_AGE_S,
                          "unit": "in/h", "unit_subdir": True},
}

# Both live — do NOT carry over AQPI's discontinued-mm handling (16-§5.3).
UNITS = ("in", "mm")

# Everything XQPI reads is on trinity, so every backend check reports the same
# source. Two characters, by the constraint in Check.source_tag.
SOURCE_TAG = "TR"
SOURCE_LABEL = "Trinity"

# The radarca-derived check modules have nothing to talk to on this profile.
# checks/__init__ reads this rather than hard-coding the list at the import
# site, so adding a radarca-only module in future fails loudly here instead of
# registering checks against an origin that does not exist.
RADARCA_ONLY_MODULES = (
    "layer0_latency",
    "layer0_website",
    "layer1_product",
    "layer1_vector",
    "layer1_stream",
    "layer2_radar",
    "layer3_overlay",
    "layer4_image",
)
