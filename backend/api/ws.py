"""WebSocket fan-out for live push.

One ConnectionManager singleton owns all connected clients. Both the
scheduler (every CheckResult) and the alarm engine (alarm transitions)
push events through it. The frontend opens a single ``/api/ws`` socket
and consumes a multiplexed event stream.

Events:
    {type: "run",          run: CheckRun}
    {type: "alarm_open",   alarm: dict}
    {type: "alarm_close",  alarm: dict}
    {type: "alarm_promote",{id, severity}}
    {type: "hello",        sentinel: {build, checks, stages}}
"""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from ..registry import CHECKS, all_stages
from .._version import __version__

log = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self._conns: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._conns.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._conns.discard(ws)

    async def broadcast(self, event: dict[str, Any]) -> None:
        """Fan out to all connected clients in parallel with per-client
        timeouts so one slow/zombie connection can't stall the broadcaster."""
        data = json.dumps(event, default=str)
        async with self._lock:
            conns = list(self._conns)
        if not conns:
            return

        async def _one(ws):
            try:
                await asyncio.wait_for(ws.send_text(data), timeout=3)
            except Exception:
                # drop broken/slow clients
                try:
                    await ws.close()
                except Exception:
                    pass
                async with self._lock:
                    self._conns.discard(ws)

        await asyncio.gather(*(_one(w) for w in conns), return_exceptions=True)


router = APIRouter()


@router.websocket("/api/ws")
async def websocket_endpoint(ws: WebSocket, token: str | None = None):
    """Accepts an optional `?token=<sid>` so authed clients can be
    identified (browser WebSocket constructors can't set custom
    headers). The dashboard is publicly readable so we don't reject
    unauthed clients here — `token` is a hook for future per-user
    event routing."""
    manager: ConnectionManager = ws.app.state.ws
    if token:
        try:
            from .. import auth as A
            await A.fetch_session(ws.app.state.store.pool, token)
        except Exception:
            pass
    await manager.connect(ws)
    try:
        await ws.send_text(
            json.dumps({
                "type": "hello",
                "sentinel": {
                    "version": __version__,
                    "checks": len(CHECKS),
                    "stages": sorted(all_stages()),
                    "at": datetime.now(timezone.utc).isoformat(),
                },
            })
        )
        # passive: we don't expect client messages in v1
        last_ping = asyncio.get_event_loop().time()
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=15)
                if msg == "pong":
                    continue
            except asyncio.TimeoutError:
                # send keepalive; if the socket is dead this raises and we exit
                try:
                    await asyncio.wait_for(
                        ws.send_text(json.dumps({"type": "ping"})), timeout=3
                    )
                except Exception:
                    return
                last_ping = asyncio.get_event_loop().time()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws error")
    finally:
        await manager.disconnect(ws)
