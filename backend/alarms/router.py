"""Match an alarm against the route table; resolve its policy + severity.

First-match wins. Severity computation:
  - status == 'warn'  → 'warn'
  - status == 'fail'  → 'warn' for first 30 min, then 'critical' (duration promo)
  - status == 'error' → 'warn'
Route ``severity_floor`` raises the severity if the floor is higher.
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
    if status == "warn":
        return "warn"
    if status == "fail":
        return "warn"     # promoted by duration in :func:`compute_severity`
    if status == "error":
        return "warn"
    return "info"


def _matches(matchers: dict[str, str], alarm: dict) -> bool:
    for k, v in matchers.items():
        a_val = alarm.get(k)
        if a_val is None or str(a_val) != str(v):
            return False
    return True


def _max_sev(a: str, b: str | None) -> str:
    if not b:
        return a
    return a if _SEV_RANK[a] >= _SEV_RANK[b] else b


class Router:
    def __init__(self, cfg: AlertsConfig):
        self.cfg = cfg

    # --------------- matching -----------------------------------------
    def match(self, alarm: dict, ctx: dict[str, Any]) -> Route | None:
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
        sev = severity_for_status(alarm.get("status_at_open", "warn"))
        opened: datetime = alarm["opened_at"]
        # duration-based promotion: fail → critical after PROMOTE_AFTER_S
        if (now - opened).total_seconds() >= PROMOTE_AFTER_S and \
           alarm.get("status_at_open") == "fail":
            sev = _max_sev("critical", sev)
        # apply floor
        if route and route.severity_floor:
            sev = _max_sev(route.severity_floor, sev)
        return sev


def reload(path: str | None = None) -> Router:
    from .models import load_config
    return Router(load_config(path))
