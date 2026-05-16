"""Injected clients available inside ``Check.run``.

Add new transports here. The scheduler builds a single ``CheckContext`` at
startup and hands it to every check. Each transport is a long-lived object
with its own connection pool / session; never construct one per-check.
"""
from __future__ import annotations
from dataclasses import dataclass

from .browser import BrowserClient
from .http import HttpClient


@dataclass
class CheckContext:
    http: HttpClient
    browser: BrowserClient
    # future: fs, ssh, s3, snmp, prom, shell, db ...

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.browser.aclose()
