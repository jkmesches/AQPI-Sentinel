"""Visible 'do we have internet?' check.

Surfaces the NetworkMonitor's current state as a regular check in the
registry, so it appears in /status, the timeline, and the alarm engine like
any other probe. When local connectivity is down, every other check is
demoted to `skip`; only this one shows red.
"""
from __future__ import annotations

from ..registry import register
from .base import Check, CheckResult, utcnow


class Layer0NetworkControlCheck(Check):
    id         = "layer0.net.control"
    target     = "internet"
    stage      = "L0"
    cadence_s  = 30
    depends_on: list[str] = []

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        net = getattr(ctx, "network", None)
        if net is None:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="skip",
                started_at=t0, finished_at=utcnow(),
                summary="no network monitor configured",
                payload={"reason": "no_monitor"},
            )
        snap   = net.snapshot()
        status = "pass" if snap["online"] else "fail"
        probes = snap.get("results") or []
        parts  = [f"{p['name']}={p['status'] if p['ok'] else 'x'}" for p in probes]
        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status=status,
            started_at=t0, finished_at=utcnow(),
            summary=("online · " if snap["online"] else "OFFLINE · ") + " ".join(parts),
            payload=snap,
        )


register(Layer0NetworkControlCheck())
