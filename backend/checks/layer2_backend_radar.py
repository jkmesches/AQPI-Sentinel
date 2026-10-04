"""Layer-2 BACKEND radar arrival — read from the filesystem, not from radarca.

Companion to ``layer1_backend_product``. Where ``layer2_radar`` asks radarca
"does /api/radar-status/ say this radar is up, and are there images for it", this asks
the only question that is unambiguous: **is this radar's data landing on disk right now**.

Two trees, because the radars do not all arrive the same way:

* Five X-bands  — DROPS2 writes a dated tree per radar under
  ``{backend_root}/PRODUCTS/DROPS/<folder>``. Entries are *created*, so the directory
  mtime is a true arrival time and one stat suffices.
* CBAND (SSCB)  — lands on trinity at ``/trinity/projects/aqpi/sscb/YYYY/MM/DD``, not in
  the DROPS tree. It has no ACCEPT entry in iris's ldmd.conf and no file_process_sscb.sh
  on granite, so it neither arrives by the X-band push path nor joins their composite.
  Monitored here because a C-band outage was previously undetectable.

FLOW (XQPI/JPL) is deliberately NOT included in this revision.

Thresholds reuse ``config.RADAR_SILENT_FAIL_S``, which was characterised against real
per-radar cadence (XEBY 300 s ... CBAND 1080 s) and is already the number operators
reason about. Overridable per radar under ``backend_silent_s``.
"""
from __future__ import annotations
import asyncio
import os
import re
from datetime import datetime, timezone
from typing import Any

from ..config import (BACKEND_SOURCE, LB2_FRESHNESS,  # noqa: F401
                      RADAR_DATED_TREE, RADAR_FOLDER, RAW_VOLUME_TS_RE,
                      RADAR_SILENT_FAIL_S, SETTINGS)
from ..errors import humanize_error
from .. import thresholds as _thresholds
from ..registry import register
from .base import Check, CheckResult, utcnow
from .helpers import headroom, worst_of

FS_TIMEOUT_S = 5.0

# Radars whose data does not land in the DROPS tree. Value is a strftime template
# evaluated in UTC, matching how the writer dates its directories.
# NOTE: these are paths INSIDE the container, which differ from the host. cira-aqpi
# mounts /trinity on the host, but sentinel-backend sees only what the compose file
# bind-mounts. Hence a setting, not a literal.
def _special_trees() -> dict[str, str]:
    # str() first: SETTINGS.sscb_root is a Path (config._opt_path), and Path has no
    # .rstrip. Calling it raised AttributeError on every LB2 run in v0.5.0.
    if not SETTINGS.sscb_root:
        return {}
    return {"CBAND": str(SETTINGS.sscb_root).rstrip("/") + "/%Y/%m/%d"}


def _radar_path(radar_id: str, now) -> str:
    # Three layouts, most specific first: a radar on its own mount (CBAND via
    # SENTINEL_SSCB_ROOT), a radar dated inside this profile's own mount (XQPI's
    # FLOW), and AQPI's flat DROPS tree.
    tmpl = _special_trees().get(radar_id)
    if tmpl:
        return now.astimezone(timezone.utc).strftime(tmpl)
    rel = RADAR_DATED_TREE.get(radar_id)
    if rel:
        return now.astimezone(timezone.utc).strftime(
            os.path.join(SETTINGS.backend_root, rel))
    return os.path.join(SETTINGS.backend_root, "PRODUCTS", "DROPS",
                        RADAR_FOLDER[radar_id])


# Generous: one FLOW day holds ~2,900 volumes. If a directory ever exceeds
# this the walk stops early, which can only make the newest timestamp look
# OLDER than it is -- a false alarm rather than a masked outage, which is the
# correct direction to fail in.
MAX_SCAN_ENTRIES = 20_000


def _newest_declared(path: str) -> float:
    """Blocking. Newest observation time the FILENAMES declare, as an epoch.

    For trees where mtime is not a sound freshness basis -- see
    config.LB2_FRESHNESS and backend/profiles/xqpi.py for the gzip sweep that
    makes it unsound on trinity.

    Dot-prefixed entries are skipped before matching, and deliberately so
    rather than relying on the pattern to reject them: gzip leaves temporary
    files like `.flow-20261003-194148_..._PPI.netcdf.gz.4u4pxw` in the live
    directory (14 of them on inspection, three suffixes for one source
    volume). They are the newest entries by mtime and their names embed a
    real, parseable timestamp, so only the leading dot distinguishes them.

    No stat(2) per entry, unlike the mtime path -- the name carries everything
    needed, so this is cheaper than what it replaces despite the larger walk.
    """
    pat = re.compile(RAW_VOLUME_TS_RE)
    newest = 0.0
    n = 0
    with os.scandir(path) as it:
        for e in it:
            n += 1
            if n > MAX_SCAN_ENTRIES:
                break
            if e.name.startswith("."):
                continue
            m = pat.match(e.name)
            if not m:
                continue
            try:
                dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            except ValueError:
                continue
            ts = dt.replace(tzinfo=timezone.utc).timestamp()
            if ts > newest:
                newest = ts
    if not newest:
        # The directory exists but declares nothing readable. Same meaning as
        # an absent directory: let the silence threshold judge it.
        raise FileNotFoundError(f"no parseable volume filenames in {path}")
    return newest


