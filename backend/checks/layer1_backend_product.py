"""Layer-1 BACKEND product freshness — read from the filesystem, not from radarca.

Why this exists
---------------
Every other L1 product verdict is derived from ``{SETTINGS.base}/api/productDetail``,
i.e. from what radarca is *serving*. That conflates two different failures: the product
not being produced, and the display tier not showing it. Operators repeatedly reported
"Sentinel says X, but when I checked the products in K2...", and K2 is ground truth.

This check reads the published file directly over cira-aqpi's existing read-only NFS
mount of K2. It is the PRIMARY indicator for "is this product being produced".

``layer1_product`` is NOT modified. It keeps measuring radarca exactly as before; what
changes is routing: ``alerts.yaml`` sends stage ``LB1`` to a paging policy and stage
``L1`` to a quiet one. The inversion is therefore config, reversible without a deploy,
and no existing check code is touched.

No new access is required: cira-aqpi already mounts the tree (verified 2026-10-02).

Path construction reuses ``config.image_path``, so the unit-subdir rules
(``rain15min/images/in/``, ``temperature/images/Deg/``) stay in one place.

Thresholds are deliberately NOT shared with the radarca check. ``/api/productDetail``
returns forecast *step* timestamps, which is why validated values like
``water_level: -41_400`` are negative. A file mtime is always in the past, so these need
their own positive thresholds, resolved under the ``backend_max_age_s`` key.
"""
from __future__ import annotations
import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any

from ..config import (BACKEND_SOURCE, LB1_FRESHNESS, PRODUCT_IMAGES_PREFIX, PRODUCTS,
                      SETTINGS, image_path)
from ..errors import humanize_error
from .. import thresholds as _thresholds
from ..registry import register
from .base import Check, CheckResult, utcnow
from .helpers import headroom, worst_of

# A hung NFS mount blocks stat(2) indefinitely. Every existing check is HTTP with a
# timeout, so the scheduler has never had to survive a blocking syscall; without this
# one wedged mount would stall the whole cycle.
FS_TIMEOUT_S = 5.0

# Fallback when a product has no explicit backend threshold. Deliberately generous:
# the point of v1 is to stop contradicting K2, not to introduce a new noise source.
DEFAULT_BACKEND_MAX_AGE_S = 1_800


def _product_dir(product_id: str) -> str:
    """Absolute directory holding this product's published images."""
    rel = image_path(product_id, "")          # trailing-slash dir, unit subdir applied
    return os.path.join(SETTINGS.backend_root, *PRODUCT_IMAGES_PREFIX, rel)


def _manifest_path(product_id: str) -> str:
    """Absolute path to the product's published manifest."""
    return os.path.join(SETTINGS.backend_root, *PRODUCT_IMAGES_PREFIX,
                        PRODUCTS[product_id]["details"])


def _newest_declared(path: str) -> dict[str, Any]:
    """Blocking. Newest observation time the manifest DECLARES, as an epoch.

    Used where the filesystem's own timestamps are not a sound freshness basis
    -- see config.LB1_FRESHNESS. Two reasons to prefer the manifest over
    parsing the image filenames on this tree:

    * A gzip sweep rewrites files, so mtime can say "fresh" when nothing new
      arrived. The manifest only changes when the publisher rewrites it.
    * The directory outlives its own window. qpe_15min/images/in/ held 26
      entries against a manifest of 14-15 -- eleven orphans from 2026-09-03
      that the rolling window never reclaimed. The manifest is the product;
      the directory is a cache with litter in it.

    Timestamps are naive ISO ("2026-10-03T22:26:00") and are UTC: the newest
    step read 23:24 while the wall clock was 23:50 UTC, and reading them as
    local time would place them an hour into the future.
    """
    with open(path, "rb") as fh:
        doc = json.load(fh)
    steps = doc.get("steps") or []
    newest = 0.0
    newest_name = ""
    for st in steps:
        raw = (st or {}).get("timestamp")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw))
        except ValueError:
            continue                      # one malformed step is not an outage
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = dt.timestamp()
        if ts > newest:
            newest, newest_name = ts, str((st or {}).get("imageName") or raw)
    return {"newest_mtime": newest, "newest_name": newest_name,
            "count": len(steps), "truncated": False, "basis": "manifest"}


def _scan(path: str) -> dict[str, Any]:
    """Blocking. Newest mtime + entry count, bounded.

    ``scandir`` rather than ``ls -t``: one syscall per entry, no sort of the whole
    directory, and it never materialises a list of 100k names. ``MAX_ENTRIES`` caps the
    walk so a directory that has grown unexpectedly cannot turn a check into a scan.
    """
    MAX_ENTRIES = 5_000
    newest = 0.0
    newest_name = ""
    n = 0
    truncated = False
    with os.scandir(path) as it:
        for e in it:
            n += 1
            if n > MAX_ENTRIES:
                truncated = True
                break
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
                m = e.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue                      # dangling symlink, or raced deletion
            if m > newest:
                newest, newest_name = m, e.name
    return {"newest_mtime": newest, "newest_name": newest_name,
            "count": n, "truncated": truncated, "basis": "newest_mtime"}


