"""Webhook sink — POST JSON. Suitable for Discord, Slack, ntfy, custom HTTP."""
from __future__ import annotations
import json
import httpx

from .base import SinkResult


class WebhookSink:
    name = "webhook"

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=10.0)

    async def aclose(self):
        await self._client.aclose()

    async def send(self, alarm, receiver, route, step_idx) -> SinkResult:
        body = {
            "severity":   alarm["severity"],
            "stage":      alarm["stage"],
            "check_id":   alarm["check_id"],
            "target":     alarm["target"],
            "message":    alarm["message"],
            "opened_at":  str(alarm["opened_at"]),
            "step":       step_idx,
            "policy":     getattr(route, "policy", ""),
        }
        try:
            r = await self._client.post(receiver.webhook, json=body)
            if 200 <= r.status_code < 300:
                return SinkResult(delivered=True, body_excerpt=json.dumps(body)[:500])
            return SinkResult(
                delivered=False, body_excerpt=json.dumps(body)[:500],
                error=f"HTTP {r.status_code}: {r.text[:200]}",
            )
        except Exception as e:
            return SinkResult(delivered=False, body_excerpt=json.dumps(body)[:500], error=str(e))
