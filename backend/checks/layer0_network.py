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

import socket

from ..errors import humanize_error
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


# ---------------------------------------------------------------------------
# radar-display TLS
# ---------------------------------------------------------------------------
# Sentinel proxies per-elevation tilt imagery from radardisplay.engr.
# colostate.edu. That host's certificate expired in 2026-05, and the proxy
# client was given verify=False to keep the imagery flowing. The certificate
# was renewed on 2026-07-14 — and nothing noticed, so verification stayed off
# for about seven weeks after it could have been restored.
#
# That is the failure this check exists to prevent, and it is the more common
# direction: a workaround with no expiry outlives its cause, silently, because
# the thing that would tell you it is safe again is the check nobody wrote.
# It also catches the reverse in advance — warning while a lapse is still days
# away, instead of the whole tilt surface failing at once on renewal day.
_RD_HOST = "radardisplay.engr.colostate.edu"
TLS_WARN_DAYS = 14


class Layer0RadarDisplayTlsCheck(Check):
    """Is radar-display's certificate valid, and how long until it is not?"""

    id         = "layer0.net.radardisplay_tls"
    target     = "radar-display"
    stage      = "L0"
    cadence_s  = 3600          # certificates change on the order of months
    # Deliberately no dependency on upstream health: this is a fact about
    # their TLS, not about whether radarca is answering.
    depends_on: list[str] = []

    async def run(self, ctx) -> CheckResult:
        import asyncio
        import ssl
        from datetime import datetime, timezone

        t0 = utcnow()

        def _probe():
            sock_ctx = ssl.create_default_context()
            with socket.create_connection((_RD_HOST, 443), timeout=10) as sock:
                with sock_ctx.wrap_socket(sock, server_hostname=_RD_HOST) as ssock:
                    return ssock.getpeercert()

        try:
            cert = await asyncio.wait_for(asyncio.to_thread(_probe), timeout=20)
        except Exception as e:
            # A verification failure is the whole point of the check, so it is
            # a fail rather than an error: the statement "their certificate is
            # not trustworthy" is a real finding about the monitored thing.
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="fail", started_at=t0, finished_at=utcnow(),
                summary=f"TLS verification failed: {humanize_error(e)}",
                payload={"host": _RD_HOST, "error": str(e),
                         "hint": "SENTINEL_RD_VERIFY_TLS=0 restores tilt "
                                 "imagery while the certificate is fixed"},
            )

        not_after = cert.get("notAfter") if isinstance(cert, dict) else None
        days = None
        if not_after:
            try:
                exp = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
                    tzinfo=timezone.utc)
                days = (exp - utcnow()).total_seconds() / 86400.0
            except ValueError:
                days = None

        payload = {"host": _RD_HOST, "not_after": not_after,
                   "days_remaining": round(days, 1) if days is not None else None}
        if days is None:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="warn", started_at=t0, finished_at=utcnow(),
                summary=f"certificate valid but its expiry is unreadable: {not_after!r}",
                payload=payload,
            )
        if days <= 0:
            status, msg = "fail", f"certificate EXPIRED {abs(days):.0f}d ago"
        elif days <= TLS_WARN_DAYS:
            status, msg = "warn", f"certificate expires in {days:.0f}d"
        else:
            status, msg = "pass", f"certificate valid, {days:.0f}d remaining"
        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status=status, started_at=t0, finished_at=utcnow(),
            summary=msg, payload=payload,
            metrics={"tls_days_remaining": float(days)},
        )


register(Layer0RadarDisplayTlsCheck())
