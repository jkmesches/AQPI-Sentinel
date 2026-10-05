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
# --------------------------------------------------------------------------
# PROVENANCE
# --------------------------------------------------------------------------
# Every figure below is tagged, because a borrowed number and a measured one
# look identical once they are both constants in a file — and that is not
# hypothetical here. A "max 12 min" quoted from the survey sat thirty lines
# from a 28-minute gap measured directly, in this file, and the two were never
# read against each other.
#
#   [M] measured here, against the live tree or the files' own headers.
#   [V] taken from the survey AND independently reproduced here.
#   [Q] taken from the survey and NOT independently checked. Treat as a
#       starting value, not as evidence. Every [Q] below is a threshold that
#       gates an alarm, which is the class worth knowing you are trusting.
#
# The rule this encodes: a number from someone else's document is not evidence
# until something measured here has reproduced or contradicted it.
from __future__ import annotations

# --------------------------------------------------------------------------
# Radar
# --------------------------------------------------------------------------
# Pasadena, 0.39 km from JPL's campus — ~375 km outside the AQPI map extents,
# which is why XQPI needs its own georeferencing rather than inheriting any.
FLOW_LAT = 34.2048        # [M] volume globals, Latitude
FLOW_LON = -118.17081     # [M] volume globals, Longitude
FLOW_GATES = 675          # [M] volume globals, NumGates

ELEVATIONS_SWEPT = (2.5, 3.5, 4.5, 5.5)   # [V] all four seen in raw filenames
ELEVATION_PUBLISHED = 3.5

RADAR_FOLDER = {"FLOW": "flow"}

# Map geography, same shape as the AQPI table in api/routes/radars.py. Every
# figure here is read from FLOW's own volume headers rather than sourced
# second-hand, which makes it the best-attested entry in either table.
#
# BAND: X. TxFrequency 9.3993 GHz (AfcFrequency agrees), so lambda = 3.19 cm —
# X-band is 8-12 GHz, C-band 4-8, and it is not near the boundary. Confirmed
# independently by the antenna: AntennaBeamwidth 1.4 deg implies a 1.59 m dish
# at this wavelength and AntennaGain 42.0 dB implies 1.65 m, agreeing within
# 4%, which they only do at X. The same 1.4 deg beam at C-band would need a
# 2.68 m dish. Worth recording because "FLOW" carries no band in its name,
# unlike AQPI's X-prefixed fleet.
#
# RANGE: derived, not nominal. GateWidth and StartRange are per-radial
# VARIABLES in millimetres, not global attributes, which is why a header dump
# does not show them:
#     StartRange -113.657 m + 675 gates x 59.941 m = 40,346.5 m
# GateWidth is a processing constant — bit-identical across every radial and
# across volumes 40 minutes apart — so this is a fixed instrument geometry,
# not a measurement that drifts. It agrees with the 40 km nominal class of
# AQPI's five X-bands to 0.9%, which is an independent check on the
# arithmetic; the other five entries are vendor-nominal MaxRange figures,
# while this one is the real last-gate range, so it is kept rather than
# rounded to match its neighbours. The 346 m difference is invisible as a ring.
# The map's home view: where it opens, and where Reset returns to.
#
# Explicit profile data rather than derived from RADAR_META, because deriving
# it would move AQPI. Fitting AQPI's nine radars plus their rings puts the
# centre near 38.5N 121.9W — the three NEXRADs at 100 km drag it ~85 km
# north-east of the tuned [-122.6, 37.95] the map has always opened at, and
# changing a live deployment's startup view is not a side effect worth having.
#
# Zoom 9.5 frames FLOW's 40.35 km ring: at latitude 34.2 that is
# 156543*cos(lat)/2^9.5 = 179 m/px, so the 80.7 km ring spans ~450 px — about
# half the height of a typical map pane, leaving surrounding context visible.
# One radar, so there is nothing to fit between; the ring IS the extent.
HOME_VIEW = {"center": [FLOW_LON, FLOW_LAT], "zoom": 9.5}  # [M] computed here

