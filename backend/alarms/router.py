"""Match an alarm against the route table; resolve its policy + severity.

First-match wins. Severity model (v0.1.2+):

  status        initial severity                     notes
  --------      ------------------                   -----
  warn          info                                 Degraded but not broken
                                                     ("requires attention")
  fail          warn → critical after 30 min        Broken
                                                     ("requires action")
  error         warn → critical after 30 min        Check itself crashed
                                                     (transport timeout — usually
                                                     means upstream is unreachable)
  pass / skip   no alarm opens                       —

This gives operators three meaningful tiers via severity_floor:
  - floor=info       → notify on everything (warn-status included)
  - floor=warn       → notify only on broken (fail/error)
  - floor=critical   → notify only on long-running outages (>30 min open)

Route ``severity_floor`` raises the severity if the floor is higher.

Prior model (v0.1.1-): warn/fail/error all mapped to severity=warn, making
severity_floor unable to distinguish "degraded" from "broken." Reshaped in
v0.1.2 so the routing tier maps cleanly onto operational priority.

Severity tracks the CURRENT status, not the status that opened the alarm.
Until 2026-09-15 it read `status_at_open` only, which made severity a
property of the first bad sample rather than of the condition — an alarm
that opened on a warn could never escalate however far the thing fell
afterwards. It cost 17h 30m of `info` on a product serving no data. See
compute_severity.

Escalation is one-way. Severity rises with the condition and falls only when
the alarm closes.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import conditions
from .models import AlertsConfig, Route

log = logging.getLogger(__name__)

# Severity ranking (higher = worse).
_SEV_RANK = {"info": 0, "warn": 1, "critical": 2}

# How long an alarm at 'fail' status stays 'warn' before auto-promoting to
# 'critical'. Configurable per-instance later; constant for v1.
PROMOTE_AFTER_S = 30 * 60


def severity_for_status(status: str) -> str:
    """Map a check's raw status to the alarm's initial severity.

    The mapping aligns the routing tier with operational priority:
    warn = "degraded but not broken" → info severity (informational).
    fail/error = "broken" → warn severity, auto-promoted to critical
    after PROMOTE_AFTER_S via compute_severity. See module docstring.
    """
    if status == "warn":
        return "info"     # degraded — informational
    if status == "fail":
        return "warn"     # broken — promoted to critical at 30 min
    if status == "error":
        return "warn"     # check crashed — same promote path as fail
    return "info"


def _matches(matchers: dict[str, str], alarm: dict) -> bool:
    for k, v in matchers.items():
        a_val = alarm.get(k)
        if a_val is None or str(a_val) != str(v):
            return False
    return True


def flatten_alarm(alarm: dict) -> dict:
    """Give an ``alarms`` row the flat shape route matchers expect.

    ``status_at_open`` is written into the payload JSONB at open time, but
    routes match it as a top-level key — which is also how ``evaluate()``
    passes it when resolving the hold-down. The two call sites disagreed:
    a route keyed on ``status_at_open`` matched at open time and then
    silently matched NOTHING at dispatch time, because ``_matches`` does a
    flat ``alarm.get(k)`` and the row keeps it one level down.

    The failure is invisible from the outside — no error, no log line, just
    a route that never fires. Production ran a single ``{status_at_open:
    error}`` route and sent zero notifications for 537 matching alarms.

    Returns a shallow copy; the row is left alone. Top-level keys win, so a
    real column always beats a payload key of the same name.
    """
    payload = alarm.get("payload")
    if isinstance(payload, str):  # defensive: un-decoded JSONB
        import json
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = None
    if not isinstance(payload, dict):
        return alarm
    merged = {k: v for k, v in payload.items() if k not in alarm}
    if not merged:
        return alarm
    return {**merged, **alarm}


def _max_sev(a: str, b: str | None) -> str:
    if not b:
        return a
    return a if _SEV_RANK[a] >= _SEV_RANK[b] else b


class Router:
    def __init__(self, cfg: AlertsConfig):
        self.cfg = cfg

    # --------------- matching -----------------------------------------
    def match(self, alarm: dict, ctx: dict[str, Any]) -> Route | None:
        alarm = flatten_alarm(alarm)
        for r in self.cfg.routes:
            if not _matches(r.match, alarm):
                continue
            if not conditions.evaluate(r.when, ctx):
                continue
            return r
        return None

    # --------------- severity -----------------------------------------
    def compute_severity(
        self, alarm: dict, route: Route | None, now: datetime
    ) -> str:
        alarm = flatten_alarm(alarm)
        at_open = alarm.get("status_at_open", "warn")
        # `status_now` is the latest run for this check/target, supplied by the
        # engine each tick. Absent (older callers, tests), fall back to the
        # open-time status — the previous behavior exactly.
        now_status = alarm.get("status_now") or at_open

        # Severity is the worse of what opened the alarm and what is happening
        # now. Reading only `status_at_open` meant an alarm could never
        # escalate past the condition that created it: on 2026-09-13 qpe_1hr
        # opened on an E_step_count warn at 21:41, went to a hard fail four
        # hours later, and stayed at severity `info` for 17h 30m while the
        # product served no data at all. A progressively degrading upstream
        # produces exactly that shape — warn first, fail later — and it is the
        # shape this used to be blind to.
        sev = _max_sev(severity_for_status(at_open),
                       severity_for_status(now_status))

        opened: datetime = alarm["opened_at"]
        # Duration-based promotion: long-running fail/error → critical. Both
        # kinds mean "broken"; if either is still open past PROMOTE_AFTER_S,
        # it's no longer transient and warrants a page-tier severity. A
        # warn-only alarm still stays at info — degraded is not broken — but
        # "warn-only" now means neither then nor now, not merely not then.
        if (now - opened).total_seconds() >= PROMOTE_AFTER_S and (
                at_open in ("fail", "error") or now_status in ("fail", "error")):
            sev = _max_sev("critical", sev)
        # apply floor
        if route and route.severity_floor:
            sev = _max_sev(route.severity_floor, sev)
        # Ratchet. Severity may rise within an alarm's life and must not fall:
        # a check flapping fail→warn while the alarm stays open would
        # otherwise quietly walk the severity back down, and a de-escalation
        # nobody asked for is indistinguishable from the problem going away.
        # The alarm closing is what ends it.
        return _max_sev(sev, alarm.get("severity"))


def reload(path: str | None = None) -> Router:
    from .models import load_config
    return Router(load_config(path))
