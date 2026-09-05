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


# ---------------------------------------------------------------------------
# Inconclusive runs
# ---------------------------------------------------------------------------
#
# The scheduler rewrites some fail/error results to ``skip`` so the dashboard
# doesn't paint 37 downstream cells red over one upstream fault. Those rows are
# NOT observations of health — they mean "we declined to judge". The original
# verdict is preserved in payload.original_status.
#
# Everything that reads a skip as good news has to know the difference, or a
# target that is permanently down manufactures alerts: the demoted skip closes
# its alarm (reads as recovery), the next real fail opens a *new* alarm, and
# the escalation policy starts over from step 1. Observed on XEBY, where 17 of
# 17 alarms closed in the 7 days to 2026-09-05 were closed by a demoted skip
# and none by an actual pass — while the radar never came back at all.
INCONCLUSIVE_SKIP_REASONS = frozenset({
    "upstream_unhealthy",     # a depends_on ancestor is fail/error
    "local_dns_error",        # our resolver, not their server
    "local_network_offline",  # Sentinel's own uplink is down
})


def is_inconclusive(status: str, payload: dict | None) -> bool:
    """True when this run declined to judge rather than observing health.

    Only ``skip`` can be inconclusive. An intrinsic skip — a check that ran
    and legitimately had nothing to assess — carries no reason (or one not in
    the set above) and stays truthy-good, so recovery still closes instantly.
    """
    if status != "skip":
        return False
    if not payload:
        return False
    return payload.get("reason") in INCONCLUSIVE_SKIP_REASONS