# No regional overlays. AQPI's watersheds, reservoirs and stream gauges are all
# Northern California datasets — two static NorCal files and one radarca-served
# feed — none of which describes the ground under FLOW. Offering the toggles
# anyway would draw Bay Area geography far off-screen or silently fail, and a
# control that does nothing is worse than an absent one. Southern California
# equivalents can be added here when they exist.
MAP_OVERLAYS: tuple[str, ...] = ()

# --------------------------------------------------------------------------
# Composite overlay bounds — PER PRODUCT, because the families do not share one
# --------------------------------------------------------------------------
# composite_ref is SOURCED, from the composite grid's own CRS:
# PRODUCTS/Composite_QPE/tmp_SRI/COMP_*.nc carries
# PROJCS["WGS 84 / UTM zone 11N"] with explicit coordinate arrays — x 936 by
# y 760 cells on 250 m spacing, so 234.0 x 190.0 km, and
# RadarFilesInComposite names FLOW alone.
#
# Converting the cell EDGES (centres +/- 125 m) through an inverse transverse
# Mercator, independently on both sides and agreeing to sub-metre:
#
#     SW 33.28770 / -119.51279      NW 34.99957 / -119.56403
#     SE 33.31312 / -117.00000      NE 35.02666 / -117.00000
#
# Two things say this is the right domain rather than a plausible one. The grid
# aspect 936/760 = 1.2316 matches the PNG's 1365/1108 = 1.2319 to 0.02%. And
# the east edge falls on exactly -117.00000 because x_max is exactly 500000,
# the zone's false easting — the domain is pinned to the central meridian,
# which is not something a coincidence does.
#
# This replaces a guess. The first shipped value was the bounding box of FLOW's
# 40.3 km range ring, on the assumption that a one-radar composite covers that
# radar's coverage. It does not: the real domain is ~2.9x wider and ~2.4x
# taller, with FLOW centred at 53.9% x 52.4% and its ring spanning 34.5% of the
# width. The regional-domain reading was right and the per-radar one was wrong.
#
# CAVEAT, and it is a real error rather than rounding: a UTM-aligned grid is a
# TRAPEZOID in lat/lon, not a rectangle. The west edge runs -119.51279 at the
# south to -119.56403 at the north, about 4.7 km of skew. MapView places
# overlays as axis-aligned lat/lon rectangles, so that skew is baked into the
# corners and the image sits very slightly rotated against the basemap.
# Acceptable over 234 km; noted so the next person does not chase it as a bug.
#
# The three QPE families deliberately have NO entry. Their PNGs are 1697x2310
# (aspect 0.7346) against composite_ref's 1365x1108 (1.2319) — a portrait
# render of what is underneath the same landscape 936x760 grid, so the
# renderer is cropping, padding or adding furniture, and which of those cannot
# be read off the pixel dimensions. Applying composite_ref's box to them would
# place them with confident-looking numbers taken from a different product's
# geometry, which is exactly the failure this is avoiding. They are omitted
# from the picker until their geometry is known.
COMP_EXTENT = {   # [M] both sides derived it independently, agreeing sub-metre
    "composite_ref": {"west": -119.5640, "east": -117.0000,
                      "south": 33.2877, "north": 35.0267},
}

# Product ids whose extent is assumed rather than sourced. Empty: the one
# extent above is derived from the grid's own CRS.
COMP_EXTENT_PROVISIONAL: tuple[str, ...] = ()

# Picker labels. The frontend's own table is keyed on AQPI's product ids and
# has no entry for any of these.
PRODUCT_LABELS = {
    "composite_ref":     "Reflectivity",
    "qpe_15min":         "Total Precip · 15 min QPE",
    "qpe_1hr":           "Total Precip · 1 h QPE",
    "radar_precip_rate": "Precip Rate",
}
RADAR_META = {
    "FLOW": {"lat": FLOW_LAT, "lon": FLOW_LON, "range_m": 40_346,
             "kind": "xband",   # [M] TxFrequency 9.3993 GHz
             "name": "FLOW (JPL)",
             # What the radar SWEEPS. Only 3.5 is ever imaged (see
             # ELEVATION_PUBLISHED) — the imagery resolver stays pinned there,
             # and the UI must not offer a selector for a choice that does not
             # exist. Listed because the sweep is a property of the radar.
             "elevations": list(ELEVATIONS_SWEPT)},
}

