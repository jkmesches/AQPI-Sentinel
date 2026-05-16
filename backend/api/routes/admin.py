"""Admin endpoints: settings (SMTP, alert routing), test-email, silences
(create/delete already in silences.py; this file owns the GET-with-extras
view for the admin UI), user management, audit log viewer.

All endpoints require an admin session.
"""
from __future__ import annotations
import json
import logging
import socket
import ssl
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ... import auth as A
from .auth import require_admin

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin")


# ---------------------------------------------------------------------------
# Settings (key → JSON)
# ---------------------------------------------------------------------------

SMTP_KEY = "smtp"

# Sensitive fields we never echo back to the client; the UI shows the
# current value as "•••••" if non-empty.
_SECRET_FIELDS = ("password",)


def _scrub(value: dict | None) -> dict | None:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    for k in _SECRET_FIELDS:
        if out.get(k):
            out[k] = "•••••"
    return out


@router.get("/settings/{key}")
async def get_setting(
    key: str, request: Request,
    user: Annotated[dict, Depends(require_admin)],
):
    """Return the JSON value stored at `key`, with secret fields masked."""
    row = await request.app.state.store.pool.fetchrow(
        "SELECT value, updated_at, updated_by FROM settings WHERE key = $1", key,
    )
    if row is None:
        return {"key": key, "value": None, "updated_at": None, "updated_by": None}
    val = row["value"]
    if isinstance(val, str):
        val = json.loads(val)
    if key == SMTP_KEY:
        val = _scrub(val)
    return {
        "key":        key,
        "value":      val,
        "updated_at": row["updated_at"].isoformat(),
        "updated_by": row["updated_by"],
    }


@router.put("/settings/{key}")
async def put_setting(
    key: str, request: Request,
    user: Annotated[dict, Depends(require_admin)],
    body: dict = Body(...),
):
    """Upsert a setting. For sensitive keys, fields set to the mask
    sentinel `•••••` are preserved from the existing row."""
    value = body.get("value")
    if value is None:
        raise HTTPException(400, "missing 'value'")
    pool = request.app.state.store.pool

    # Preserve masked secret fields by merging with the existing row.
    if isinstance(value, dict):
        existing = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", key)
        if existing:
            ev = existing["value"]
            if isinstance(ev, str):
                ev = json.loads(ev)
            if isinstance(ev, dict):
                merged = dict(ev)
                for k, v in value.items():
                    if k in _SECRET_FIELDS and v == "•••••":
                        # client sent the mask sentinel → keep old value
                        continue
                    merged[k] = v
                value = merged

    await pool.execute(
        """
        INSERT INTO settings (key, value, updated_at, updated_by)
        VALUES ($1, $2::jsonb, now(), $3)
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value,
              updated_at = EXCLUDED.updated_at,
              updated_by = EXCLUDED.updated_by
        """,
        key, json.dumps(value), user["email"],
    )
    await A.audit(pool, user_email=user["email"], action=f"settings.put",
                  target=f"settings:{key}",
                  payload={"keys": list(value.keys()) if isinstance(value, dict) else None})
    return {"ok": True, "key": key}


# ---------------------------------------------------------------------------
# SMTP test-send
# ---------------------------------------------------------------------------

@router.post("/email/test")
async def email_test(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    body: dict = Body(default={}),
):
    """Send a test email using the current SMTP settings. `to` defaults to
    the calling admin's email. Returns timing + delivery status."""
    pool = request.app.state.store.pool
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SMTP_KEY)
    if row is None:
        raise HTTPException(400, "SMTP not configured")
    cfg = row["value"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    host = cfg.get("host"); port = int(cfg.get("port") or 587)
    use_tls = bool(cfg.get("use_tls", True))
    use_starttls = bool(cfg.get("use_starttls", not use_tls))
    username = cfg.get("username") or ""
    password = cfg.get("password") or ""
    from_addr = cfg.get("from_addr") or username
    if not host or not from_addr:
        raise HTTPException(400, "SMTP host or from_addr missing")

    to_addr = (body or {}).get("to") or user["email"]
    subject = (body or {}).get("subject") or "Sentinel test email"

    # Use aiosmtplib (already a project dep).
    import aiosmtplib
    from email.message import EmailMessage
    import time

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(
        f"This is a test email from Sentinel.\n\n"
        f"Triggered by: {user['email']}\n"
        f"At: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"\n"
        f"If you received this, your SMTP config is working.\n"
    )

    t0 = time.monotonic()
    try:
        await aiosmtplib.send(
            msg, hostname=host, port=port,
            username=username or None,
            password=password or None,
            use_tls=use_tls and not use_starttls,
            start_tls=use_starttls,
            timeout=15,
        )
        ms = int((time.monotonic() - t0) * 1000)
        await A.audit(pool, user_email=user["email"], action="email.test",
                      payload={"to": to_addr, "ms": ms, "ok": True})
        return {"ok": True, "to": to_addr, "ms": ms}
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        msg_err = f"{type(e).__name__}: {e}"
        await A.audit(pool, user_email=user["email"], action="email.test",
                      payload={"to": to_addr, "ms": ms, "ok": False, "error": msg_err})
        raise HTTPException(502, msg_err)
