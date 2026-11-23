"""Layer-1 BACKEND product freshness — read from the filesystem, not from radarca.

Why this exists
---------------
Every other L1 product verdict is derived from ``{SETTINGS.base}/api/productDetail``,
i.e. from what radarca is *serving*. That conflates two different failures: the product
not being produced, and the display tier not showing it. Operators repeatedly reported
"Sentinel says X, but when I checked the products in the archive...", and the
archive is ground truth.

This check reads the published file directly over the AQPI deployment's existing read-only NFS
mount of the main archive. It is the PRIMARY indicator for "is this product being produced".

``layer1_product`` is NOT modified. It keeps measuring radarca exactly as before; what
changes is routing: ``alerts.yaml`` sends stage ``LB1`` to a paging policy and stage
``L1`` to a quiet one. The inversion is therefore config, reversible without a deploy,
and no existing check code is touched.

No new access is required: the AQPI deployment already mounts the tree (verified 2026-10-02).

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

# Relative, as the whole package imports.
from ..config import (BACKEND_PRODUCT_CADENCE_S, BACKEND_SOURCE,  # noqa: TID252
                      COSMOS_PRODUCTS, LB1_FRESHNESS, PRODUCT_IMAGES_PREFIX, PRODUCTS,
                      SETTINGS, image_path)
from ..cosmos_manifest import explained_gap, manifest_age_s  # noqa: TID252
from ..errors import humanize_error
from .. import thresholds as _thresholds
from ..registry import register
from .base import Check, CheckResult, utcnow
from .helpers import headroom, worst_of
from .layer1_cosmos_cycle import manifest_path, read_failure, read_manifest

# A hung NFS mount blocks stat(2) indefinitely. Every existing check is HTTP with a
# timeout, so the scheduler has never had to survive a blocking syscall; without this
# one wedged mount would stall the whole cycle.
FS_TIMEOUT_S = 5.0

# Fallback when a product has no explicit backend threshold. Deliberately generous:
# the point of v1 is to stop contradicting the main archive, not to introduce a new noise source.
#
# Per-product values are set from /admin/thresholds under `backend_max_age_s` and
# live in the settings blob, not here, so this constant is NOT where a deployment
# records its choice. The convention a deployment is choosing between, when it
# sets one against an hourly publish, is roughly:
#
#     ~1.5x the publish interval   a single missed publish FAILS
#     ~2.25x                       a single miss passes; two or more fail
#
# Either is defensible and the choice is the deployment's, but it is a choice --
# a value between one and two publish intervals will fire on every ordinary late
# publish, and the next person reads it firing and widens it back.
#
# B_freshness is binary (pass/fail) and a warn arm was CONSIDERED AND DECLINED
# on 2026-10-12. Do not add one without re-opening the two reasons:
#
#   1. The operator intent it would serve is already served. "A missed publish
#      on a flood product should be visible" is satisfied by a fail at ~1.5x
#      the publish interval. A "late" state between late and missed adds a
#      verdict nobody has said they would act on differently.
#   2. Nothing can make it quiet. A `warn` opens an alarm (engine.evaluate
#      returns early only for inconclusive and pass/skip), and NOTHING on the
#      notification path filters on severity -- the step loop fires every due
#      step and _dispatch has no severity test. So a warn arm here would open
#      an alarm and re-notify on the route's repeat_interval (LB1: hourly)
#      for every ordinary late publish. That is the v0.8.1 band's failure
#      exactly, and renaming severity_floor to severity_at_least did not
#      change it -- see alarms/router.py.
#
# If a "late" state is wanted later, the prerequisite is a way to record a
# verdict without notifying, not a threshold.
#
# Reopened 2026-11-23 for the four CoSMoS products alone, by Joseph. A
# product past its limit warns when the cycle manifest is under 150 min old,
# names a skip (HRRR short or the warmstart stand-down) and the product is
# under 240 min old. Each part of reason 2 is met. AQPI's routing sends an
# alarm that opens on a warn to a silent policy, so it is recorded and
# notifies nobody. The alarm engine now closes such an alarm and opens a new
# one when the check worsens to fail, so a failed publish after a skip still
# pages. Reason 1 is answered by the reason itself, which the warn carries:
# the schedule declining to run reads differently from a product that broke.
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


# What counts as a published frame. A positive filter rather than a list of
# junk suffixes, because this check's failure direction is a false PASS: any
# entry carrying a fresh mtime sets the product's age, so a lock file, an
# editor backup or a half-written temp file reports a stale product as
# current. A denylist only covers the junk someone thought of.
#
# Measured on the main archive on 2026-11-21, between publishes: 2138 frames under
# product_images, and no non-PNG entry in any images/ directory. The .lock
# and .tmp leftovers that do exist sit beside the manifests one level up,
# which _scan never reads, and some have sat there for months
# (rain60min/details_in.json.868453.tmp since 2026-09-17). All 13 products on
# this basis published a .png as their newest entry, names like
# qpe_15m_step_COMP_20261121_033600_rainfall.nc.png included.
#
# During a publish it is not inert, so do not read the measurement above as
# dead code. The XQPI session's probe stat()ed
# images/20261121_1600.png.tmp at 03:20:02Z, 1.4 s before that frame's
# rename, so a temp file is present in images/ for much of the 76 s a burst
# takes. It changes no verdict, because a temp file written mid-burst carries
# nearly the same mtime as the frames landing beside it. It would change one
# if a burst ever abandoned, which is the case this guards.
FRAME_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _is_frame(name: str) -> bool:
    """Whether a directory entry is a published image frame.

    Args:
        name: The entry's name.

    Returns:
        True when it may set the product's freshness.
    """
    if name.startswith("."):
        return False
    return name.lower().endswith(FRAME_SUFFIXES)


def _scan(path: str) -> dict[str, Any]:
    """Blocking. Newest mtime + entry count, bounded.

    ``scandir`` rather than ``ls -t``: one syscall per entry, no sort of the whole
    directory, and it never materialises a list of 100k names. ``MAX_ENTRIES`` caps the
    walk so a directory that has grown unexpectedly cannot turn a check into a scan.

    Only published frames count toward the mtime and the count. See
    ``FRAME_SUFFIXES``; anything else reports a stale product as fresh.
    """
    MAX_ENTRIES = 5_000
    newest = 0.0
    newest_name = ""
    n = 0
    skipped = 0
    truncated = False
    with os.scandir(path) as it:
        for e in it:
            n += 1
            if n > MAX_ENTRIES:
                truncated = True
                break
            if not _is_frame(e.name):
                skipped += 1
                continue
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
                m = e.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue                      # dangling symlink, or raced deletion
            if m > newest:
                newest, newest_name = m, e.name
    return {"newest_mtime": newest, "newest_name": newest_name,
            "count": n - skipped, "non_frames": skipped,
            "truncated": truncated, "basis": "newest_mtime"}


class Layer1BackendProductCheck(Check):
    """One per product in config.PRODUCTS. Reads the main archive directly."""

    # Its own stage, not L1. These stand alongside the radarca product checks as a
    # separate family -- the same shape as layer4_image, which sits beside the radar
    # checks rather than modifying them. A distinct stage is what lets alerts.yaml route
    # the two differently (backend pages, radarca informational) with no code change to
    # the existing checks at all.
    stage = "LB1"
    # Intentionally NOT depends_on layer0.origin.alive: radarca being down has no
    # bearing on whether the product exists on the main archive. That independence is the entire
    # point of this check.
    depends_on: list[str] = []

    def __init__(self, product_id: str):
        cfg = PRODUCTS[product_id]
        self.product_id = product_id
        # Whichever tree THIS profile publishes to. A tag baked in here was
        # right for one deployment and silently wrong for the other. See
        # config.BACKEND_SOURCE and Check.source_*.
        self.source_tag, self.source_label = BACKEND_SOURCE

        self.cfg = cfg
        self.id = f"layer1.backend.{product_id}"
        self.target = product_id
        # NOT cfg["cadence_s"]. That is the upstream PUBLISH interval, and using it
        # here sampled every product at its own period -- aliased by construction,
        # so a missed publish went unnoticed for up to a full interval and a panel
        # edit took as long to apply. Filesystem reads are cheap; the publish rate
        # is not a reason to sample slowly. See config.BACKEND_PRODUCT_CADENCE_S.
        self.cadence_s = max(60, int(cfg.get("backend_cadence_s")
                                     or BACKEND_PRODUCT_CADENCE_S))

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
        # Recorded so the frame filter is not silent. If this ever rises, the
        # directory has grown something the check is deliberately ignoring,
        # and "no files published" would otherwise be the only symptom.
        metrics["non_frames"] = float(info.get("non_frames", 0))
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

        excuse = None
        if sub["B_freshness"] == "fail" and self.product_id in COSMOS_PRODUCTS:
            excuse = await _cycle_excuse(t0, age_s, payload)
            if excuse:
                sub["B_freshness"] = "warn"

        mins = age_s / 60.0
        unit = "steps" if info["basis"] == "manifest" else "files"
        summary = (f"newest {info['newest_name']} {mins:.0f} min old"
                   f"  (limit {max_age / 60:.0f} min, {info['count']} {unit})")
        if excuse:
            summary = f"BACKEND LATE, cycle skipped ({excuse}): " + summary
        elif sub["B_freshness"] != "pass":
            summary = "BACKEND STALE — " + summary
        return _final(self, t0, sub, payload, metrics, summary)


async def _cycle_excuse(now: datetime, age_s: float, payload: dict[str, Any]) -> str | None:
    """Why a late CoSMoS product is late, when the cycle manifest vouches for it.

    Records what it read under payload["cycle_manifest"], with whether it
    vouched, for the reprocess engine to re-apply. A manifest that cannot be
    read excuses nothing, so the product stays failed.

    Args:
        now: The check's start time.
        age_s: The product's age.
        payload: The result payload, written to.

    Returns:
        The manifest's reason, or None when the gap stays unexplained.
    """
    try:
        m = await read_manifest(manifest_path())
    except (OSError, ValueError, TimeoutError) as e:
        payload["cycle_manifest"] = {"error": read_failure(e)[1], "explains_gap": False}
        return None
    excuse = explained_gap(m, now, age_s)
    payload["cycle_manifest"] = {
        "metfile": m.metfile, "outcome": m.outcome, "manifest_reason": m.reason,
        "wrote_at": m.wrote_at.isoformat(),
        "manifest_age_s": round(manifest_age_s(m, now), 1),
        "explains_gap": excuse is not None,
    }
    return excuse


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

# Gated on SENTINEL_BACKEND_ROOT. Unset (the test deployment, which has no VPN
# and mounts neither tree) means these checks are never registered at all,
# not skipped, not silent, absent. Same image, different .env; the established pattern
# here, as with SENTINEL_ARCHIVE_ENABLED.
if SETTINGS.backend_root:
    for _pid in PRODUCTS:
        register(Layer1BackendProductCheck(product_id=_pid))