# Elevations the radar sweeps vs the one it publishes. The gap is deliberate
# to record: a resolver takes elevation as a parameter with a single legal
# value, so the axis can appear later if publishing 1-of-4 turns out to be an
# oversight — but the UI must not offer a selector for a choice that does not
# exist. See 16-§5.2.

MOMENTS = (
    "Reflectivity",
    "Velocity",
    "DifferentialReflectivity",
    "DifferentialPhase",
)

# Raw volume arrival: 120/hour, p50 24 s, p90 50 s (16-§2). 1800 s gives the
# LB2 banding pass <=24 min / warn <=30 min / fail >30 min.
# [V] Reproduced here over 193 days: 496,022 distinct arrival times parsed
# from raw filenames across the dated tree (dotfiles excluded). p50 0.40 min,
# p90 0.83 min, p99 0.85 min — the four elevations of a volume arrive ~24 s
# apart, so these are per-sweep.
#
# 1800 s sits in dead space, which is the property that matters rather than
# the value: the largest normal gap is 25.7 min and the smallest outage is
# 30.8 min, with nothing in between. 103 gaps of 496,021 exceed it.
#
# Margin is asymmetric and worth knowing: 4.3 min of room below (to the
# largest normal gap) but only 0.8 min above (to the smallest observed
# outage). So the exposure is false NEGATIVES — a 29-minute gap would pass
# while being well outside anything normal. Tightening buys little, since the
# distribution is empty there; it is a gap in coverage, not a defect.
RADAR_SILENT_FAIL_S = {"FLOW": 1_800}

# --------------------------------------------------------------------------
# Published product families
# --------------------------------------------------------------------------
# One 2-minute publisher cycle across every family: p50 2 min, p90 8 min
# (16-§2). Those two hold and are what `cadence_s` encodes.
#
# The same table's "max 12 min" and "14-frame window" do NOT hold, and both
# were quoted here before being checked:
#
#   * the max was an artifact of globbing the images directory, which picks up
#     eleven 2026-09-03 orphans the window never reclaims; the real span for
#     the `in` unit is 43,184 min, i.e. a 29.9-day jump onto stale files. A
#     percentile is unmoved by one outlier, which is why p50 and p90 survive
#     and the max does not.
#   * the frame count is not a window size at all — see `expected_steps` below.
#
# Worth recording how this got through: the gap measurement thirty lines down
# observed 28 minutes between consecutive steps, in this same file, and I did
# not reconcile it against the 12 I had quoted from the survey. A borrowed
# figure and a first-hand one disagreeing by more than 2x should have been the
# cheapest catch of the lot. The lesson is not to re-read harder — it is that a
# number taken from someone else's document is not evidence until it has been
# reproduced or contradicted by something measured here.
#
# `backend_max_age_s` is 1500 (25 min) — the FAIL threshold from 16-§7. The
# survey also proposes a 15-minute WARN, which LB1 cannot express: its
# B_freshness is a two-way pass/fail against one limit, unlike LB2's three-way
# banding. Raised with the laptop session; until LB1 grows a warn band the
# 25-minute fail is the whole of it.
_CYCLE_S = 120        # [V] 2.0-min gaps observed between healthy steps

