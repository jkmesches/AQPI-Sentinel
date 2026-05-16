"""Sink registry. Built at startup from AlertsConfig; one instance per channel.

If a channel can't be constructed (e.g. email sink with no SMTP), the entry
is omitted from the registry and the engine logs a one-time warning instead
of crashing the process.
"""
from __future__ import annotations
import logging
from typing import Any

from .base import Sink, SinkResult       # noqa: F401  (re-export)
from .console import ConsoleSink
from .email import EmailSink
from .webhook import WebhookSink

log = logging.getLogger(__name__)


def build_sinks(cfg) -> dict[str, Any]:
    """Return a {channel_name: Sink} mapping.

    Console is always present. Email is present iff smtp is configured.
    Webhook is always present (each receiver supplies its own URL)."""
    sinks: dict[str, Any] = {"console": ConsoleSink()}
    try:
        sinks["email"] = EmailSink(cfg.smtp)
    except RuntimeError as e:
        log.warning("email sink disabled: %s", e)
    sinks["webhook"] = WebhookSink()
    return sinks
