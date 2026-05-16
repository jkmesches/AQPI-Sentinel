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


def _load() -> Settings:
    db = os.environ.get("SENTINEL_DB_URL")
    if not db:
        raise RuntimeError(
            "SENTINEL_DB_URL is not set. Copy .env.example to .env or export "
            "it before launching."
        )
    return Settings(
        db_url=db,
        base=os.environ.get("SENTINEL_BASE", "https://radarca.engr.colostate.edu"),
        data_dir=Path(os.environ.get("SENTINEL_DATA_DIR", "./data")).resolve(),
        api_host=os.environ.get("SENTINEL_API_HOST", "127.0.0.1"),
        api_port=int(os.environ.get("SENTINEL_API_PORT", "8000")),
        log_level=os.environ.get("SENTINEL_LOG_LEVEL", "INFO"),
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
    "comp_now":               {"details": "composite_nowcast/details.json",
                               "image_dir": "composite_nowcast/images/",
                               "cadence_s": 120, "expected_steps": 31,
                               "max_freshness_s": -3000, "min_png_bytes": 5_000,
                               "unit": "dBZ"},
    "fcst_total_precip":      {"details": "total_precip/details_in.json",
                               "image_dir": "total_precip/images/",
                               "cadence_s": 3600, "expected_steps": 74,
                               "max_freshness_s": 7200, "min_png_bytes": 5_000,
                               "unit": "in", "unit_subdir": True},
    "fcst_total_precip_cum":  {"details": "total_precip_cumulative/details_in.json",
                               "image_dir": "total_precip_cumulative/images/",
                               "cadence_s": 3600, "expected_steps": 74,
                               "max_freshness_s": 7200, "min_png_bytes": 5_000,
                               "unit": "in", "unit_subdir": True},
    "fcst_precip_rate":       {"details": "precip_rate/details_in.json",
                               "image_dir": "precip_rate/images/",
                               "cadence_s": 900, "expected_steps": 72,
                               "max_freshness_s": 7200, "min_png_bytes": 5_000,
                               "unit": "in/h", "unit_subdir": True},
    "fcst_temp":              {"details": "temperature/details_F.json",
                               "image_dir": "temperature/images/",
                               "cadence_s": 3600, "expected_steps": 75,
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


def image_path(product_id: str, image_name: str) -> str:
    """Mirror the JS: unit-subdir products interpolate <images_dir>/<unit>/<name>."""
    cfg = PRODUCTS[product_id]
    if not cfg.get("unit_subdir"):
        return cfg["image_dir"] + image_name
    unit = cfg["unit"]
    if product_id == "fcst_temp":
        return cfg["image_dir"] + TEMP_UNIT_SUBDIR.get(unit, "F") + "/" + image_name
    return cfg["image_dir"] + unit.split("/")[0] + "/" + image_name
