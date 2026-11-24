"""Alarms that are recorded and never notify anyone.

Two rules make an alarm record-only. It opens, escalates in severity and
shows on the timeline and the dashboard as any other does, but it never
sends email, webhook or web push. The daily report lists no alarms and none
of these checks, so it needs no rule of its own.

1. Its target is a NEXRAD radar. Joseph, 2026-11-23: users must not receive
   warnings about the NEXRAD radars.
2. Its check is on a trial (config.RECORD_ONLY_TRIAL_CHECKS) and the alarm
   opened within the trial's first days, counted from the check's first
   stored run on this deployment. An alarm that opened during the trial stays
   record-only for the rest of its life, so the day the trial ends does not
   release a burst of alarms that opened quietly before it.

Three paths send or would send, and all ask this module: the engine's step
loop writes a `record-only` row in notification_log where each message would
have gone, as a disabled tier does, the web push listener on alarm_open
returns early, and the admin test alert reports instead of sending. A fourth
sender added later must ask here as well.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from ..config import (  # noqa: TID252  the package imports relatively throughout
    NEXRAD_RADARS, RECORD_ONLY_TRIAL_CHECKS, RECORD_ONLY_TRIAL_DAYS)

# notification_log's status for a step this rule kept quiet.
STATUS = "record-only"

TRIAL = timedelta(days=RECORD_ONLY_TRIAL_DAYS)

# Each trial check's first run on this deployment. Process state, because the
# rule is asked from three call sites that hold no database handle. The engine
# seeds it at start from the start it stored in `settings`, falling back to the
# earliest row in check_runs, and stores each new first run. Retention trims
# check_runs from the old end, so without the stored start a restart months
# later would move a trial's start forward and quiet an alarm that has long
# been notifying.
_FIRST_RUN: dict[str, datetime] = {}


def seed(first_runs: Mapping[str, datetime]) -> None:
    """Load each trial check's first stored run, keeping the earlier of two.

    Args:
        first_runs: Check id to the started_at of its first stored run.
    """
    for check_id, started_at in first_runs.items():
        # The caller stores what it seeds, so whether this moved is not needed.
        _ = note_run(check_id, started_at)


def note_run(check_id: str, started_at: datetime) -> bool:
    """Record a run, which starts a trial check's clock if it is the first.

    Called on every result the engine evaluates, so it never raises: a time
    without a timezone is taken as UTC, as the rest of the backend does.

    Args:
        check_id: The check that ran.
        started_at: When the run started.

    Returns:
        True when this run set the check's first run, which the caller then
        stores so a restart finds it.
    """
    if check_id not in RECORD_ONLY_TRIAL_CHECKS:
        return False
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    known = _FIRST_RUN.get(check_id)
    if known is None or started_at < known:
        _FIRST_RUN[check_id] = started_at
        return True
    return False


def trial_ends(check_id: str) -> datetime | None:
    """When a trial check's trial ends, or None if it has not run yet.

    Args:
        check_id: A check id.

    Returns:
        The end of the trial, or None for a trial check with no run and for
        any check that is not on a trial.
    """
    first = _FIRST_RUN.get(check_id)
    return None if first is None else first + TRIAL


def reason(alarm: Mapping[str, object]) -> str | None:
    """Why an alarm is recorded without notifying anyone, if it is.

    Args:
        alarm: An alarm row or an alarm_open payload, carrying `target`,
            `check_id` and `opened_at`.

    Returns:
        A sentence for notification_log and the admin page, or None when the
        alarm notifies as usual.
    """
    target = str(alarm.get("target") or "")
    if target in NEXRAD_RADARS:
        return "alarms on NEXRAD radars are recorded, never sent"
    check_id = str(alarm.get("check_id") or "")
    if check_id not in RECORD_ONLY_TRIAL_CHECKS:
        return None
    ends = trial_ends(check_id)
    if ends is None:
        # Opened before the engine saw a run, so inside any trial it will have.
        return f"{check_id} has not run here yet and starts on a record-only trial"
    opened = alarm.get("opened_at")
    opened_at = opened if isinstance(opened, datetime) else datetime.now(timezone.utc)
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    if opened_at < ends:
        return (f"{check_id} opened during its record-only trial, which ends "
                f"{ends:%Y-%m-%d %H:%M}Z")
    return None


def record_only(alarm: Mapping[str, object]) -> bool:
    """Whether an alarm is recorded without notifying anyone.

    Args:
        alarm: An alarm row or an alarm_open payload.

    Returns:
        True for an alarm on a NEXRAD radar, or one that opened during its
        check's record-only trial.
    """
    return reason(alarm) is not None
