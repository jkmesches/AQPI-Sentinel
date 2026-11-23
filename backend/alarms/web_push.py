"""The listener that turns a newly opened alarm into a Web Push.

Only on alarm_open, at the severity the alarm opened with, so later
promotions do not page again. Failures are logged and dropped, since push is
a best-effort channel.

It fires from the engine's listener list, outside the escalation tick, so it
has to apply the two rules the tick applies on its own: an active silence
holds it back, and so does a record-only target (a NEXRAD radar, see
alarms/record_only.py). Without the first, a "silence everything" rule would
still let pushes through. Without the second, users would be warned about the
NEXRAD radars, which Joseph ruled out on 2026-11-23.

Built by a factory, so a test can hand it a fake store and a fake sender and
see what it would have sent.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from ..stages import stage_descriptor  # noqa: TID252  the package imports relatively throughout
from . import silences as silence_lib
from .record_only import record_only

log = logging.getLogger(__name__)

Listener = Callable[[str, dict[str, Any]], Awaitable[None]]
Sender = Callable[[Any, dict[str, Any]], Awaitable[dict[str, int]]]


def make_listener(store: Any, send: Sender) -> Listener:  # noqa: ANN401  the store is duck-typed
    """Build the alarm_open listener over one store and one push sender.

    Args:
        store: Has `list_active_silences(now)` and a `pool`.
        send: push.dispatch_push, or a fake in a test.

    Returns:
        The listener to pass to AlarmEngine.add_listener.
    """
    async def listener(event_type: str, payload: dict[str, Any]) -> None:
        if event_type != "alarm_open":
            return
        if record_only(payload):
            log.info("push withheld, record-only target: %s/%s",
                     payload.get("check_id"), payload.get("target"))
            return
        try:
            now = datetime.now(timezone.utc)
            active = await store.list_active_silences(now)
            if silence_lib.find_active_silence(active, payload, now):
                log.info("push suppressed by active silence: %s/%s",
                         payload.get("check_id"), payload.get("target"))
                return
            sev = (payload.get("severity") or "warn").lower()
            stage = payload.get("stage") or ""
            target = payload.get("target") or ""
            check = payload.get("check_id") or ""
            msg = (payload.get("message") or "").split("\n")[0][:140]
            # Push titles surface the descriptor name; canonical stage ID
            # rides in `data` for downstream click routing/debug.
            stage_label = stage_descriptor(stage) if stage else ""
            title = f"[{sev.upper()}] {stage_label}/{target or check.split('.')[-1]}"
            # `stage`, `check_id` and `severity` travel as fields so the
            # per-device filter reads them instead of parsing the title, and
            # `alarm_id` lets a delayed push check whether the alarm is still
            # open and unacked when its delay runs out.
            result = await send(store.pool, {
                "title": title, "body": msg or f"{check} fired",
                "tag": f"{check}|{target}", "url": "/m/alarms",
                "stage": stage, "check_id": check, "severity": sev,
                "alarm_id": payload.get("id"),
            })
            log.info(
                "web-push %s/%s: sent=%d failed=%d expired=%d filtered=%d deferred=%d",
                check, target, result["sent"], result["failed"], result["expired"],
                result["filtered"], result["deferred"])
        except Exception:
            log.exception("web-push dispatch failed for alarm %s", payload.get("id"))

    return listener
