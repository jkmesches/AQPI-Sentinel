"""HTTP transport — thin httpx.AsyncClient wrapper.

One client per Sentinel process, shared by every check. Connection pool,
cookie jar, automatic redirect handling.
"""
from __future__ import annotations
import httpx


class HttpClient:
    """Shared httpx client.

    Defaults give us a generous connection pool so that user-driven proxy
    fetches (map scrubs, timeline image previews) can't starve the
    scheduled checks. Connect timeout is shortened separately from the
    overall timeout so dead-network scenarios fail fast and free the
    connection back to the pool instead of stalling at 15s.
    """
    def __init__(self, timeout: float = 12.0):
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

    async def get(self, url: str, **kw) -> httpx.Response:
        self.in_flight += 1
        self.total_requests += 1
        try:
            return await self._client.get(url, **kw)
        finally:
            self.in_flight -= 1

    async def head(self, url: str, **kw) -> httpx.Response:
        self.in_flight += 1
        self.total_requests += 1
        try:
            return await self._client.head(url, **kw)
        finally:
            self.in_flight -= 1

    async def aclose(self) -> None:
        await self._client.aclose()
