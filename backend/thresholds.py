"""Threshold registry — admin-managed knobs, read by every check at evaluation time.

Why this exists
---------------
Every detection threshold in Sentinel (per-product freshness limits, per-radar
ghost-up timeouts, L4 image-QC parameters, global hysteresis/tolerance) was
originally a constant in config.py. That made tuning a code-change-and-deploy
cycle, and made retroactive reclassification of historical rows fragile because
the reprocess scripts had to import the same constants the live evaluator used.

This module routes every threshold read through a single in-process cache that
backs onto a `settings.thresholds` jsonb blob. The admin UI writes the blob;
the cache reloads on demand; every check picks up the new value on its next
run. config.py constants remain as fallback defaults so a fresh database boots
with current behavior unchanged.

Storage shape
-------------
A single row at `settings.thresholds` (jsonb) with this structure::

    {
      "products": {
        "<product_id>": {"max_freshness_s": int, "min_png_bytes": int,
                          "expected_steps": int|null, "cadence_s": int|null}
      },
      "radars":   {"<radar_id>": {"silent_fail_s": int}},
      "l4":       {"<product_id>": {"extreme_threshold": float,
                                     "skip_frozen": bool,
                                     "frozen_min_cov_pct": float,
                                     "skip_range_ring": bool}},
      "globals":  {"hysteresis": float, "cadence_tol": float,
                    "step_count_tol": int, "silent_fail_default": int}
    }

Single-blob (vs per-key rows): atomic PATCH semantics, simpler caching, and
last-write-wins is fine for a 1-3 admin team. Per-key would only matter at
higher concurrency.

Bootstrap
---------
First call to init() that finds no `thresholds` row writes the current
config.py values into the blob so the row matches live behavior from then on.
Idempotent — re-running init() is a no-op once seeded.

Cache invalidation
------------------
`bump_version()` invalidates the cache; the next getter call reloads. The
admin PATCH endpoint calls it after committing. Checks read the latest
snapshot synchronously; worst case a check sees stale values for one
cadence tick after a change, which is fine.
"""
from __future__ import annotations
import json
import logging
from typing import Any

from . import config as _config

log = logging.getLogger(__name__)

THRESHOLDS_KEY = "thresholds"

# Hard-coded global fallbacks. Mirror the values currently in
# layer1_product.py / layer2_radar.py — moving them here so a fresh DB
# bootstraps with identical behavior to the current code.
_GLOBAL_DEFAULTS: dict[str, float | int] = {
    "hysteresis":          0.10,
    "cadence_tol":         0.10,
    "step_count_tol":      4,
    "silent_fail_default": 600,
}

# In-process snapshot. None until init() runs; after that, a dict mirroring
# the storage shape above. Reads fall through this dict, then config.py
# defaults, then _GLOBAL_DEFAULTS / None.
_cache: dict[str, Any] | None = None
_version: int = 0


def current_version() -> int:
    return _version


def _seed_from_config() -> dict[str, Any]:
    """Build the canonical default blob from config.py constants."""
    products: dict[str, dict[str, Any]] = {}
    for pid, cfg in _config.PRODUCTS.items():
        products[pid] = {
            "max_freshness_s": cfg.get("max_freshness_s"),
            "min_png_bytes":   cfg.get("min_png_bytes"),
            "expected_steps":  cfg.get("expected_steps"),
            "cadence_s":       cfg.get("cadence_s"),
        }

    radars: dict[str, dict[str, Any]] = {}
    for rid, sec in _config.RADAR_SILENT_FAIL_S.items():
        radars[rid] = {"silent_fail_s": sec}

    l4: dict[str, dict[str, Any]] = {}
    default_l4 = _config.DEFAULT_L4_PROFILE
    for pid, overrides in _config.L4_PROFILES.items():
        # Persist the resolved profile (defaults merged with overrides) so
        # the admin UI can show every cell without re-fetching defaults.
        profile = {**default_l4, **overrides}
        l4[pid] = {
            "extreme_threshold":  float(profile["extreme_threshold"]),
            "skip_frozen":        bool(profile["skip_frozen"]),
            "frozen_min_cov_pct": float(profile["frozen_min_cov_pct"]),
            "skip_range_ring":    bool(profile.get("skip_range_ring", False)),
        }

    globals_: dict[str, Any] = dict(_GLOBAL_DEFAULTS)

    return {
        "products": products,
        "radars":   radars,
        "l4":       l4,
        "globals":  globals_,
    }


