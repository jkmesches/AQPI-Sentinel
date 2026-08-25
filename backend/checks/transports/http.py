"""HTTP transport — thin httpx.AsyncClient wrapper.

One client per Sentinel process, shared by every check. Connection pool,
cookie jar, automatic redirect handling, and a bounded retry for
idempotent requests.
"""
from __future__ import annotations
import asyncio
import logging
import random

import httpx

log = logging.getLogger(__name__)

# === Load-bearing: this timeout tracks upstream's latency, not a round number ===
#
# radarca's latency changed regime on 2026-08-16: our own latency_ms p95 went
# from ~2.4s to 5.9-8.4s and stayed there. A 25-probe sample of
# /api/radar-status/ on 2026-08-25 measured p50 3.4s, p90 17.5s, max >25s.
#
# The old 12.0s default was tuned when p95 was 2.4s. Against the new
# distribution it sits *below* upstream's p90, so a large slice of perfectly
# successful-but-slow responses were being converted into error ticks —
# ~1,500-1,800/day, rendered on the timeline as radar outages.
#
# Raise this if upstream slows further; lower it if it recovers. Check with:
#   for i in $(seq 25); do curl -s -o /dev/null -w '%{time_total}\n' \
#     --max-time 30 https://radarca.engr.colostate.edu/api/radar-status/; done | sort -n
DEFAULT_TIMEOUT_S = 20.0

# One retry, not three. These are 2-minute-cadence health probes: the next
# scheduled run is itself a retry, so deep retry ladders mostly add load and
# blur the signal. A single fast retry is what converts "transient blip" into
# "no event", which measurement showed is 28% of all errors.
DEFAULT_RETRIES = 1
RETRY_BACKOFF_S = 0.5

# 5xx and 429 are retried because they are frequently transient upstream
# (qpe_1hr served 3,918 HTTP failures over 14 days yet probes clean now).
# 4xx other than 429 are NOT retried — they are deterministic answers.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class HttpClient:
    """Shared httpx client.

    Defaults give us a generous connection pool so that user-driven proxy
    fetches (map scrubs, timeline image previews) can't starve the
    scheduled checks. Connect timeout is shortened separately from the
    overall timeout so dead-network scenarios fail fast and free the
    connection back to the pool instead of stalling.
    """
    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5.0),
            limits=httpx.Limits(
                max_connections=200,
                max_keepalive_connections=50,
                keepalive_expiry=30.0,
            ),
            headers={"User-Agent": "Sentinel/0.1"},
            follow_redirects=True,
            http2=False,  # radarca is HTTP/1.1; avoid extra negotiation
        )
        # Cheap instrumentation for the /api/_debug/stats endpoint. If these
        # ever drift far apart from each other it means we're leaking
        # requests (e.g. an exception path didn't decrement).
        self.in_flight = 0
        self.total_requests = 0
        # Retry instrumentation: retries_attempted vs retries_succeeded tells
        # you whether the retry is earning its keep or just doubling load on a
        # genuinely-down upstream.
        self.retries_attempted = 0
        self.retries_succeeded = 0

    async def _request(self, method: str, url: str, *, retries: int | None = None,
                       **kw) -> httpx.Response:
        attempts = (DEFAULT_RETRIES if retries is None else retries) + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            self.in_flight += 1
            self.total_requests += 1
            try:
                r = await self._client.request(method, url, **kw)
                if r.status_code in RETRY_STATUS and attempt < attempts - 1:
                    self.retries_attempted += 1
                    await self._backoff(attempt)
                    continue
                # Count a retry as successful only if the retry actually
                # produced a good answer — a final attempt that still returns
                # 500 is a failed retry, not a saved one.
                if attempt > 0 and r.status_code not in RETRY_STATUS:
                    self.retries_succeeded += 1
                    log.debug("http retry succeeded: %s %s", method, url)
                return r
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                if attempt < attempts - 1:
                    self.retries_attempted += 1
                    await self._backoff(attempt)
                    continue
                raise
            finally:
                self.in_flight -= 1
        # Only reachable if the loop exhausted on a retryable status code.
        if last_exc is not None:
            raise last_exc
        return r

    @staticmethod
    async def _backoff(attempt: int) -> None:
        # Jitter so six radar checks retrying the same URL don't resynchronise
        # into a thundering herd against an already-slow upstream.
        await asyncio.sleep(RETRY_BACKOFF_S * (attempt + 1) * (0.5 + random.random()))

    async def get(self, url: str, **kw) -> httpx.Response:
        return await self._request("GET", url, **kw)

    async def head(self, url: str, **kw) -> httpx.Response:
        return await self._request("HEAD", url, **kw)

    async def aclose(self) -> None:
        await self._client.aclose()
