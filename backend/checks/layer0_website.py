"""Layer 0 — website status.

Validated by validation_tests/test_layer0_website.py. Ported to the new
``Check`` interface; uses the injected HTTP transport.
"""
from __future__ import annotations
import socket
import ssl
from datetime import datetime, timezone

from ..config import SETTINGS
from ..registry import register
from .base import Check, CheckResult, utcnow


PUBLIC_MARKERS = (
    "Radar Data", "Atmospheric Forecast", "CoSMoS Data",
    "National Water Model", "Reflectivity",
)
NOTFOUND_MARKERS = ("404: This page could not be found", "next-error-h1", "_next/static")
MIN_PUBLIC_BYTES = 50_000
TLS_DAYS_FLOOR = 30


@register
class WebsitePublicCheck(Check):
    """``GET /public`` must be 200 HTML, ≥50 KB, with the expected SSR markers."""
    id = "layer0.website.public"
    stage = "L0"
    target = "website"
    cadence_s = 60
    depends_on: list[str] = []

    async def run(self, ctx):
        t0 = utcnow()
        r = await ctx.http.get(f"{SETTINGS.base}/public")
        elapsed_ms = (utcnow() - t0).total_seconds() * 1000

        ok_status  = r.status_code == 200
        ok_type    = r.headers.get("content-type", "").startswith("text/html")
        ok_size    = len(r.content) >= MIN_PUBLIC_BYTES
        markers    = {m: m in r.text for m in PUBLIC_MARKERS}
        ok_markers = all(markers.values())
        passed     = all([ok_status, ok_type, ok_size, ok_markers])

        missing = [m for m, ok in markers.items() if not ok]
        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status="pass" if passed else "fail",
            started_at=t0, finished_at=utcnow(),
            summary=(
                f"HTTP {r.status_code} {len(r.content)}B"
                + (f" missing={missing}" if missing else "")
            ),
            payload={
                "http": r.status_code,
                "bytes": len(r.content),
                "content_type": r.headers.get("content-type", ""),
                "markers": markers,
                "nextjs_cache": r.headers.get("x-nextjs-cache", ""),
            },
            metrics={"bytes": float(len(r.content)), "latency_ms": elapsed_ms},
        )


@register
class RootNotFoundCheck(Check):
    """``GET /`` must contain the Next.js not-found markers. Note: served with
    HTTP 200, *not* 404 — body markers are the assertion."""
    id = "layer0.website.root_notfound"
    stage = "L0"
    target = "root"
    cadence_s = 300
    depends_on: list[str] = []

    async def run(self, ctx):
        t0 = utcnow()
        r = await ctx.http.get(f"{SETTINGS.base}/")
        markers = {m: m in r.text for m in NOTFOUND_MARKERS}
        passed = all(markers.values())
        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status="pass" if passed else "fail",
            started_at=t0, finished_at=utcnow(),
            summary=f"HTTP {r.status_code} {len(r.content)}B",
            payload={"http": r.status_code, "markers": markers},
            metrics={"bytes": float(len(r.content))},
        )


@register
class OriginAliveCheck(Check):
    """``/public`` is permanently cached and stays 200 even if origin dies.
    Probe an ``/api/*`` endpoint (no-store) to prove Django is alive."""
    id = "layer0.origin.alive"
    stage = "L0"
    target = "origin"
    cadence_s = 60
    depends_on: list[str] = []

    async def run(self, ctx):
        t0 = utcnow()
        r = await ctx.http.get(f"{SETTINGS.base}/api/radar-status/")
        elapsed_ms = (utcnow() - t0).total_seconds() * 1000
        passed = (
            r.status_code == 200
            and r.headers.get("content-type", "").startswith("application/json")
        )
        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status="pass" if passed else "fail",
            started_at=t0, finished_at=utcnow(),
            summary=f"HTTP {r.status_code} {len(r.content)}B",
            payload={
                "http": r.status_code,
                "bytes": len(r.content),
                "cache_control": r.headers.get("cache-control", ""),
            },
            metrics={"latency_ms": elapsed_ms},
        )


@register
class TLSCertCheck(Check):
    """TLS handshake + cert expiry. Synchronous socket so we offload it to a
    thread (cadence is 1 h — overhead is irrelevant)."""
    id = "layer0.tls.cert"
    stage = "L0"
    target = "tls"
    cadence_s = 3600
    depends_on: list[str] = []

    async def run(self, ctx):
        import asyncio
        t0 = utcnow()

        def _probe() -> dict:
            host = SETTINGS.base.split("://", 1)[1].rstrip("/")
            cctx = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=10) as s:
                with cctx.wrap_socket(s, server_hostname=host) as ss:
                    return ss.getpeercert()

        try:
            cert = await asyncio.to_thread(_probe)
        except Exception as e:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="fail",
                started_at=t0, finished_at=utcnow(),
                summary=f"TLS probe failed: {type(e).__name__}: {e}",
                payload={"error": str(e)},
            )
        not_after = datetime.strptime(
            cert["notAfter"], "%b %d %H:%M:%S %Y %Z"
        ).replace(tzinfo=timezone.utc)
        days = (not_after - utcnow()).days
        passed = days > TLS_DAYS_FLOOR
        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status="pass" if passed else "warn",
            started_at=t0, finished_at=utcnow(),
            summary=f"expires {cert['notAfter']} ({days} days)",
            payload={
                "not_after": cert["notAfter"],
                "subject": dict(x[0] for x in cert.get("subject", [])),
                "issuer":  dict(x[0] for x in cert.get("issuer",  [])),
            },
            metrics={"days_until_expiry": float(days)},
        )
