"""Side-conditions evaluated by the router. All built-in for v1; extensibility
is a new function added here. Each predicate returns True/False given the
alarm + global context (rest of open alarms).

Context shape:
    {
      "now":           datetime (UTC),
      "open_alarms":   list[dict],     # all currently-open alarms
      "alarm":         dict,           # the candidate alarm
      "latest_runs":   dict[check_id → CheckRun row],
    }
"""
from __future__ import annotations
import operator
import re
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .models import Condition

_OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt,
        "<":  operator.lt, "==": operator.eq, "!=": operator.ne}
_OP_RE = re.compile(r"^\s*(>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")


def _parse_comparison(s: str):
    m = _OP_RE.match(s)
    if not m:
        raise ValueError(f"bad comparison: {s!r}")
    return _OPS[m.group(1)], float(m.group(2))


def _parse_hhmm(s: str) -> time:
    return time(*map(int, s.split(":")))


def _in_window(now_local: datetime, window: str) -> bool:
    start_s, end_s = window.split("-")
    start, end = _parse_hhmm(start_s), _parse_hhmm(end_s)
    t = now_local.time()
    if start <= end:
        return start <= t <= end
    # wraps midnight
    return t >= start or t <= end


def evaluate(cond: Condition | None, ctx: dict[str, Any]) -> bool:
    """Return True if all set fields on Condition hold. None = always true."""
    if cond is None:
        return True
    alarm = ctx["alarm"]
    now: datetime = ctx["now"]

    # time_of_day
    if cond.time_of_day_in or cond.time_of_day_not_in:
        local = now.astimezone(ZoneInfo(cond.timezone))
        if cond.weekday_only and local.weekday() >= 5:
            return False
        if cond.time_of_day_in and not _in_window(local, cond.time_of_day_in):
            return False
        if cond.time_of_day_not_in and _in_window(local, cond.time_of_day_not_in):
            return False

    # duration_at_severity_min
    if cond.duration_at_severity_min is not None:
        opened: datetime = alarm["opened_at"]
        if (now - opened).total_seconds() / 60 < cond.duration_at_severity_min:
            return False

    # count_of_targets_failing — N other open alarms sharing this check_id
    if cond.count_of_targets_failing:
        op, n = _parse_comparison(cond.count_of_targets_failing)
        siblings = sum(
            1 for a in ctx["open_alarms"]
            if a["check_id"] == alarm["check_id"]
        )
        if not op(siblings, n):
            return False

    # metric_above — last value of a named metric on this alarm's check/target
    if cond.metric_above:
        # caller is expected to supply latest metric in ctx if needed; for v1
        # we evaluate against the alarm's payload metrics if present.
        m = cond.metric_above
        val = (alarm.get("payload", {}) or {}).get("metrics", {}).get(m["metric"])
        if val is None or val < m["value"]:
            return False

    return True
