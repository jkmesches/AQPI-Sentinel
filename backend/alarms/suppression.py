"""Dependency-graph suppression.

If a check's ``depends_on`` lists another check that is currently failing,
the dependent's alarm is recorded but its notifications are suppressed.
"""
from __future__ import annotations
from typing import Iterable


def compute_suppression(
    check_id: str, latest_status_by_check: dict[str, str], depends_on_index: dict[str, list[str]]
) -> str | None:
    """Walk ``depends_on`` ancestors. Return the id of the first ancestor
    whose latest status is ``fail`` or ``error``, else None.

    Cycle-safe via a visited set.
    """
    visited: set[str] = set()
    frontier = list(depends_on_index.get(check_id, []))
    while frontier:
        nxt: list[str] = []
        for dep_id in frontier:
            if dep_id in visited:
                continue
            visited.add(dep_id)
            st = latest_status_by_check.get(dep_id)
            if st in ("fail", "error"):
                return dep_id
            nxt.extend(depends_on_index.get(dep_id, []))
        frontier = nxt
    return None


def build_depends_on_index(
    checks: Iterable, *, include_alarm_only: bool = False
) -> dict[str, list[str]]:
    """Map check_id -> dependency ids.

    `include_alarm_only` adds each check's ``alarm_only_depends_on``. The
    AlarmEngine wants those (they should suppress duplicate pages); the
    scheduler must NOT (they should not demote a real verdict to skip). See
    Check.alarm_only_depends_on for why the distinction exists.
    """
    out: dict[str, list[str]] = {}
    for c in checks:
        deps = list(c.depends_on)
        if include_alarm_only:
            deps += list(getattr(c, "alarm_only_depends_on", None) or [])
        out[c.id] = deps
    return out
