"""Alarms that are recorded and never notify anyone.

Joseph, 2026-11-23: users must not receive warnings about the NEXRAD radars.
A NEXRAD site missing from the composite is still a fact about the product,
so its alarms open as any other does. They escalate in severity and show on
the timeline, the dashboard and in the daily report, but they never send
email, webhook or web push.

Two paths send, and both ask this module. The engine's step loop writes a
`record-only` row in notification_log where each message would have gone, as
a disabled tier does, and the web push listener on alarm_open returns early.
A third sender added later must ask here as well, or it notifies about the
radars this rule exists to keep quiet.
"""
from __future__ import annotations

from collections.abc import Mapping

from ..config import NEXRAD_RADARS  # noqa: TID252  the package imports relatively throughout

# notification_log's status for a step this rule kept quiet.
STATUS = "record-only"


def record_only(alarm: Mapping[str, object]) -> bool:
    """Whether an alarm is recorded without notifying anyone.

    Args:
        alarm: An alarm row or an alarm_open payload, either carrying `target`.

    Returns:
        True for an alarm on a NEXRAD radar.
    """
    return str(alarm.get("target") or "") in NEXRAD_RADARS
