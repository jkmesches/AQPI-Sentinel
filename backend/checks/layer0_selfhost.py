"""Sentinel watching Sentinel — host resources the stack itself depends on.

Motivation: on 2026-08-01 the production LXC filled its disk and Sentinel
stopped collecting for six days. Every radar check kept its own counsel; the
one thing nobody was watching was the box doing the watching. A monitoring
system that can't report its own failure mode is only half a monitoring
system.

Two filesystems matter and they fail differently:

- **local disk** (`/`, the container's overlay → the LXC root) holds pgdata.
  Filling it wedges Postgres and takes the whole stack down. This is the one
  that actually broke.
- **cold/archive storage** (`SENTINEL_ARCHIVE_ROOT`, production: NFS on
  Erebor) holds the image archive and offloaded CSVs. Filling it loses new
  archive writes and blocks retention sweeps, but the stack keeps monitoring.

Hence: warn/fail thresholds on the local disk, and a softer verdict on the
archive mount, which is also reported as `skip` when it isn't a separate
filesystem (the dev default, where archive lives under ./data).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from backend.storage_io import STORAGE_IO, StorageBusyError

from .. import thresholds
from ..config import SETTINGS
from ..registry import register
from .base import Check, CheckResult, utcnow

# Percent-used thresholds. Deliberately well below 100: Postgres needs
# headroom to checkpoint and VACUUM, and the operator needs time to act.
DISK_WARN_PCT = 75.0
DISK_FAIL_PCT = 88.0


def _usage(path: Path) -> dict:
    total, used, free = shutil.disk_usage(path)
    return {
        "path":      str(path),
        "total_gb":  round(total / 1e9, 1),
        "used_gb":   round(used / 1e9, 1),
        "free_gb":   round(free / 1e9, 1),
        "used_pct":  round(used / total * 100, 1) if total else 0.0,
    }


def _usage_if_exists(path: Path) -> dict | None:
    """_usage, or None when the path isn't there. Both calls run in the
    worker thread — `exists()` on a hung NFS mount blocks just as hard as
    statfs does, so it must not be hoisted back onto the event loop."""
    if not path.exists():
        return None
    return _usage(path)


class Layer0DiskCheck(Check):
    """Local disk headroom on the host running the stack."""

    id         = "layer0.self.disk"
    target     = "sentinel-host"
    stage      = "L0"
    cadence_s  = 300
    depends_on: list[str] = []

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        warn_at = float(thresholds.get_global("disk_warn_pct", DISK_WARN_PCT))
        fail_at = float(thresholds.get_global("disk_fail_pct", DISK_FAIL_PCT))

        try:
            local = await asyncio.to_thread(_usage, Path("/"))
        except OSError as e:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error", started_at=t0, finished_at=utcnow(),
                summary=f"disk_usage failed: {e}", payload={"error": str(e)},
            )

        pct = local["used_pct"]
        if pct >= fail_at:
            status = "fail"
        elif pct >= warn_at:
            status = "warn"
        else:
            status = "pass"

        payload = {"local": local, "warn_pct": warn_at, "fail_pct": fail_at}
        parts = [f"local {pct:.0f}% used ({local['free_gb']:.0f} GB free)"]

        # Archive mount, when it's genuinely a different filesystem.
        #
        # On the stores' pool and timed out: in production this path is a
        # `hard` NFS mount, where statfs blocks indefinitely while the NAS is
        # unreachable. This check exists to report trouble, so it must not
        # become the thing that hangs, and a probe left stuck on the stores'
        # pool cannot hold a thread the local statfs above needs.
        try:
            archive_root = SETTINGS.archive_root
            arch = await STORAGE_IO.run(_usage_if_exists, archive_root, timeout_s=10)
            if arch is not None:
                if arch["total_gb"] != local["total_gb"]:
                    payload["archive"] = arch
                    parts.append(
                        f"archive {arch['used_pct']:.0f}% used "
                        f"({arch['free_gb']:.0f} GB free)"
                    )
                    # The archive filling doesn't stop monitoring, so it never
                    # escalates past warn on its own.
                    if arch["used_pct"] >= fail_at and status == "pass":
                        status = "warn"
        except (TimeoutError, StorageBusyError) as e:
            # Almost always a hung NFS mount. Worth a warn on its own, since an
            # unreachable archive means new captures are being dropped.
            busy = isinstance(e, StorageBusyError)
            payload["archive_error"] = "busy" if busy else "timeout"
            parts.append("archive UNREACHABLE ("
                         + ("its earlier calls never returned" if busy else "statfs timed out")
                         + ")")
            if status == "pass":
                status = "warn"
        except OSError:
            payload["archive_error"] = "unreadable"
            parts.append("archive unreadable")

        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status=status, started_at=t0, finished_at=utcnow(),
            summary=" · ".join(parts), payload=payload,
            metrics={"used_pct": pct, "free_gb": local["free_gb"]},
        )


register(Layer0DiskCheck())


# ---------------------------------------------------------------------------
# Backup freshness
# ---------------------------------------------------------------------------
#
# The disk check above exists because nobody was watching the box doing the
# watching. This is the same argument one step further: nobody is watching the
# backups either.
#
# A cron backup fails silently by construction. `ops/backup.sh` writes its
# outcome to status.json on every run — success or failure, including an
# unexpected abort — and this check reads it. Without this, the first sign that
# backups stopped working is needing one.
#
# Deliberately `skip` rather than `fail` when the status file is absent. Not
# every deployment mounts the backup directory into the backend, and a check
# that fails on an unconfigured optional feature trains operators to ignore it.
# The distinction that matters is "configured and broken" versus "not
# configured", and those must not look the same.

BACKUP_STATUS_PATH = Path(
    os.environ.get("SENTINEL_BACKUP_STATUS_PATH", "/data/backups/status.json")
)
# A daily backup that has not run in 36h has missed one and is into the second.
# Warn there; fail at 3 days, by which point the newest restore point is old
# enough to lose real data.
BACKUP_WARN_AGE_H = 36
BACKUP_FAIL_AGE_H = 72


def _read_backup_status(path: Path) -> dict | None:
    try:
        with path.open("r") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


_STATUS_RANK = {"pass": 0, "warn": 1, "fail": 2}


def _grade_copy(
    data: dict[str, object], now: datetime
) -> tuple[str, str, float | None] | None:
    """Grade the copy of the dumps to SENTINEL_BACKUP_COPY_DIR, if backup.sh made one.

    A failed copy warns while the newest checked copy is younger than the dump's
    own fail age, and fails past it or when no copy was ever checked. The dump
    and the copy are graded apart, since a share that is down leaves the local
    dumps good and the off-host history going stale.

    Args:
        data: The parsed status.json.
        now: The current time, UTC.

    Returns:
        (status, summary, age of the newest checked copy in hours), or None when
        the run made no copy or the copy succeeded.
    """
    copy_status = str(data.get("copy_status") or "off")
    if copy_status in ("off", "ok"):
        return None
    newest_at = str(data.get("copy_newest_at") or "")
    try:
        when = datetime.fromisoformat(newest_at.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age_h: float | None = (now - when).total_seconds() / 3600.0
    except ValueError:
        age_h = None
    detail = str(data.get("copy_detail") or "no detail")
    if age_h is None:
        return "fail", f"backup copy FAILED and no copy was ever checked: {detail}", None
    # A future time means a clock was wrong when the copy was checked, so its
    # real age is unknown, and an unknown age must not keep reading as a warn.
    if age_h < 0:
        return "fail", f"backup copy FAILED, newest checked copy is future-dated: {detail}", None
    status = "fail" if age_h >= BACKUP_FAIL_AGE_H else "warn"
    return status, f"backup copy FAILED, newest checked copy {age_h:.0f}h old: {detail}", age_h


def _with_copy(
    data: dict[str, object], status: str, msg: str
) -> tuple[str, str, float | None]:
    """Fold the copy's grade into the dump's, the worse status and its reason first.

    Args:
        data: The parsed status.json.
        status: The dump's status.
        msg: The dump's summary.

    Returns:
        (status, summary, age of the newest checked copy in hours, or None).
    """
    copy = _grade_copy(data, utcnow())
    if copy is None:
        return status, msg, None
    copy_status, copy_msg, copy_age_h = copy
    if _STATUS_RANK[copy_status] >= _STATUS_RANK[status]:
        return copy_status, copy_msg, copy_age_h
    return status, f"{msg}; {copy_msg}", copy_age_h


@register
class Layer0BackupCheck(Check):
    """Is the database backup actually running, and recent?"""

    id         = "layer0.self.backup"
    stage      = "L0"
    target     = "sentinel-backup"
    cadence_s  = 900
    # Nothing upstream: a broken backup is our problem regardless of radarca.
    depends_on: list[str] = []

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        path = BACKUP_STATUS_PATH

        # Blocking I/O, and the path may be a hung network mount — the same
        # hazard the disk check guards against.
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(_read_backup_status, path), timeout=10
            )
        except asyncio.TimeoutError:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="warn", started_at=t0, finished_at=utcnow(),
                summary=f"timed out reading {path} — backup volume may be hung",
                payload={"path": str(path), "reason": "timeout"},
            )

        if data is None:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="skip", started_at=t0, finished_at=utcnow(),
                summary="backup monitoring not configured",
                payload={
                    "path": str(path),
                    "reason": "no_status_file",
                    "hint": "Run ops/backup.sh on the host and mount its "
                            "SENTINEL_BACKUP_DIR read-only at /data/backups, "
                            "or set SENTINEL_BACKUP_STATUS_PATH.",
                },
            )

        finished = data.get("finished_at") or ""
        try:
            when = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            age_h = (utcnow() - when).total_seconds() / 3600.0
        except (ValueError, AttributeError):
            when, age_h = None, None

        reported = str(data.get("status", "unknown"))
        detail   = str(data.get("detail", ""))
        payload  = {
            "path": str(path),
            "reported_status": reported,
            "detail": detail,
            "finished_at": finished,
            "age_hours": round(age_h, 1) if age_h is not None else None,
            "artifact": data.get("artifact", ""),
            "bytes": data.get("bytes", 0),
        }

        # The last run failing is worse than it being old: it means backups
        # are actively broken now, not merely overdue.
        if reported != "ok":
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="fail", started_at=t0, finished_at=utcnow(),
                summary=f"last backup FAILED: {detail or 'no detail'}",
                payload=payload,
                metrics={"backup_age_hours": age_h} if age_h is not None else {},
            )

        if age_h is None:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="warn", started_at=t0, finished_at=utcnow(),
                summary=f"backup status has an unreadable timestamp: {finished!r}",
                payload=payload,
            )

        if age_h >= BACKUP_FAIL_AGE_H:
            status, msg = "fail", f"no successful backup for {age_h:.0f}h"
        elif age_h >= BACKUP_WARN_AGE_H:
            status, msg = "warn", f"last backup was {age_h:.0f}h ago — a run has been missed"
        else:
            status, msg = "pass", f"last backup {age_h:.1f}h ago ({detail})"

        metrics = {"backup_age_hours": age_h,
                   "backup_bytes": float(data.get("bytes", 0) or 0)}
        payload.update({k: data.get(k, "") for k in
                        ("copy_dir", "copy_status", "copy_detail", "copy_newest",
                         "copy_newest_at")})
        status, msg, copy_age_h = _with_copy(data, status, msg)
        if copy_age_h is not None:
            metrics["backup_copy_age_hours"] = copy_age_h

        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status=status, started_at=t0, finished_at=utcnow(),
            summary=msg, payload=payload, metrics=metrics,
        )
