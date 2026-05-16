"""Console sink — logs alarms with a one-liner. Always available."""
from __future__ import annotations
import logging

from .base import SinkResult

log = logging.getLogger("sentinel.alarm")


class ConsoleSink:
    name = "console"

    async def send(self, alarm, receiver, route, step_idx) -> SinkResult:
        line = (
            f"[ALERT step={step_idx} sev={alarm['severity']}] "
            f"{alarm['stage']}/{alarm['check_id']} target={alarm['target']}  "
            f"{alarm['message']}"
        )
        log.warning(line)
        return SinkResult(delivered=True, body_excerpt=line[:500])
