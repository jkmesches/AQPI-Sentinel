"""Transactional email helpers + the canonical SMTP-settings loader.

There used to be two parallel SMTP configs (settings.smtp for the test-send
endpoint; AlertsConfig.smtp for the alarm-engine email sink). This module is
the single source of truth: load_smtp_settings() reads settings.smtp, and
both the alarm engine and the transactional helpers below adapt from it.
"""
from __future__ import annotations
import json
import logging
from email.message import EmailMessage

import aiosmtplib

log = logging.getLogger(__name__)


DEFAULT_FROM_NAME = "AQPI Sentinel"


async def load_smtp_settings(pool) -> dict | None:
    """Return the saved settings.smtp dict, or None if not configured.

    Keys: host, port, username, password, from_addr, from_name (optional,
    defaults to ``AQPI Sentinel`` when composing headers), use_tls,
    use_starttls. Missing host or from_addr → returns None.
    """
    try:
        row = await pool.fetchrow("SELECT value FROM settings WHERE key = 'smtp'")
    except Exception:
        return None
    if row is None:
        return None
    val = row["value"]
    if isinstance(val, str):
        val = json.loads(val)
    if not isinstance(val, dict):
        return None
    if not val.get("host") or not val.get("from_addr"):
        return None
    return val


def from_header(cfg: dict) -> str:
    """Compose an RFC-5322 ``From:`` header from the settings dict."""
    name = (cfg.get("from_name") or DEFAULT_FROM_NAME).strip()
    addr = (cfg.get("from_addr") or "").strip()
    if not addr:
        return ""
    return f'"{name}" <{addr}>' if name else addr


def to_alerts_smtp_dict(cfg: dict) -> dict:
    """Adapt settings.smtp → kwargs for AlertsConfig.SmtpConfig.

    The alarm engine's email sink consumes the old SmtpConfig shape; this
    bridge keeps that sink untouched while still letting /admin/email be
    the only place a user configures SMTP.
    """
    use_tls = bool(cfg.get("use_tls", False))
    use_starttls = bool(cfg.get("use_starttls", not use_tls))
    return {
        "host":     cfg["host"],
        "port":     int(cfg.get("port") or 587),
        "starttls": use_starttls,
        "username": cfg.get("username") or None,
        "password": cfg.get("password") or None,
        "from":     from_header(cfg),
    }


async def send_transactional(
    pool, *, to: str, subject: str, body: str,
) -> tuple[bool, str | None]:
    """Send a plain-text email via the saved SMTP config.

    Returns ``(delivered, error_message)``. ``(False, "SMTP not configured")``
    when there's no usable settings.smtp row — callers should treat that as
    "fall back to manual link copy" rather than an error.
    """
    cfg = await load_smtp_settings(pool)
    if cfg is None:
        return False, "SMTP not configured"
    msg = EmailMessage()
    msg["From"] = from_header(cfg)
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    use_tls = bool(cfg.get("use_tls", False))
    use_starttls = bool(cfg.get("use_starttls", not use_tls))
    try:
        await aiosmtplib.send(
            msg,
            hostname=cfg["host"], port=int(cfg.get("port") or 587),
            username=cfg.get("username") or None,
            password=cfg.get("password") or None,
            use_tls=use_tls and not use_starttls,
            start_tls=use_starttls,
            timeout=15,
        )
        return True, None
    except Exception as e:
        log.exception("send_transactional failed")
        return False, f"{type(e).__name__}: {e}"
