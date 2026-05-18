"""Notification-schedule evaluator for groups.

A group's `schedule` is a JSONB blob with this shape::

    {
      "kind":          "always" | "weekly" | "biweekly",

      // weekly + biweekly: bit-mask of allowed weekdays. Monday=0..Sunday=6.
      "weekdays":      [0, 1, 2, 3, 4],         // mon-fri

      // weekly + biweekly: list of [start, end] time-of-day pairs (UTC,
      // 24-hour, "HH:MM"). Multiple pairs allowed: lunch break, on-call
      // windows, etc. Empty list = whole day when weekday matches.
      "time_windows":  [["08:00", "17:00"]],

      // biweekly only: anchor date ("YYYY-MM-DD"). On-weeks are those
      // where `(now.date() - anchor).days // 7 % 2 == 0`. Default = group
      // creation date, but admin can pick any reference (e.g. align with
      // an existing security on-call rotation).
      "anchor_date":   "2026-05-18",

      // Optional downtime windows that override everything else — even an
      // active weekly window won't notify if "now" falls inside a downtime.
      // [{start: ISO 8601, end: ISO 8601}, ...]. Useful for vacations,
      // planned maintenance, conferences.
      "downtime":      [{"start": "2026-06-01T00:00Z", "end": "2026-06-08T00:00Z"}],

      // Recurring blackouts that repeat weekly (or daily if weekdays
      // covers 0..6). Each entry suppresses notifications whose
      // weekday-and-time matches. Common pattern: nightly quiet hours,
      // weekend-only paging, etc. Overnight windows wrap midnight when
      // start > end.
      "recurring_downtime": [
        {"weekdays": [0, 1, 2, 3, 4, 5, 6], "time_window": ["22:00", "06:00"]}
      ]
    }

Inheritance: a child group's effective schedule is layered on top of its
parent's. The intent is "I inherit the parent's on-windows AND add my own
constraints" — implemented as an AND of both is_active_at() decisions.
Downtime in either side suppresses the notification.

Empty / missing schedule == "always on".
"""
from __future__ import annotations
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _in_downtime(schedule: dict, when: datetime) -> bool:
    for w in schedule.get("downtime") or []:
        try:
            s = datetime.fromisoformat(str(w["start"]).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(w["end"]).replace("Z", "+00:00"))
        except Exception:
            continue
        if s <= when < e:
            return True
    return False


def _in_recurring_downtime(schedule: dict, when: datetime) -> bool:
    """Weekly-recurring blackouts. Each entry: {weekdays: [0..6], time_window: [hh:mm, hh:mm]}.
    Overnight windows (start > end) wrap midnight; in that case we count
    EITHER side of midnight as in-downtime based on the calendar day the
    start belongs to.
    """
    for w in schedule.get("recurring_downtime") or []:
        try:
            weekdays = w.get("weekdays") or list(range(7))
            window = w.get("time_window") or []
            if len(window) != 2:
                continue
            start = _parse_hhmm(window[0])
            end   = _parse_hhmm(window[1])
        except Exception:
            continue
        t = when.time()
        wd_today = when.weekday()
        if start <= end:
            if wd_today in weekdays and start <= t < end:
                return True
        else:
            # Overnight: start-23:59 belongs to "today", 00:00-end belongs to "tomorrow".
            if wd_today in weekdays and t >= start:
                return True
            wd_yest = (wd_today - 1) % 7
            if wd_yest in weekdays and t < end:
                return True
    return False


def _matches_weekly_window(schedule: dict, when: datetime) -> bool:
    weekdays = schedule.get("weekdays")
    if weekdays is not None and when.weekday() not in weekdays:
        return False
    windows = schedule.get("time_windows") or []
    if not windows:
        return True  # whole day
    t = when.time()
    for pair in windows:
        try:
            start = _parse_hhmm(pair[0])
            end   = _parse_hhmm(pair[1])
        except Exception:
            continue
        # Overnight window (start > end) wraps midnight.
        if start <= end:
            if start <= t < end:
                return True
        else:
            if t >= start or t < end:
                return True
    return False


def _on_week_for_biweekly(schedule: dict, when: datetime) -> bool:
    anchor_str = schedule.get("anchor_date")
    if not anchor_str:
        return True  # no anchor → treat as weekly (every week is on-week)
    try:
        anchor = date.fromisoformat(str(anchor_str))
    except Exception:
        return True
    delta_days = (when.date() - anchor).days
    return (delta_days // 7) % 2 == 0


def _is_active_single(schedule: dict, when: datetime) -> bool:
    """Evaluate one schedule against `when` (UTC). Empty schedule = always."""
    if not schedule:
        return True
    if _in_downtime(schedule, when):
        return False
    if _in_recurring_downtime(schedule, when):
        return False
    kind = schedule.get("kind") or "always"
    if kind == "always":
        return True
    if kind == "weekly":
        return _matches_weekly_window(schedule, when)
    if kind == "biweekly":
        if not _on_week_for_biweekly(schedule, when):
            return False
        return _matches_weekly_window(schedule, when)
    # Unknown kind — fail-safe to "active" so an invalid schedule doesn't
    # silently drop alerts. The admin UI will surface a warning.
    log.warning("unknown schedule kind %r — treating as always", kind)
    return True


async def effective_is_active(pool, group_id: int, when: datetime) -> bool:
    """Walk the parent chain and AND every schedule's verdict.

    A child group inherits its parent's downtime / on-windows. If either
    is inactive (or in downtime) the result is inactive. Cycles are
    defended against by capping the chain at 8 levels.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seen: set[int] = set()
    cur_id: int | None = group_id
    for _ in range(8):
        if cur_id is None or cur_id in seen:
            return True
        seen.add(cur_id)
        row = await pool.fetchrow(
            "SELECT schedule, parent_group_id FROM groups WHERE id = $1",
            cur_id,
        )
        if row is None:
            return True
        schedule = row["schedule"] or {}
        if isinstance(schedule, str):
            import json as _json
            schedule = _json.loads(schedule)
        if not _is_active_single(schedule, when):
            return False
        cur_id = row["parent_group_id"]
    log.warning("group %s schedule chain capped at 8 — cycle?", group_id)
    return True


def is_active_at(schedule: dict, when: datetime) -> bool:
    """Synchronous single-schedule eval. Used by previews and tests."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return _is_active_single(schedule, when)


def next_on_windows(schedule: dict, start: datetime, *, count: int = 5,
                    step_minutes: int = 15) -> list[tuple[datetime, datetime]]:
    """Return up to `count` upcoming contiguous on-windows starting from
    `start`. Used by the admin UI to show "next 5 windows" so the operator
    can sanity-check the schedule before saving.

    Scans forward in `step_minutes` increments — coarse enough that 5
    windows over the next month finishes in ~3000 iterations.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    out: list[tuple[datetime, datetime]] = []
    cur = start
    cur_active = is_active_at(schedule, cur)
    window_start = cur if cur_active else None
    # Scan up to 60 days forward.
    for _ in range(60 * 24 * 60 // step_minutes):
        nxt = cur + timedelta(minutes=step_minutes)
        nxt_active = is_active_at(schedule, nxt)
        if cur_active and not nxt_active and window_start:
            out.append((window_start, nxt))
            window_start = None
            if len(out) >= count:
                break
        elif not cur_active and nxt_active:
            window_start = nxt
        cur, cur_active = nxt, nxt_active
    if cur_active and window_start and len(out) < count:
        out.append((window_start, cur))
    return out
