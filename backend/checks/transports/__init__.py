"""Injected clients available inside ``Check.run``.

Add new transports here. The scheduler builds a single ``CheckContext`` at
startup and hands it to every check. Each transport is a long-lived object
with its own connection pool / session; never construct one per-check.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

from .browser import BrowserClient
from .http import HttpClient
from ..network import NetworkMonitor


@dataclass
class CheckContext:
    http:    HttpClient
    browser: BrowserClient
    network: NetworkMonitor | None = None
    # The asyncpg pool is injected here so checks that want to persist
    # side-data (image archive, future per-check artifacts) don't have to
    # reach into FastAPI app state. Type-erased as Any to keep the import
    # graph shallow.
    pool:    Any | None = None
    # future: fs, ssh, s3, snmp, prom, shell, db ...

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.browser.aclose()
        if self.network is not None:
            await self.network.stop()
