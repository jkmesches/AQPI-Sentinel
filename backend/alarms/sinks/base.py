"""Sink protocol — every channel implements one ``send()``.

Sinks are constructed once at startup with their AlertsConfig section. Each
``send()`` returns a SinkResult or raises; the AlarmTicker wraps the call so
failures don't blow up the loop.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class SinkResult:
    delivered: bool
    body_excerpt: str
    error: str | None = None


class Sink(Protocol):
    name: str               # "email" | "webhook" | "console" | future

    async def send(
        self,
        alarm: dict,
        receiver: Any,      # Receiver
        route: Any,         # Route
        step_idx: int,
    ) -> SinkResult: ...
