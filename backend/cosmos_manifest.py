"""The CoSMoS cycle manifest: what one cycle did, and whether it explains a gap.

The driver on the model host writes one manifest after every cycle, whatever
its outcome, through a temp file and a rename, so a reader never sees half a
file (wiki plans/74- §3). This module turns the parsed JSON into the few facts
the checks need and holds the rules that judge them. It reads no files and
no clock, so each rule runs in a test without a mount.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import COSMOS_GAP_CEILING_S, COSMOS_MANIFEST_WARN_S, COSMOS_SKIP_OUTCOMES

# Item 3's verdict per outcome (74- §4 item 3, with the stand-down added on
# 2026-11-22). A skip is a cycle declining on purpose, and the two failures
# lost a forecast or failed to publish one.
OUTCOME_STATUS: dict[str, str] = {
    "ran": "pass",
    "skipped_incomplete_hrrr": "warn",
    "skipped_warmstart_running": "warn",
    "no_forecast_exit1": "fail",
    "publish_failed": "fail",
}

# "warmstart holds model/active since 2026-11-22 18:25:01.785167311 -0700":
# local time from `date`, with nanoseconds and a numeric offset.
_LOCK_RE = re.compile(
    r"since (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:\.(\d+))? ([+-])(\d{2})(\d{2})\b")


class ManifestError(ValueError):
    """A manifest that lacks a field the checks read, or holds one they cannot parse."""


@dataclass(frozen=True)
class CycleManifest:
    """The fields of one manifest that the checks judge."""

    metfile: str
    wrote_at: datetime
    outcome: str
    reason: str
    file_count: int | None
    file_count_expected: int | None
    lock_since: datetime | None


def _int_or_none(value: object) -> int | None:
    """An integer field, or None for a missing or non-integer one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def parse_lock_time(reason: str) -> datetime | None:
    """When the warmstart took the model lock, read from a stand-down reason.

    Args:
        reason: The manifest's `reason`.

    Returns:
        The lock time in UTC, with the fraction cut to microseconds, or None
        when the reason names no time this can read.
    """
    m = _LOCK_RE.search(reason)
    if m is None:
        return None
    hours, minutes = int(m.group(5)), int(m.group(6))
    if hours > 23 or minutes > 59:
        return None
    frac = (m.group(3) or "")[:6].ljust(6, "0")
    sign = 1 if m.group(4) == "+" else -1
    zone = timezone(sign * timedelta(hours=hours, minutes=minutes))
    try:
        local = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}.{frac}")
        # Year 1 or 9999 overflows once the offset moves it.
        return local.replace(tzinfo=zone).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def parse_manifest(doc: Any) -> CycleManifest:  # noqa: ANN401  parsed JSON of unknown shape
    """Read the fields the checks judge out of a parsed manifest.

    Args:
        doc: The manifest as json.load returned it.

    Returns:
        The parsed manifest.

    Raises:
        ManifestError: If `doc` is not an object, or its `wrote_at` or
            `outcome` is missing or unreadable. `wrote_at` must carry a zone,
            because a naive time cannot be placed against this host's clock.
    """
    if not isinstance(doc, dict):
        raise ManifestError("manifest is not a JSON object")
    wrote = doc.get("wrote_at")
    if not isinstance(wrote, str) or not wrote:
        raise ManifestError("manifest has no wrote_at")
    try:
        wrote_at = datetime.fromisoformat(wrote.replace("Z", "+00:00"))
        if wrote_at.tzinfo is None:
            raise ManifestError(f"wrote_at carries no zone: {wrote!r}")
        wrote_at = wrote_at.astimezone(timezone.utc)
    except (ValueError, OverflowError) as e:
        raise ManifestError(f"wrote_at is not a usable time: {wrote[:64]!r}") from e
    outcome = doc.get("outcome")
    if not isinstance(outcome, str) or not outcome:
        raise ManifestError("manifest has no outcome")
    reason = doc.get("reason")
    reason = reason if isinstance(reason, str) else ""
    metfile = doc.get("metfile")
    return CycleManifest(
        metfile=metfile if isinstance(metfile, str) else "",
        wrote_at=wrote_at,
        outcome=outcome,
        reason=reason,
        file_count=_int_or_none(doc.get("file_count")),
        file_count_expected=_int_or_none(doc.get("file_count_expected")),
        lock_since=(parse_lock_time(reason)
                    if outcome == "skipped_warmstart_running" else None),
    )


def hrrr_complete(m: CycleManifest) -> bool | None:
    """Whether the cycle had its full HRRR forcing set.

    Complete means at least the expected count. HRRR's extended t00z, t06z,
    t12z and t18z runs publish 20 files against 19 expected, and the driver's
    gate refuses only fewer than 19.

    Args:
        m: The manifest.

    Returns:
        True or False, or None when either count is missing.
    """
    if m.file_count is None or m.file_count_expected is None:
        return None
    return m.file_count >= m.file_count_expected


def manifest_age_s(m: CycleManifest, now: datetime) -> float:
    """Seconds since the manifest was written, negative if dated ahead of `now`."""
    return (now - m.wrote_at).total_seconds()


def in_window(m: CycleManifest, now: datetime) -> bool:
    """Whether the manifest can speak for the current cycle.

    It must be under 150 min old, past which the hourly driver has missed a
    write it should have made, and not dated ahead of this host's clock,
    where its age cannot be known.

    Args:
        m: The manifest.
        now: The time of the check.

    Returns:
        True when its outcome describes the current cycle.
    """
    return 0 <= manifest_age_s(m, now) < COSMOS_MANIFEST_WARN_S


def explained_gap(m: CycleManifest | None, now: datetime, product_age_s: float,
                  ) -> str | None:
    """Why a late CoSMoS product is late, when the manifest vouches for it.

    The rule Joseph set on 2026-11-23 (74- item 3). The manifest must be
    fresh, its outcome one of the two skips, and the product no older than
    the ceiling. A manifest dated ahead of this host's clock vouches for
    nothing, since its age cannot be known. That needs no tolerance for small
    skew: a manifest a second ahead reads so for at most one run after it is
    written, and the alarm hold-down absorbs one run. A NaN product age is
    never explained.

    Args:
        m: The newest manifest, or None when it could not be read.
        now: The time of the check.
        product_age_s: How old the product's newest frame is.

    Returns:
        The reason to report, or None when the gap stays unexplained.
    """
    if m is None or m.outcome not in COSMOS_SKIP_OUTCOMES:
        return None
    if not in_window(m, now):
        return None
    if not product_age_s <= COSMOS_GAP_CEILING_S:
        return None
    return m.reason or m.outcome
