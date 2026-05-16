"""HTTP transport — thin httpx.AsyncClient wrapper.

One client per Sentinel process, shared by every check. Connection pool,
cookie jar, automatic redirect handling.
"""
from __future__ import annotations
import httpx


class HttpClient:
    def __init__(self, timeout: float = 15.0):
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "Sentinel/0.1"},
            follow_redirects=True,
        )

    async def get(self, url: str, **kw) -> httpx.Response:
        return await self._client.get(url, **kw)

    async def head(self, url: str, **kw) -> httpx.Response:
        return await self._client.head(url, **kw)

    async def aclose(self) -> None:
        await self._client.aclose()
