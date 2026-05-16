"""Shared config for all monitoring tests. Mirrors the product registry
extracted from the public/page-*.js bundle (see radarca-public-characterization.md §6)."""
from __future__ import annotations

BASE = "https://radarca.engr.colostate.edu"

# Per-product expectations.
#   details      : path passed to /api/productDetail?file=
#   image_dir    : path joined with steps[*].imageName for /api/imageData?file=
#   cadence_s    : expected interval between consecutive steps
#   expected_steps : nominal count returned by productDetail (±2 tolerated)
#   max_freshness_s : alarm threshold on (now - max(steps.timestamp));
#                     negative means newest step is allowed to be in the future
#                     (forecasts, nowcasts)
#   min_png_bytes : alarm floor on PNG size (suspiciously small = blank)
#   unit         : the unit/variant we're probing (some products have in/mm,
#                  F/DEG, etc.; pick one for the smoke test)
PRODUCTS = {
    # ---- Radar data (2-minute cadence) -----------------------------------
    "qpe_15min":          {"details": "rain15min/details_in.json",
                           "image_dir": "rain15min/images/",
                           "cadence_s": 120, "expected_steps": 31,
                           "max_freshness_s": 360, "min_png_bytes": 5_000,
                           "unit": "in"},
    "qpe_1hr":            {"details": "rain60min/details_in.json",
                           "image_dir": "rain60min/images/",
                           "cadence_s": 120, "expected_steps": 31,
                           "max_freshness_s": 360, "min_png_bytes": 5_000,
                           "unit": "in"},
    "precip_rate_radar":  {"details": "rainrate/details_in.json",
                           "image_dir": "rainrate/images/",
                           "cadence_s": 120, "expected_steps": 31,
                           "max_freshness_s": 360, "min_png_bytes": 5_000,
                           "unit": "in/h"},
    "comp_ref":           {"details": "composite_ref_max/details.json",
                           "image_dir": "composite_ref_max/images/",
                           "cadence_s": 120, "expected_steps": 31,
                           "max_freshness_s": 360, "min_png_bytes": 5_000,
                           "unit": "dBZ"},
    "comp_now":           {"details": "composite_nowcast/details.json",
                           "image_dir": "composite_nowcast/images/",
                           "cadence_s": 120, "expected_steps": 31,
                           # nowcast steps reach 60 min into the future
                           "max_freshness_s": -3000, "min_png_bytes": 5_000,
                           "unit": "dBZ"},

    # ---- Atmospheric forecast (1-h cadence except precip_rate=15 min) ----
    "fcst_total_precip":  {"details": "total_precip/details_in.json",
                           "image_dir": "total_precip/images/",
                           "cadence_s": 3600, "expected_steps": 74,
                           "max_freshness_s": 7200, "min_png_bytes": 5_000,
                           "unit": "in"},
    "fcst_total_precip_cum": {"details": "total_precip_cumulative/details_in.json",
                              "image_dir": "total_precip_cumulative/images/",
                              "cadence_s": 3600, "expected_steps": 74,
                              "max_freshness_s": 7200, "min_png_bytes": 5_000,
                              "unit": "in"},
    "fcst_precip_rate":   {"details": "precip_rate/details_in.json",
                           "image_dir": "precip_rate/images/",
                           "cadence_s": 900, "expected_steps": 72,
                           "max_freshness_s": 7200, "min_png_bytes": 5_000,
                           "unit": "in/h"},
    "fcst_temp":          {"details": "temperature/details_F.json",
                           "image_dir": "temperature/images/",
                           "cadence_s": 3600, "expected_steps": 75,
                           "max_freshness_s": 7200, "min_png_bytes": 5_000,
                           "unit": "F"},

    # ---- CoSMoS (1-h cadence; forecasts) ---------------------------------
    "water_level":        {"details": "water_level/details.json",
                           "image_dir": "water_level/images/",
                           "cadence_s": 3600, "expected_steps": 19,
                           # 15h into the future is normal
                           "max_freshness_s": -3600, "min_png_bytes": 5_000,
                           "unit": "ft"},
    "water_depth":        {"details": "water_depth/details.json",
                           "image_dir": "water_depth/images/",
                           "cadence_s": 3600, "expected_steps": 19,
                           "max_freshness_s": -3600, "min_png_bytes": 5_000,
                           "unit": "ft"},
    "max_water_level":    {"details": "max_water_level/details.json",
                           "image_dir": "max_water_level/images/",
                           "cadence_s": None, "expected_steps": 1,
                           "max_freshness_s": 90_000, "min_png_bytes": 5_000,
                           "unit": "ft"},
    "max_water_depth":    {"details": "max_water_depth/details.json",
                           "image_dir": "max_water_depth/images/",
                           "cadence_s": None, "expected_steps": 1,
                           "max_freshness_s": 90_000, "min_png_bytes": 5_000,
                           "unit": "ft"},
}