# Floor for G_image_size, per product. Was 5000 carried over from AQPI and
# tagged [Q]; now measured, and lowered on the measurement.
#
# WHAT IT IS FOR. Catching a truncated or empty write, not judging content. A
# truncated PNG is hundreds of bytes; a legitimately EMPTY frame is not small
# at all here — composite_ref's blank frames are a stable 8496 bytes, because
# a fully transparent raster still carries its full IHDR and a compressed
# all-zero image. So the floor wants to sit far below the smallest legitimate
# frame rather than just under it.
#
# MEASURED on the four families this key actually applies to:
#     composite_ref      8,496 - 8,686 B
#     qpe_15min/1hr/pr  20,563 - 22,028 B
# 5000 would have passed all of them, so it was not wrong — it was
# under-motivated, with only 1.7x headroom under composite_ref's blank. 2000
# is set from the failure it exists to catch instead, and keeps ~4x.
#
# DO NOT REUSE THIS FOR THE MOMENT TREE. Byte floors scale with pixel count
# and the moment frames are 697x693 against composite_ref's 1365x1108 and the
# QPE families' 1697x2310 — an 8x difference. Measured on the moment tree,
# Reflectivity runs 3,920-5,971 B on an active day and 4,431-5,625 B on a
# quiet one, so a 5000 floor would fail 45% of frames on the first and 95% on
# the second. Frame size tracks echo content, so any floor there must survive
# the clearest conditions the radar will ever see, and a dry winter week will
# be smaller than anything measured yet. When an image check is pointed at
# that tree it needs its own floor, around 2000 for the same truncation
# reason — not this one.
#
# The same recalibration already happened on AQPI: its four forecast products
# went 5000 -> 1500, needing a one-shot reprocess of stored verdicts
# (backend/reprocess_l1_forecasts.py). Nothing on this profile reads
# min_png_bytes yet — layer1_product does not register without an HTTP origin
# — so changing it now costs nothing, where changing it after an image check
# ships would cost that same reprocess.
_MIN_PNG = 2_000      # [V] measured against all four families
# AVAILABILITY, for whoever sizes expectations. Measured over ONE 6-day window
# so the layers are comparable — figures from different spans are not, and
# reading a 6-day product number against a 193-day ingest number makes the
# product layer look twice as bad as it is:
#
#     raw volume ingest    17.3% unavailable   (24.84 h of 144.0 h)
#     composite products   22.5% unavailable   (32.40 h of 144.0 h)
#
# The gap is 5.2 points, not a doubling. And the dominant outage is SHARED
# rather than independent: raw's largest gap is 647 min against the
# composites' 672 min — essentially one event propagating from ingest to
# products. The products do also fail on their own (a 131-minute QPE stall was
# observed while volumes landed every 2 minutes), but that mechanism accounts
# for the 5.2 points, not the bulk of the downtime.
#
# Over its own 193 days the ingest tree runs 10.6% unavailable, so this window
# was worse than typical for both layers.

# [V] Reproduced here over 6 days from the composite source tree,
# PRODUCTS/Composite_QPE/tmp_SRI — 3,306 COMP_*.nc files, the inputs all four
# published families render from, timestamps parsed from filenames rather than
# taken from a directory listing. p50 = p90 = p99 = 2.00 min exactly:
# metronomic when running.
#
# 1500 s (25 min) lands in a real gap. Largest normal interval 16.0 min,
# smallest outage 28.0 min, nothing between — 9 min of margin below, 3 above.
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
COLORBAR_GLOB = "*colorbar*"   # [Q] not observed directly; no check reads it yet

# Raw volumes arrive both compressed and not.
RAW_GLOBS = ("*.netcdf", "*.netcdf.gz")   # [V] both extensions seen in flow/

# Published composites sit directly under PRODUCT_IMAGES/, undated, as a
# rolling window — NOT under K2's realtime/product_images/. Different case,
# different depth, no date partition. See 16-§1.
PRODUCT_IMAGES_PREFIX = ("PRODUCT_IMAGES",)   # [M] observed on the live tree

# FLOW's raw volumes are date-partitioned, the same shape as AQPI's CBAND tree
# but rooted inside the profile's own mount rather than a separate one.
# strftime template, relative to SETTINGS.backend_root, evaluated in UTC.
RADAR_DATED_TREE = {"FLOW": "flow/%Y/%m/%d"}  # [M] observed; UTC-partitioned

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
# LB1 reads the manifest for TWO independent reasons, and the second was found
# after the first:
#
#   1. The images directory keeps orphans the window never reclaims — 26
#      entries against a manifest of 14-15 on qpe_15min.
#   2. mtime masks a stall outright. Measured 2026-10-04 01:56Z on
#      qpe_15min/images/in while the QPE pipeline had been down two hours:
#
#          mtime     1 min ago   declared 23:54      <- re-touched each cycle
#          mtime   119 min ago   declared 23:52
#          mtime   121 min ago   declared 23:24
#          mtime 43331 min ago   declared 09-03      <- the orphans
#
#      Something re-touches only the newest frame, so mtime read 1 minute while
#      the product was 121 minutes stale. newest-by-mtime and newest-by-name
#      agreed throughout, because they are the same file — which is why that
#      comparison is not a test for masking.
#
# So the observation-time rule applies to every tree on this profile, not only
# the raw volumes the gzip sweep rewrites.
LB1_FRESHNESS = "manifest"      # [M] the per-step timestamp the manifest declares
LB2_FRESHNESS = "filename"      # [M] flow-<YYYYMMDD>-<HHMMSS>_...