async def init(pool) -> None:
    """Load the blob from settings.thresholds, bootstrapping if missing.

    Idempotent — safe to call once at startup. Subsequent edits go through
    save_blob() which calls bump_version() on success.
    """
    global _cache, _version
    row = await pool.fetchrow(
        "SELECT value FROM settings WHERE key = $1", THRESHOLDS_KEY,
    )
    if row is None:
        seed = _seed_from_config()
        await pool.execute(
            "INSERT INTO settings (key, value, updated_at, updated_by) "
            "VALUES ($1, $2, now(), $3)",
            THRESHOLDS_KEY, seed, "system:bootstrap",
        )
        _cache = seed
        log.info("thresholds: bootstrapped from config.py defaults")
    else:
        val = row["value"]
        if isinstance(val, str):
            val = json.loads(val)
        _cache = val if isinstance(val, dict) else _seed_from_config()
    _version += 1


async def refresh(pool) -> None:
    """Reload the blob from the DB. Called after a successful PATCH."""
    global _cache, _version
    row = await pool.fetchrow(
        "SELECT value FROM settings WHERE key = $1", THRESHOLDS_KEY,
    )
    if row is None:
        await init(pool)
        return
    val = row["value"]
    if isinstance(val, str):
        val = json.loads(val)
    _cache = val if isinstance(val, dict) else _seed_from_config()
    _version += 1


async def save_blob(pool, blob: dict[str, Any], *, updated_by: str) -> None:
    """Persist a new threshold blob and refresh the in-process cache.

    Caller is responsible for validation. asyncpg's jsonb codec handles
    encoding — DO NOT json.dumps() the blob first or it stores as a jsonb
    string (see [[reference-sentinel-prod-gotchas]] for the original bug).
    """
    await pool.execute(
        """
        INSERT INTO settings (key, value, updated_at, updated_by)
        VALUES ($1, $2, now(), $3)
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value,
              updated_at = EXCLUDED.updated_at,
              updated_by = EXCLUDED.updated_by
        """,
        THRESHOLDS_KEY, blob, updated_by,
    )
    await refresh(pool)


async def fetch_blob(pool) -> dict[str, Any]:
    """Read the persisted blob (with updated_at/by) for the admin UI."""
    row = await pool.fetchrow(
        "SELECT value, updated_at, updated_by FROM settings WHERE key = $1",
        THRESHOLDS_KEY,
    )
    if row is None:
        return {"value": _seed_from_config(), "updated_at": None, "updated_by": None}
    val = row["value"]
    if isinstance(val, str):
        val = json.loads(val)
    return {
        "value":      val if isinstance(val, dict) else _seed_from_config(),
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "updated_by": row["updated_by"],
    }


def seed_defaults() -> dict[str, Any]:
    """Expose the seed blob (used by the admin UI to show defaults inline)."""
    return _seed_from_config()


def bump_version() -> None:
    global _version
    _version += 1


# ---------------------------------------------------------------------------
# Synchronous getters used by check evaluators. Each falls through cache →
# config.py default → hard-coded fallback. Returning None means "no value
# configured" (some checks treat that as skip; the caller decides).
# ---------------------------------------------------------------------------

def get_product(product_id: str, key: str, default: Any = None) -> Any:
    if _cache is None:
        cfg = _config.PRODUCTS.get(product_id, {})
        return cfg.get(key, default)
    prods = _cache.get("products") or {}
    overrides = prods.get(product_id) or {}
    if key in overrides and overrides[key] is not None:
        return overrides[key]
    # Fall back to the import-time config so missing keys still resolve.
    cfg = _config.PRODUCTS.get(product_id, {})
    return cfg.get(key, default)


def get_radar(radar_id: str, key: str, default: Any = None) -> Any:
    if _cache is None:
        if key == "silent_fail_s":
            return _config.RADAR_SILENT_FAIL_S.get(radar_id, default)
        return default
    radars = _cache.get("radars") or {}
    overrides = radars.get(radar_id) or {}
    if key in overrides and overrides[key] is not None:
        return overrides[key]
    if key == "silent_fail_s":
        return _config.RADAR_SILENT_FAIL_S.get(radar_id, default)
    return default


def get_l4(product_id: str, key: str, default: Any = None) -> Any:
    """L4 image-QC knobs. Falls through to L4_PROFILES then DEFAULT_L4_PROFILE."""
    if _cache is None:
        prof = _config.l4_profile(product_id)
        return prof.get(key, default)
    l4 = _cache.get("l4") or {}
    overrides = l4.get(product_id) or {}
    if key in overrides and overrides[key] is not None:
        return overrides[key]
    prof = _config.l4_profile(product_id)
    return prof.get(key, default)


def get_global(key: str, default: Any = None) -> Any:
    if _cache is None:
        return _GLOBAL_DEFAULTS.get(key, default)
    globals_ = _cache.get("globals") or {}
    if key in globals_ and globals_[key] is not None:
        return globals_[key]
    return _GLOBAL_DEFAULTS.get(key, default)