class Layer1BackendProductCheck(Check):
    """One per product in config.PRODUCTS. Reads K2 directly."""

    # Its own stage, not L1. These stand alongside the radarca product checks as a
    # separate family -- the same shape as layer4_image, which sits beside the radar
    # checks rather than modifying them. A distinct stage is what lets alerts.yaml route
    # the two differently (backend pages, radarca informational) with no code change to
    # the existing checks at all.
    stage = "LB1"
    # Intentionally NOT depends_on layer0.origin.alive: radarca being down has no
    # bearing on whether the product exists on K2. That independence is the entire
    # point of this check.
    depends_on: list[str] = []

    def __init__(self, product_id: str):
        cfg = PRODUCTS[product_id]
        self.product_id = product_id
        # Whichever tree THIS profile publishes to — K2 for AQPI, trinity for
        # XQPI. Hardcoding "K2" was right for one deployment and silently
        # wrong for the other. See config.BACKEND_SOURCE and Check.source_*.
        self.source_tag, self.source_label = BACKEND_SOURCE

        self.cfg = cfg
        self.id = f"layer1.backend.{product_id}"
        self.target = product_id
        # Filesystem reads are far cheaper than the radarca fetch, but there is no
        # value in running faster than the product is published.
        self.cadence_s = max(60, int(cfg.get("cadence_s") or 120))

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        sub: dict[str, str] = {}
        metrics: dict[str, float] = {}
        path = _product_dir(self.product_id)
        payload: dict[str, Any] = {"path": path, "source": "backend-filesystem"}

        if LB1_FRESHNESS == "manifest":
            read_path = _manifest_path(self.product_id)
            reader, payload["manifest"] = _newest_declared, read_path
        else:
            read_path, reader = path, _scan
        payload["freshness_basis"] = LB1_FRESHNESS

        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(reader, read_path), FS_TIMEOUT_S)
        except asyncio.TimeoutError:
            # Distinct from "stale": the mount did not answer. Reported as error so it
            # routes like a broken check rather than a broken product.
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage, status="error",
                started_at=t0, finished_at=utcnow(),
                summary=f"NFS read timed out after {FS_TIMEOUT_S:g}s — mount may be hung",
                payload=payload, metrics={"fs_timeout": 1.0},
            )
        except FileNotFoundError:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage, status="fail",
                started_at=t0, finished_at=utcnow(),
                summary="product directory does not exist on the backend",
                payload=payload, metrics={"fs_timeout": 0.0},
            )
        except OSError as e:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage, status="error",
                started_at=t0, finished_at=utcnow(),
                summary=f"backend read failed: {humanize_error(e)}",
                payload=payload, metrics={"fs_timeout": 0.0},
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # Reported as error, not fail: a manifest caught mid-rewrite is a
            # broken read, not a product that stopped publishing. Routing it as
            # a product failure would page for a torn file.
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage, status="error",
                started_at=t0, finished_at=utcnow(),
                summary=f"manifest is not valid JSON: {humanize_error(e)}",
                payload=payload, metrics={"fs_timeout": 0.0},
            )

        metrics["fs_timeout"] = 0.0
        metrics["file_count"] = float(info["count"])
        payload["newest_file"] = info["newest_name"]
        payload["truncated"] = info["truncated"]

        # --- A. presence ---
        if not info["newest_mtime"]:
            sub["A_present"] = "fail"
            return _final(self, t0, sub, payload, metrics,
                          "manifest declares no usable steps"
                          if info["basis"] == "manifest"
                          else "no files published on the backend")
        sub["A_present"] = "pass"

        # --- B. freshness (the primary verdict) ---
        age_s = (t0.timestamp() - info["newest_mtime"])
        metrics["age_s"] = float(age_s)
        max_age = _thresholds.get_product(
            self.product_id, "backend_max_age_s",
            self.cfg.get("backend_max_age_s", DEFAULT_BACKEND_MAX_AGE_S),
        )
        metrics["backend_max_age_s"] = float(max_age)
        metrics["headroom"] = headroom(age_s, max_age)
        sub["B_freshness"] = "pass" if age_s <= max_age else "fail"
        # Also in the payload, not just metrics: check_runs stores payload but NOT
        # metrics (those go to metric_samples), and the reprocess engine reads only
        # check_runs. Without this a threshold change cannot be applied retroactively.
        payload["age_s"] = round(age_s, 1)
        payload["max_age_s"] = float(max_age)

        mins = age_s / 60.0
        unit = "steps" if info["basis"] == "manifest" else "files"
        summary = (f"newest {info['newest_name']} {mins:.0f} min old"
                   f"  (limit {max_age / 60:.0f} min, {info['count']} {unit})")
        if sub["B_freshness"] != "pass":
            summary = "BACKEND STALE — " + summary
        return _final(self, t0, sub, payload, metrics, summary)


def _final(check: Check, t0, sub: dict[str, str], payload: dict, metrics: dict,
           summary: str) -> CheckResult:
    payload["sub_status"] = sub
    return CheckResult(
        check_id=check.id, target=check.target, stage=check.stage,
        status=worst_of(*sub.values()) if sub else "pass",
        started_at=t0, finished_at=utcnow(),
        summary=summary, payload=payload, metrics=metrics,
    )


# --------------------------------------------------------------------------
# Register one instance per product
# --------------------------------------------------------------------------

# Gated on SENTINEL_BACKEND_ROOT. Unset (the shirejoe dev deployment, which has no VPN
# and mounts neither K2 nor trinity) means these checks are never registered at all --
# not skipped, not silent, absent. Same image, different .env; the established pattern
# here, as with SENTINEL_ARCHIVE_ENABLED.
if SETTINGS.backend_root:
    for _pid in PRODUCTS:
        register(Layer1BackendProductCheck(product_id=_pid))
