"""Upstream request attribution — monitoring traffic vs map traffic.

Run directly (no DB, no network):

    python validation_tests/test_http_caller.py

One HttpClient is shared by the scheduler and the /api/upstream map proxy, so
before this split a single pair of counters mixed two unrelated populations.
Observed 2026-08-31: the counter read 47 read timeouts while only 4 scheduled
checks had timed out — the other 43 were map frames fetched while someone was
driving the map.

That is not a cosmetic problem. read_timeouts is the signal for "upstream's
latency tail has moved, revisit DEFAULT_TIMEOUT_S", and a number that rises
whenever anyone scrubs the map cannot carry that meaning. Acting on the
polluted figure would mean retuning a timeout in response to UI usage.

The subtle failure here is contextvar leakage: attribution is set per task, and
if it escaped into sibling tasks then one map request would mislabel every
check that happened to overlap it. That case is asserted explicitly.
"""
import asyncio, sys, os
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent)); os.environ.setdefault('SENTINEL_DB_URL','postgresql://unused/unused')
import httpx
from backend.checks.transports.http import HttpClient, set_http_caller, reset_http_caller

async def main():
    fails = []
    def ck(l, c, d=''):
        print(f"  {'ok  ' if c else 'FAIL'}  {l}" + (f"  — {d}" if d else ""))
        if not c: fails.append(l)

    def boom(_r): raise httpx.ReadTimeout("")
    c = HttpClient(); c._client = httpx.AsyncClient(transport=httpx.MockTransport(boom))

    # default bucket is the scheduler
    try: await c.get("https://x/")
    except httpx.ReadTimeout: pass
    ck("an untagged request counts as a check",
       c.by_caller.get("check", {}).get("read_timeouts") == 1, str(c.by_caller))

    # a request made while serving an API request is proxy traffic
    tok = set_http_caller("proxy")
    try:
        try: await c.get("https://x/")
        except httpx.ReadTimeout: pass
    finally: reset_http_caller(tok)
    ck("a tagged request counts as proxy",
       c.by_caller.get("proxy", {}).get("read_timeouts") == 1, str(c.by_caller))
    ck("the check bucket is unchanged by proxy traffic",
       c.by_caller["check"]["read_timeouts"] == 1)
    ck("the total still counts both", c.read_timeouts == 2, str(c.read_timeouts))

    # the tag must not leak across concurrent tasks
    async def tagged():
        t = set_http_caller("proxy")
        try:
            await asyncio.sleep(0.01)
            try: await c.get("https://x/")
            except httpx.ReadTimeout: pass
        finally: reset_http_caller(t)
    async def untagged():
        await asyncio.sleep(0.005)
        try: await c.get("https://x/")
        except httpx.ReadTimeout: pass
    before = dict(check=c.by_caller["check"]["read_timeouts"], proxy=c.by_caller["proxy"]["read_timeouts"])
    await asyncio.gather(tagged(), untagged(), tagged())
    ck("concurrent tasks do not leak their tag into each other",
       c.by_caller["proxy"]["read_timeouts"] == before["proxy"] + 2 and
       c.by_caller["check"]["read_timeouts"] == before["check"] + 1, str(c.by_caller))
    print("\nall caller-attribution assertions passed" if not fails else f"\n{len(fails)} FAILED")
    return 1 if fails else 0
sys.exit(asyncio.run(main()))