# Static vector layers — alarm if Last-Modified older than this (seconds).
VECTOR_STATICS = {
    "flowlines":   {"path": "/geojson/flowlines.geojson",
                    "max_age_s": 90 * 86400},
    "watersheds":  {"path": "/geojson/watersheds.geojson",
                    "max_age_s": 90 * 86400},
    "stream_csv":  {"path": "/data/stream_data.csv",
                    "max_age_s": 90 * 86400},
}

# Radar id → on-disk folder slug, taken from the JS bundle's `C` object.
# (This is what /api/xbandRadarImages expects as radarFolder; the radar-status
# API uses the public-facing id.)
RADAR_FOLDER = {
    "XEBY":  "ebay",
    "XSCV":  "scvw",
    "XSCW":  "scwa",
    "XSCR":  "scrz",
    "XSWR":  "swyr",
    "CBAND": "sscb",
}
# Radar-status API uses these aliases for the same radars:
STATUS_TO_RADAR = {"EBAY": "XEBY", "CBand": "CBAND"}

# Moment name → productPrefix mapping (the JS bundle's `j` object + L's CBAND
# exception). The radar-side filename uses these prefixes.
MOMENT_TO_PREFIX_DEFAULT = {
    "Reflectivity":              "CorrReflectivity",
    "Differential Reflectivity": "CorrDifferentialReflectivity",
    "Velocity":                  "Velocity",
    "PhiDP":                     "FilteredPhiDP",
    "RhoHV":                     "RhoHV",
}
def moment_to_prefix(radar_id: str, moment: str) -> str:
    if radar_id == "CBAND" and moment == "PhiDP":
        return "PhiDP"   # CBAND uses unfiltered PhiDP
    return MOMENT_TO_PREFIX_DEFAULT[moment]

X_MOMENTS = list(MOMENT_TO_PREFIX_DEFAULT.keys())

# Products whose image path interpolates a unit subdir between images_dir and
# imageName, i.e. URL = images_dir + unit + "/" + imageName. Derived from the
# `eb` array in the JS bundle plus the temperature special-case.
UNIT_SUBDIR_PRODUCTS = {
    "qpe_15min", "qpe_1hr", "precip_rate_radar",
    "fcst_total_precip", "fcst_total_precip_cum",
    "fcst_precip_rate", "fcst_temp",
}
# Temperature uses "F" or "Deg" (note: NOT "DEG") for its subdir.
TEMP_UNIT_SUBDIR = {"F": "F", "DEG": "Deg"}

# Radar geographies (from radarCoordinates in the JS bundle).
RADAR_COORDS = {
    "XSCV": {"lat": 37.3989, "lon": -121.8334, "range_m":  40_000},
    "XSCW": {"lat": 38.5216, "lon": -122.8022, "range_m":  40_000},
    "XSCR": {"lat": 36.9847, "lon": -121.9786, "range_m":  40_000},
    "XEBY": {"lat": 37.8156, "lon": -122.0620, "range_m":  40_000},
    "KBBX": {"lat": 39.4956, "lon": -121.6316, "range_m": 100_000},
    "KDAX": {"lat": 38.5010, "lon": -121.6770, "range_m": 100_000},
    "KMUX": {"lat": 37.1553, "lon": -121.8980, "range_m": 100_000},
}
