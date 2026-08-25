"""Upstream API error handling — retry, fetch sharing, and fleet correlation.

Pure unit tests: no network, no database. Run directly:

    python validation_tests/test_api_error_handling.py

These cover the 2026-08-25 investigation into why 14% of check runs were
fail/error and why the downtime picture was overstated. Three mechanisms were
found and fixed; each has tests here, and each test exists because getting it
wrong is silent rather than loud.

1. HttpClient retry. Upstream latency changed regime on 2026-08-16 (p95
   2.4s -> 5.9-8.4s; a live sample of /api/radar-status/ measured p90 17.5s).
   A 12s timeout with no retry converted slow-but-fine responses into ~1,500
   error ticks/day. The subtle case is #2: a retry that still fails must not
   be counted as a save, or the instrumentation lies about its own value.

2. Shared radar-status fetch. Six radar checks each fetched the same URL every
   cycle, so one slow response produced six independent "unreachable" errors
   that rendered as six simultaneous radar outages.

3. Fleet correlation. 80.2% of GHOST_UP runs occurred while 4-5 radars ghosted
   simultaneously (one episode: four sites entering and leaving GHOST_UP within
   the same second, 79 hours apart). That is one upstream event, not five radar
   outages. The negative cases matter most: an isolated radar failure must
   still page, and CBAND must never be suppressed by an X-band event.
"""
from __future__ import annotations
import asyncio
import os
import sys
import types
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import httpx                                             # noqa: E402
import backend.checks as _all_checks                     # noqa: E402,F401  (import order)
import backend.checks.layer2_radar as L2                 # noqa: E402
from backend.checks.base import utcnow                   # noqa: E402
from backend.checks.transports.http import HttpClient    # noqa: E402
from backend.alarms.suppression import (                 # noqa: E402
    build_depends_on_index, compute_suppression,
)

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def _mock(client: HttpClient, handler) -> HttpClient:
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


class _Resp:
    def __init__(self, code, payload=None):
        self.status_code, self._p = code, payload

    def json(self):
        return self._p


class _Http:
    """Counts calls per endpoint so fetch-sharing is observable."""
    def __init__(self, rows, images=None, raise_exc=None):
        self.rows, self.images, self.raise_exc = rows, images or [], raise_exc
        self.status_calls = 0

    async def get(self, url, **kw):
        if self.raise_exc:
            raise self.raise_exc
        if "radar-status" in url:
            self.status_calls += 1
            return _Resp(200, self.rows)
        return _Resp(200, {"images": self.images})


ROWS = [{"radar": "XSCV", "status": "UP"}, {"radar": "XSCW", "status": "UP"},
        {"radar": "XSCR", "status": "UP"}, {"radar": "XSWR", "status": "UP"},
        {"radar": "EBAY", "status": "DOWN"}, {"radar": "CBand", "status": "UP"}]


async def test_retry() -> None:
    print("\n[1] HttpClient retry")
    n = {"c": 0}

    def transient(_):
        n["c"] += 1
        return httpx.Response(500 if n["c"] == 1 else 200)
    c = _mock(HttpClient(), transient)
    r = await c.get("http://x/")
    check("transient 500 is retried and succeeds", r.status_code == 200 and n["c"] == 2)
    check("successful retry is counted", c.retries_succeeded == 1)

    m = {"c": 0}

    def persistent(_):
        m["c"] += 1
        return httpx.Response(500)
    c2 = _mock(HttpClient(), persistent)
    r2 = await c2.get("http://x/")
    check("persistent 500 returns the 500 after retrying", r2.status_code == 500 and m["c"] == 2)
    check("a retry that still fails is NOT counted as a save",
          c2.retries_succeeded == 0, "otherwise the metric overstates its own value")

    k = {"c": 0}

    def notfound(_):
        k["c"] += 1
        return httpx.Response(404)
    c3 = _mock(HttpClient(), notfound)
    await c3.get("http://x/")
    check("404 is not retried (deterministic answer)", k["c"] == 1)

    t = {"c": 0}

    def timeout(_):
        t["c"] += 1
        raise httpx.ConnectTimeout("boom")
    c4 = _mock(HttpClient(), timeout)
    try:
        await c4.get("http://x/")
        check("persistent timeout raises", False)
    except httpx.TimeoutException:
        check("persistent timeout raises after retrying", t["c"] == 2)
    check("in_flight is balanced after a raising request", c4.in_flight == 0,
          "a leak here silently corrupts /api/_debug/stats")

    z = {"c": 0}

    def flaky(_):
        z["c"] += 1
        return httpx.Response(503)
    c5 = _mock(HttpClient(), flaky)
    await c5.get("http://x/", retries=0)
    check("retries=0 disables retry", z["c"] == 1)


