"""Visible "is our infra OK?" checks.

Split into two so the operator can tell internet reachability and DNS
resolution apart at a glance — they fail with very different symptoms
upstream (timeouts vs. `[Errno -5]` markers) and demand different
remediation. NetworkMonitor (network.py) tracks both states; these
classes surface them as regular registry checks.

When local connectivity is down, every other check is demoted to
`skip` via the scheduler's _maybe_downgrade_for_network pass; only the
relevant Sentinel-* check below shows red.
"""
from __future__ import annotations

from ..registry import register
from .base import Check, CheckResult, utcnow


class Layer0InternetControlCheck(Check):
    id         = "layer0.net.internet"
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


class Layer0DnsControlCheck(Check):
    """Resolver-health probe. Surfaces NetworkMonitor.dns_ok as a check so
    DNS issues (which manifest as `[Errno -5]` markers downstream and get
    swallowed by the DNS-flake demote) are still VISIBLE on the dashboard
    — one row, plainly named, with the failing resolver targets in the
    summary so the operator knows whether to look at our own resolver or
    the WAN."""

    id         = "layer0.net.dns"
    target     = "dns"
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
        ok     = bool(snap.get("dns_ok"))
        status = "pass" if ok else "fail"
        probes = snap.get("dns_results") or []
        parts  = [f"{p['name']}={'ok' if p['ok'] else 'x'}" for p in probes]
        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status=status,
            started_at=t0, finished_at=utcnow(),
            summary=("resolver ok · " if ok else "RESOLVER FAIL · ") + " ".join(parts),
            payload={
                "dns_ok":               ok,
                "dns_consecutive_fail": snap.get("dns_consecutive_fail"),
                "dns_results":          probes,
            },
        )


register(Layer0InternetControlCheck())
register(Layer0DnsControlCheck())