def _dir_mtime(path: str) -> float:
    """Blocking. Directory mtime only — one stat, no listing.

    Valid here *because DROPS2 creates entries*: a directory's mtime moves when a file is
    added or removed in it. It would NOT be valid for a tree whose files are overwritten
    in place, which is why the product check scans for the newest file instead.
    """
    return os.stat(path).st_mtime


class Layer2BackendRadarCheck(Check):
    """One per radar in config.RADAR_FOLDER. Reads the backend tree directly."""

    # Own stage — see layer1_backend_product for why.
    stage = "LB2"
    # Independent of radarca by design.
    depends_on: list[str] = []

    def __init__(self, radar_id: str):
        self.radar_id = radar_id
        # A radar in _special_trees() lives somewhere other than the profile's
        # default tree — on AQPI that is CBAND on trinity while the rest are on
        # K2. Otherwise it is wherever this profile publishes, which is K2 for
        # AQPI and trinity for XQPI. See config.BACKEND_SOURCE.
        if radar_id in _special_trees():
            self.source_tag, self.source_label = "TR", "Trinity"
        else:
            self.source_tag, self.source_label = BACKEND_SOURCE

        self.id = f"layer2.backend.{radar_id}"
        self.target = radar_id
        self.cadence_s = 60

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        sub: dict[str, str] = {}
        metrics: dict[str, float] = {}
        path = _radar_path(self.radar_id, t0)
        payload: dict[str, Any] = {"path": path, "source": "backend-filesystem"}

        reader = _newest_declared if LB2_FRESHNESS == "filename" else _dir_mtime
        payload["freshness_basis"] = LB2_FRESHNESS

        try:
            mtime = await asyncio.wait_for(
                asyncio.to_thread(reader, path), FS_TIMEOUT_S)
        except asyncio.TimeoutError:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage, status="error",
                started_at=t0, finished_at=utcnow(),
                summary=f"NFS read timed out after {FS_TIMEOUT_S:g}s — mount may be hung",
                payload=payload, metrics={"fs_timeout": 1.0},
            )
        except FileNotFoundError:
            # For a dated tree this is the normal shape of "nothing has arrived today
            # yet", which just after 00Z is not yet a fault. The silence threshold is
            # what decides; treat absence as maximally stale and let it be judged.
            metrics["fs_timeout"] = 0.0
            sub["A_arriving"] = "fail"
            payload["absent"] = True
            return _final(self, t0, sub, payload, metrics,
                          "no data directory for the current UTC day")
        except re.error as e:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage, status="error",
                started_at=t0, finished_at=utcnow(),
                summary=f"profile RAW_VOLUME_TS_RE is not a valid pattern: {e}",
                payload=payload, metrics={"fs_timeout": 0.0},
            )
        except OSError as e:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage, status="error",
                started_at=t0, finished_at=utcnow(),
                summary=f"backend read failed: {humanize_error(e)}",
                payload=payload, metrics={"fs_timeout": 0.0},
            )

        metrics["fs_timeout"] = 0.0
        age_s = t0.timestamp() - mtime
        metrics["age_s"] = float(age_s)

        silent_s = _thresholds.get_radar(
            self.radar_id, "backend_silent_s",
            RADAR_SILENT_FAIL_S.get(self.radar_id, 900),
        )
        metrics["backend_silent_s"] = float(silent_s)
        # Sparkline series. See helpers.headroom: the dashboard needs one
        # fixed 0..1 axis, and these limits differ per radar (300 s..1080 s).
        metrics["headroom"] = headroom(age_s, silent_s)
        # See layer1_backend_product: payload is what the reprocess engine can read.
        payload["age_s"] = round(age_s, 1)
        payload["silent_s"] = float(silent_s)

        # One threshold, three bands: a radar at 0.8x its characterised silence limit is
        # worth seeing before it crosses.
        if age_s <= silent_s * 0.8:
            sub["A_arriving"] = "pass"
        elif age_s <= silent_s:
            sub["A_arriving"] = "warn"
        else:
            sub["A_arriving"] = "fail"

        what = "newest volume" if LB2_FRESHNESS == "filename" else "last arrival"
        summary = (f"{what} {age_s / 60:.1f} min ago"
                   f"  (silent limit {silent_s / 60:.0f} min)")
        if sub["A_arriving"] == "fail":
            summary = "BACKEND SILENT — " + summary
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
# Register one instance per radar in the profile's table (FLOW on xqpi).
# --------------------------------------------------------------------------

# Gated identically to layer1_backend_product. CBAND additionally requires
# SENTINEL_SSCB_ROOT, since it does not live under the DROPS tree.
if SETTINGS.backend_root:
    for _rid in RADAR_FOLDER:
        if _rid == "CBAND" and not SETTINGS.sscb_root:
            continue
        register(Layer2BackendRadarCheck(radar_id=_rid))