async def test_shared_status_fetch() -> None:
    print("\n[2] shared /api/radar-status/ fetch")
    L2._reset_status_cache()
    ctx = types.SimpleNamespace(http=_Http(ROWS))
    await asyncio.gather(*[L2._radar_status_map(ctx) for _ in range(6)])
    check("six concurrent lookups make ONE upstream call", ctx.http.status_calls == 1,
          f"got {ctx.http.status_calls}")

    mapping, err = await L2._radar_status_map(ctx)
    check("aliases resolve (EBAY->XEBY, CBand->CBAND)",
          err is None and mapping["XEBY"] == "DOWN" and mapping["CBAND"] == "UP")

    L2._status_cache["at"] = L2.time.monotonic() - (L2._STATUS_TTL_S + 1)
    await L2._radar_status_map(ctx)
    check("memo expires after its TTL", ctx.http.status_calls == 2)

    chk = L2.Layer2RadarReconcile(radar_id="XSCV")
    L2._reset_status_cache()
    partial = [r for r in ROWS if r["radar"] != "XSCV"]
    st, e = await chk._declared(types.SimpleNamespace(http=_Http(partial)))
    check("radar absent from payload gets its own message",
          st is None and "absent from radar-status payload" in (e or ""), repr(e))

    L2._reset_status_cache()
    st, e = await chk._declared(types.SimpleNamespace(http=_Http(ROWS, raise_exc=RuntimeError("x"))))
    check("transport failure is reported as unreachable, with the cause",
          st is None and "unreachable" in (e or "") and "RuntimeError" in (e or ""), repr(e))


async def test_fleet_correlation() -> None:
    print("\n[3] fleet correlation")
    fleet = L2.Layer2XbandFleet()
    ctx = types.SimpleNamespace(http=None)
    now = utcnow()

    L2._reset_fleet_state()
    r = await fleet.run(ctx)
    check("no verdicts yet -> skip, not a false 'healthy'", r.status == "skip")

    L2._reset_fleet_state()
    for rid in L2.XBAND_FLEET:
        L2._publish_verdict(rid, "HEALTHY", now)
    check("all healthy -> pass", (await fleet.run(ctx)).status == "pass")

    L2._reset_fleet_state()
    for rid in ["XSCR", "XSCV", "XSCW", "XSWR"]:
        L2._publish_verdict(rid, "GHOST_UP", now)
    L2._publish_verdict("XEBY", "HEALTHY", now)
    r = await fleet.run(ctx)
    check("4 of 5 ghosting -> systemic fail",
          r.status == "fail" and r.payload["systemic"] and r.payload["n"] == 4)

    L2._reset_fleet_state()
    for rid in L2.XBAND_FLEET:
        L2._publish_verdict(rid, "HEALTHY", now)
    L2._publish_verdict("XSCR", "GHOST_UP", now)
    check("ONE radar down is NOT systemic — it must still page",
          (await fleet.run(ctx)).status == "pass")

    L2._reset_fleet_state()
    stale = now - timedelta(seconds=L2._VERDICT_TTL_S + 60)
    for rid in ["XSCR", "XSCV", "XSCW", "XSWR"]:
        L2._publish_verdict(rid, "GHOST_UP", stale)
    check("stale verdicts expire", L2._unhealthy_xband(now) == set())

    checks = [L2.Layer2RadarReconcile(radar_id=r) for r in L2.RADAR_FOLDER] + [fleet]
    idx = build_depends_on_index(checks)
    failing = {L2.FLEET_CHECK_ID: "fail", "layer0.origin.alive": "pass"}
    check("X-band alarms suppress under a fleet event",
          compute_suppression("layer2.radar.XSCR", failing, idx) == L2.FLEET_CHECK_ID)
    check("CBAND is NEVER suppressed by an X-band event",
          compute_suppression("layer2.radar.CBAND", failing, idx) is None,
          "different band, different site — it stayed healthy through the real episodes")
    healthy = {L2.FLEET_CHECK_ID: "pass", "layer0.origin.alive": "pass"}
    check("no suppression while the fleet is healthy",
          compute_suppression("layer2.radar.XSCR", healthy, idx) is None)


async def main() -> int:
    await test_retry()
    await test_shared_status_fetch()
    await test_fleet_correlation()
    print(f"\n{'PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
