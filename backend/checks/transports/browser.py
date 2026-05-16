"""Headless-browser transport (Playwright). Lazy-launched on first use because
Chromium startup is ~500 ms — paying that at process start for every Sentinel
run regardless of whether overlay checks are wanted is wasteful.
"""
from __future__ import annotations
import asyncio
import logging

log = logging.getLogger(__name__)


class BrowserClient:
    def __init__(self):
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        if self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            log.info("playwright chromium launched")

    async def new_page(self, viewport: dict | None = None):
        await self._ensure()
        ctx = await self._browser.new_context(viewport=viewport or {"width": 1600, "height": 1000})
        return await ctx.new_page()

    async def aclose(self):
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
