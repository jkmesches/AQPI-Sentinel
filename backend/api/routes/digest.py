"""Daily digest — read the report, configure it, send a test copy.

The report is computed in one place (`backend/digest.py`) and rendered from
that one result, so the email, the JSON and any future web view can never
disagree about a number. Recomputing per surface is how two views of the same
morning end up quoting different availabilities.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ... import digest as _digest
from ...config import SETTINGS
from .auth import require_admin

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/report")
admin_router = APIRouter(prefix="/api/admin/digest")


def _window(since: str | None, until: str | None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    def _p(v, default):
        if not v:
            return default
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, f"bad timestamp: {v!r}")
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    u = _p(until, now)
    s = _p(since, u - timedelta(hours=24))
    if s >= u:
        raise HTTPException(400, "since must be before until")
    return s, u


async def load_config(pool) -> dict:
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1",
                              _digest.SETTINGS_KEY)
    cfg = dict(_digest.DEFAULTS)
    if row and row["value"]:
        val = row["value"]
        if isinstance(val, str):
            import json
            val = json.loads(val)
        if isinstance(val, dict):
            cfg.update(val)
    return cfg


@router.get("/daily")
async def daily(request: Request, since: str | None = None,
                until: str | None = None):
    """The digest as data. Readable without an email, and the thing tests
    assert against — a rendering bug should not be able to hide a data bug."""
    s, u = _window(since, until)
    cfg = await load_config(request.app.state.store.pool)
    return await _digest.compute(
        request.app.state.store.pool, since=s, until=u,
        tz=cfg.get("tz") or "UTC", products=bool(cfg.get("products", True)),
    )


@admin_router.get("")
async def get_config(request: Request,
                     user: Annotated[dict, Depends(require_admin)]):
    return await load_config(request.app.state.store.pool)


@admin_router.put("")
async def put_config(request: Request,
                     user: Annotated[dict, Depends(require_admin)],
                     body: dict = Body(...)):
    cfg = dict(_digest.DEFAULTS)
    cfg.update(await load_config(request.app.state.store.pool))
    for k in ("enabled", "recipients", "hour", "tz", "products"):
        if k in body:
            cfg[k] = body[k]

    # Validate before saving, not at 07:00 tomorrow. A bad timezone or hour
    # stored quietly would mean the report simply never arrives, and a report
    # that fails silently is worse than one that was never configured.
    try:
        ZoneInfo(str(cfg["tz"]))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise HTTPException(400, f"unknown timezone: {cfg['tz']!r}")
    try:
        hour = int(cfg["hour"])
    except (TypeError, ValueError):
        raise HTTPException(400, "hour must be a whole number")
    if not 0 <= hour <= 23:
        raise HTTPException(400, "hour must be between 0 and 23")
    cfg["hour"] = hour
    if not isinstance(cfg.get("recipients"), list):
        raise HTTPException(400, "recipients must be a list of addresses")
    cfg["recipients"] = [str(x).strip() for x in cfg["recipients"] if str(x).strip()]
    if cfg.get("enabled") and not cfg["recipients"]:
        raise HTTPException(400, "enable the digest only once it has a recipient")

    import json
    await request.app.state.store.pool.execute(
        """
        INSERT INTO settings (key, value, updated_at, updated_by)
        VALUES ($1, $2::jsonb, now(), $3)
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by
        """,
        _digest.SETTINGS_KEY, json.dumps(cfg), user.get("email", "admin"),
    )
    task = getattr(request.app.state, "digest", None)
    if task is not None:
        task.reload(cfg)
    return cfg


@admin_router.post("/test")
async def send_test(request: Request,
                    user: Annotated[dict, Depends(require_admin)],
                    body: dict = Body(default={})):
    """Send one copy of the current report to ONE address.

    Deliberately not "send to the recipient list": the point of a test is to
    see the thing yourself before Professor Chandra does. Defaults to the
    address of the admin who clicked, and never touches the configured
    recipients even when the digest is enabled.
    """
    pool = request.app.state.store.pool
    to = (body or {}).get("to") or user.get("email")
    if not to:
        raise HTTPException(400, "no recipient — pass 'to'")
    if not isinstance(to, str) or "@" not in to:
        raise HTTPException(400, f"not an email address: {to!r}")

    cfg = await load_config(pool)
    s, u = _window((body or {}).get("since"), (body or {}).get("until"))
    data = await _digest.compute(pool, since=s, until=u,
                                 tz=cfg.get("tz") or "UTC",
                                 products=bool(cfg.get("products", True)))
    subject = "[TEST] " + _digest.subject_line(data)
    text = _digest.render_text(data, SETTINGS.public_url)
    html = _digest.render_html(data, SETTINGS.public_url)

    from ...auth.email import send_transactional
    ok, err = await send_transactional(pool, to=to, subject=subject,
                                       body=text, html=html)
    if not ok:
        raise HTTPException(502, f"send failed: {err}")
    log.info("digest: test report sent to %s by %s", to, user.get("email"))
    return {"sent_to": to, "subject": subject,
            "radars": len(data["radars"]), "products": len(data["products"])}


@admin_router.post("/preview")
async def preview(request: Request,
                  user: Annotated[dict, Depends(require_admin)],
                  body: dict = Body(default={})):
    """Render without sending, so the admin page can show the real thing."""
    cfg = await load_config(request.app.state.store.pool)
    s, u = _window((body or {}).get("since"), (body or {}).get("until"))
    data = await _digest.compute(request.app.state.store.pool, since=s, until=u,
                                 tz=cfg.get("tz") or "UTC",
                                 products=bool(cfg.get("products", True)))
    return {
        "subject": _digest.subject_line(data),
        "text":    _digest.render_text(data, SETTINGS.public_url),
        "html":    _digest.render_html(data, SETTINGS.public_url),
    }