# Anchored at the start and excluding dotfiles upstream, so a gzip temp file
# cannot match even though its name embeds the same timestamp.
RAW_VOLUME_TS_RE = {"FLOW": r"^flow-(\d{8})-(\d{6})_"}   # per radar

# The dated tree is partitioned by UTC date, not local: the first file of each
# day directory lands at 00:00:1x UTC (verified across 09/26, 09/30, 10/02).
# _radar_path formats in UTC, which is therefore correct — getting this wrong
# would look in the wrong directory for the six hours of MDT offset.

PRODUCTS = {
    # One manifest, no unit subdirectory.
    "composite_ref":     {"details": "composite_ref/details.json",
                          "image_dir": "composite_ref/images/",
                          "cadence_s": _CYCLE_S, "expected_steps": _WINDOW_FRAMES,
                          "max_freshness_s": _FAIL_AGE_S,
                          "min_png_bytes": _MIN_PNG,
                          "backend_max_age_s": _FAIL_AGE_S,
                          "unit": "dBZ"},
    # details_in.json + details_mm.json over images/in/ and images/mm/.
    "qpe_15min":         {"details": "qpe_15min/details_in.json",
                          "image_dir": "qpe_15min/images/",
                          "cadence_s": _CYCLE_S, "expected_steps": _WINDOW_FRAMES,
                          "max_freshness_s": _FAIL_AGE_S, "min_png_bytes": _MIN_PNG,
                          "backend_max_age_s": _FAIL_AGE_S,
                          "unit": "in", "unit_subdir": True},
    "qpe_1hr":           {"details": "qpe_1hr/details_in.json",
                          "image_dir": "qpe_1hr/images/",
                          "cadence_s": _CYCLE_S, "expected_steps": _WINDOW_FRAMES,
                          "max_freshness_s": _FAIL_AGE_S, "min_png_bytes": _MIN_PNG,
                          "backend_max_age_s": _FAIL_AGE_S,
                          "unit": "in", "unit_subdir": True},
    "radar_precip_rate": {"details": "radar_precip_rate/details_in.json",
                          "image_dir": "radar_precip_rate/images/",
                          "cadence_s": _CYCLE_S, "expected_steps": _WINDOW_FRAMES,
                          "max_freshness_s": _FAIL_AGE_S, "min_png_bytes": _MIN_PNG,
                          "backend_max_age_s": _FAIL_AGE_S,
                          "unit": "in/h", "unit_subdir": True},
}

# Both live — do NOT carry over AQPI's discontinued-mm handling (16-§5.3).
UNITS = ("in", "mm")

# Everything XQPI reads is on trinity, so every backend check reports the same
# source. Two characters, by the constraint in Check.source_tag.
# What this deployment calls itself, everywhere a human reads it.
SITE_NAME = "XQPI Sentinel"

# Shown in the header where AQPI names radarca. There is no HTTP origin here;
# everything is read off the trinity mount.
DATA_SOURCE = "trinity · /projects/xqpi"

SOURCE_TAG = "TR"
SOURCE_LABEL = "Trinity"

# NOTE: this profile does NOT list the radarca-only check modules. That list
# is config.RADARCA_ONLY_MODULES, canonical in one place because it describes
# which modules need an HTTP origin rather than which network is watched — the
# same list on every profile. It used to be restated here with an assert
# keeping the two in step, which was two places to get right for no gain.
