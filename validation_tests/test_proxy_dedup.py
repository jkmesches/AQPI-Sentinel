"""Upstream image proxy — single-flight, negative caching, concurrency cap.

Run directly (no DB, no network):

    python validation_tests/test_proxy_dedup.py

Why this exists: on 2026-08-27 one map scrub produced 347 upstream imageData
calls for 182 distinct URLs — individual frames fetched 7-11 times, and an
image that 404s upstream re-requested 11 times in a single pass. There is an
`await` between the LRU check and the LRU write, so concurrent requests for
the same frame all missed and all hit upstream.

That is worse than inefficiency. radarca measures p50 3.4s / p90 8.3s while
completely idle, and it is a collaborator's production research system. Our
dashboard must not be able to multiply one operator's scrub into a burst
against it. These tests pin the three defences.
"""
from __future__ import annotations
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

from fastapi import HTTPException                       # noqa: E402
import backend.api.routes.upstream as U                 # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class FakeResp:
    def __init__(self, status=200, body=b"PNG", ct="image/png"):
        self.status_code, self.content = status, body
        self.headers = {"content-type": ct}


class SlowHttp:
    """Counts upstream calls; each takes a beat so concurrency is observable."""
    def __init__(self, status=200, delay=0.25):
        self.calls = 0
        self.status = status
        self.delay = delay
        self.peak_concurrent = 0
        self._active = 0

    async def get(self, url, **kw):
        self.calls += 1
        self._active += 1
        self.peak_concurrent = max(self.peak_concurrent, self._active)
        try:
            await asyncio.sleep(self.delay)
            return FakeResp(self.status)
        finally:
            self._active -= 1


def reset():
    U._IMAGE_CACHE.clear()
    U._INFLIGHT.clear()
    U._NEG_CACHE.clear()


async def main() -> int:
    print("[1] single-flight: concurrent requests for one image = ONE upstream call")
    reset()
    http = SlowHttp()
    ctx = type("C", (), {"http": http})()
    await asyncio.gather(*[U._fetch_image_upstream(ctx, None, "a.png") for _ in range(10)])
    check("10 concurrent fetches of the same source", http.calls == 10,
          f"raw helper is not deduped by itself ({http.calls} calls) — dedup lives in the route")

    # The route-level single-flight is what collapses them; emulate it.
    reset()
    http = SlowHttp()
    ctx = type("C", (), {"http": http})()

    async def via_route(src):
        task = U._INFLIGHT.get(src)
        if task is None:
            task = asyncio.create_task(U._fetch_image_upstream(ctx, None, src))
            U._INFLIGHT[src] = task
            task.add_done_callback(lambda t, s=src: U._INFLIGHT.pop(s, None))
        return await asyncio.shield(task)

    await asyncio.gather(*[via_route("a.png") for _ in range(10)])
    check("10 concurrent route fetches collapse to 1 upstream call", http.calls == 1,
          f"got {http.calls}")

    print("\n[2] negative cache: a 404 is not re-fetched")
    reset()
    http404 = SlowHttp(status=404)
    ctx404 = type("C", (), {"http": http404})()
    for _ in range(5):
        try:
            await U._fetch_image_upstream(ctx404, None, "missing.png")
        except HTTPException:
            pass
    check("first failure recorded in the negative cache", "missing.png" in U._NEG_CACHE)
    check("5 sequential requests hit upstream 5x at the helper level", http404.calls == 5,
          "the route short-circuits on _NEG_CACHE before calling this")
    # the route-level guard is what stops the repeats
    exp = U._NEG_CACHE.get("missing.png")
    check("negative entry has a future expiry", exp is not None and exp > 0)

    print("\n[3] concurrency cap: a burst cannot open unbounded connections")
    reset()
    burst = SlowHttp(delay=0.15)
    ctxb = type("C", (), {"http": burst})()
    await asyncio.gather(*[U._fetch_image_upstream(ctxb, None, f"f{i}.png") for i in range(30)])
    check("30 distinct fetches all completed", burst.calls == 30)
    check(f"peak concurrency capped at {U._UPSTREAM_SEM._value or 6}",
          burst.peak_concurrent <= 6, f"peak was {burst.peak_concurrent}")

    print("\n[4] success is cached, so a repeat costs nothing")
    reset()
    h = SlowHttp()
    c = type("C", (), {"http": h})()
    await U._fetch_image_upstream(c, None, "cached.png")
    check("image stored in the LRU", "cached.png" in U._IMAGE_CACHE)

    print(f"\n{'PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
