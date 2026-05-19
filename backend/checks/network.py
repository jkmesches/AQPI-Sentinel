"""Local-internet control monitor.

A long-lived background task that pings two external "always-up" endpoints
(Google's `generate_204` and Cloudflare's `cdn-cgi/trace`) on a short
interval. The result is consulted by the scheduler so that when the host
itself loses internet, the radarca checks aren't blamed (and don't alarm) —
they're skipped with a "local network offline" reason and render as grey
"no data" cells in the timeline.

Two probes (Google + Cloudflare) are used so a single-vendor outage doesn't
falsely flip us offline; we need ALL probes to fail to call it offline. A
small grace window (2 consecutive all-fail rounds) protects against
transient blips that resolve before the next round of checks runs.
"""
from __future__ import annotations
import asyncio
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..errors import humanize_error

log = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    name:   str
    ok:     bool
    status: int | None
    ms:     int
    error:  str | None = None


@dataclass
class NetworkSnapshot:
    online:          bool
    last_probe_at:   float                              # epoch seconds
    consecutive_fail: int
    results:         list[ProbeResult] = field(default_factory=list)


class NetworkMonitor:
    # Internet probes: go through IP-routed endpoints so the result reflects
    # raw outbound TCP/IP reachability. (cloudflare probe is a literal IP
    # so we can't lose the result to DNS issues — that's tracked separately
    # in DNS_PROBES below.)
    PROBES: list[tuple[str, str]] = [
        ("google",     "https://www.google.com/generate_204"),
        ("cloudflare", "https://1.1.1.1/cdn-cgi/trace"),
    ]
    # DNS probes: (short_label, hostname). The short label is what surfaces
    # in the check summary so the row stays on one line on a 360-px phone
    # viewport — `www.google.com www.cloudflare.com one.one.one.one`
    # wrapped to two lines on the live view.
    DNS_PROBES: list[tuple[str, str]] = [
        ("google",     "www.google.com"),
        ("cloudflare", "www.cloudflare.com"),
        ("quad-one",   "one.one.one.one"),
    ]
    INTERVAL_S     = 15.0
    PROBE_TIMEOUT  = 4.0
    GRACE_ROUNDS   = 2

    def __init__(self):
        # Separate httpx client — must not share the main one whose timeout is
        # 15s and whose User-Agent we want to keep for radarca traffic.
        self._client = httpx.AsyncClient(
            timeout=self.PROBE_TIMEOUT,
            headers={"User-Agent": "Sentinel-net-monitor/0.1"},
            follow_redirects=False,
        )
        self._task: asyncio.Task | None = None
        self._stop  = asyncio.Event()

        self.online           = True       # optimistic default — proven by first probe
        self.last_probe_at: float = 0.0
        self._consecutive_fail   = 0
        self._results: list[ProbeResult] = []

        # DNS state — tracked in parallel with internet state. Same grace
        # window so a single transient lookup failure doesn't flip us
        # "DNS down" prematurely.
        self.dns_ok: bool = True
        self._dns_consecutive_fail: int = 0
        self._dns_results: list[dict[str, Any]] = []

    async def start(self) -> None:
        # Run one probe synchronously so the initial state is real, then spawn
        # the background loop.
        await self._probe_once()
        self._task = asyncio.create_task(self._loop(), name="net-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self._client.aclose()

    def snapshot(self) -> dict[str, Any]:
        return {
            "online":            self.online,
            "last_probe_at":     self.last_probe_at,
            "consecutive_fail":  self._consecutive_fail,
            "results": [
                {"name": r.name, "ok": r.ok, "status": r.status, "ms": r.ms, "error": r.error}
                for r in self._results
            ],
            # Surfaced separately so the two control checks (Sentinel
            # Internet, Sentinel DNS) each read their own slice of state.
            "dns_ok":                 self.dns_ok,
            "dns_consecutive_fail":   self._dns_consecutive_fail,
            "dns_results":            list(self._dns_results),
        }

    # -------------------------------------------------------------------
    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.INTERVAL_S)
                return  # stop fired
            except asyncio.TimeoutError:
                pass
            try:
                await self._probe_once()
            except Exception:
                log.exception("net-monitor probe iteration failed")

    async def _probe_once(self) -> None:
        results = await asyncio.gather(
            *(self._one(name, url) for name, url in self.PROBES),
            return_exceptions=False,
        )
        self.last_probe_at = time.time()
        self._results = results
        if any(r.ok for r in results):
            self._consecutive_fail = 0
            self.online = True
        else:
            self._consecutive_fail += 1
            if self._consecutive_fail >= self.GRACE_ROUNDS:
                self.online = False
        # DNS probe runs alongside the HTTP probes — same cadence + grace
        # so the operator sees both signals advance together.
        await self._probe_dns_once()

    async def _probe_dns_once(self) -> None:
        loop = asyncio.get_event_loop()
        out: list[dict[str, Any]] = []
        for label, host in self.DNS_PROBES:
            t0 = time.monotonic()
            try:
                # AF_INET keeps it deterministic — IPv6-only environments
                # would need AF_UNSPEC, but the radarca host targets are
                # IPv4 and we'd rather measure the same code path used
                # by the L1/L2 checks.
                await asyncio.wait_for(
                    loop.getaddrinfo(host, None, family=socket.AF_INET,
                                     type=socket.SOCK_STREAM),
                    timeout=self.PROBE_TIMEOUT,
                )
                out.append({
                    "name":  label, "host": host, "ok": True,
                    "ms":    int((time.monotonic() - t0) * 1000),
                    "error": None,
                })
            except Exception as e:
                out.append({
                    "name":  label, "host": host, "ok": False,
                    "ms":    int((time.monotonic() - t0) * 1000),
                    "error": humanize_error(e),
                })
        self._dns_results = out
        if any(r["ok"] for r in out):
            self._dns_consecutive_fail = 0
            self.dns_ok = True
        else:
            self._dns_consecutive_fail += 1
            if self._dns_consecutive_fail >= self.GRACE_ROUNDS:
                self.dns_ok = False

    async def _one(self, name: str, url: str) -> ProbeResult:
        t0 = time.monotonic()
        try:
            r = await self._client.get(url)
            ok = r.status_code < 500
            return ProbeResult(
                name=name, ok=ok, status=r.status_code,
                ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            return ProbeResult(
                name=name, ok=False, status=None,
                ms=int((time.monotonic() - t0) * 1000),
                error=humanize_error(e),
            )
