"""Admin endpoints: settings (SMTP, alert routing), test-email,
audit log viewer.

All endpoints require an admin session.
"""
from __future__ import annotations
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ... import auth as A
from ...alarms.models import AlertsConfig
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
        # Surface the default from_name to the UI when none is saved yet so
        # the field shows the right placeholder instead of blank.
        if isinstance(val, dict) and not val.get("from_name"):
            from ...auth.email import DEFAULT_FROM_NAME
            val["from_name"] = DEFAULT_FROM_NAME
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

    # Pass the dict directly: asyncpg has a jsonb codec registered (see
    # backend/db/store.py) that handles encoding. Passing json.dumps(value)
    # here would double-encode and store the row as a jsonb STRING instead
    # of a jsonb OBJECT, which then makes downstream loaders see the wrong
    # type and silently disable features (e.g. the email sink).
    await pool.execute(
        """
        INSERT INTO settings (key, value, updated_at, updated_by)
        VALUES ($1, $2, now(), $3)
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value,
              updated_at = EXCLUDED.updated_at,
              updated_by = EXCLUDED.updated_by
        """,
        key, value, user["email"],
    )
    await A.audit(pool, user_email=user["email"], action=f"settings.put",
                  target=f"settings:{key}",
                  payload={"keys": list(value.keys()) if isinstance(value, dict) else None})

    # SMTP changes need to propagate to the running alarm engine's email
    # sink so alarm dispatch picks up the new from_name / credentials
    # without a process restart.
    if key == SMTP_KEY:
        engine = getattr(request.app.state, "engine", None)
        if engine is not None:
            try:
                await engine.refresh_smtp(pool)
            except Exception:
                log.exception("engine.refresh_smtp failed")
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
    from ...auth.email import from_header

    msg = EmailMessage()
    msg["From"] = from_header(cfg)
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


# ---------------------------------------------------------------------------
# Alert routing config
# ---------------------------------------------------------------------------

ALERTS_KEY = "alerts_config"


@router.get("/alerts")
async def get_alerts(
    request: Request, user: Annotated[dict, Depends(require_admin)],
):
    """Return the current routing config. If no DB row exists yet, returns
    the on-disk YAML so the editor can show the starting state."""
    pool = request.app.state.store.pool
    row = await pool.fetchrow(
        "SELECT value, updated_at, updated_by FROM settings WHERE key = $1",
        ALERTS_KEY,
    )
    if row is not None:
        val = row["value"]
        if isinstance(val, str):
            val = json.loads(val)
        return {
            "source":     "db",
            "value":      val,
            "updated_at": row["updated_at"].isoformat(),
            "updated_by": row["updated_by"],
        }
    # Fall back to disk
    from ...alarms.models import load_config as _load_yaml
    cfg = _load_yaml()
    return {
        "source":     "yaml",
        "value":      cfg.model_dump(mode="json"),
        "updated_at": None,
        "updated_by": None,
    }


@router.put("/alerts")
async def put_alerts(
    request: Request, user: Annotated[dict, Depends(require_admin)],
    body: dict = Body(...),
):
    """Validate the submitted routing config, persist to DB, hot-reload
    the running alarm engine."""
    value = body.get("value")
    if value is None:
        raise HTTPException(400, "missing 'value'")
    try:
        new_cfg = AlertsConfig.model_validate(value)
    except Exception as e:
        raise HTTPException(400, f"invalid config: {e}")
    pool = request.app.state.store.pool
    serialized = new_cfg.model_dump(mode="json")
    # Pass the dict directly — see comment in put_setting about the jsonb
    # codec / double-encoding pitfall.
    await pool.execute(
        """
        INSERT INTO settings (key, value, updated_at, updated_by)
        VALUES ($1, $2, now(), $3)
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value,
              updated_at = EXCLUDED.updated_at,
              updated_by = EXCLUDED.updated_by
        """,
        ALERTS_KEY, serialized, user["email"],
    )
    # Hot-reload the running engine so changes take effect without a restart.
    engine = getattr(request.app.state, "engine", None)
    if engine is not None:
        try:
            await engine.reload(new_cfg)
        except Exception:
            log.exception("alarm engine reload failed")
    await A.audit(pool, user_email=user["email"], action="alerts.put",
                  payload={"receivers": len(serialized.get("receivers") or []),
                           "routes":    len(serialized.get("routes") or []),
                           "policies":  len(serialized.get("escalation_policies") or [])})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Dispatch a synthetic test alert through one routing rule.
#
# Uses the currently-running engine config (so the user must save first).
# Targets the rule's policy's *first* step only — same shape the real
# dispatcher fires on opening, no escalation delay involved.
# ---------------------------------------------------------------------------

@router.post("/alerts/test")
async def test_alert(
    request: Request, user: Annotated[dict, Depends(require_admin)],
    body: dict = Body(...),
):
    from datetime import datetime, timezone
    route_idx = body.get("route_index")
    if not isinstance(route_idx, int):
        raise HTTPException(400, "route_index (int) required")
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(500, "alarm engine not running")
    cfg = engine.cfg
    if route_idx < 0 or route_idx >= len(cfg.routes):
        raise HTTPException(400, f"route_index {route_idx} out of range")
    route = cfg.routes[route_idx]
    policy = cfg.policy(route.policy)
    if policy is None:
        raise HTTPException(400, f"plan {route.policy!r} not found")
    if not policy.steps:
        raise HTTPException(400, f"plan {route.policy!r} has no steps")
    step = policy.steps[0]

    fake_alarm = {
        "id":             -1,
        "check_id":       route.match.get("check_id") or "test.synthetic",
        "target":         route.match.get("target")   or "TEST",
        "stage":          route.match.get("stage")    or "L0",
        "severity":       route.severity_floor or "warn",
        "status_at_open": route.match.get("status_at_open") or "warn",
        "opened_at":      datetime.now(timezone.utc),
        "suppressed_by":  None,
        "message":        "[TEST] Synthetic alert from the admin UI — no actual alarm fired.",
        "payload":        {"test": True},
    }
    results: list[dict] = []
    for receiver_name in step.receivers:
        recv = cfg.receiver(receiver_name)
        if recv is None:
            results.append({"receiver": receiver_name, "channel": None,
                            "ok": False, "error": "unknown receiver"})
            continue
        channels: list[str] = []
        if recv.email:   channels.append("email")
        if recv.webhook: channels.append("webhook")
        if recv.console: channels.append("console")
        if not channels:
            results.append({"receiver": receiver_name, "channel": None,
                            "ok": False, "error": "no channels configured"})
            continue
        for ch in channels:
            sink = engine.sinks.get(ch)
            if sink is None:
                results.append({"receiver": receiver_name, "channel": ch,
                                "ok": False, "error": "sink unavailable"})
                continue
            try:
                sr = await sink.send(fake_alarm, recv, route, 0)
                results.append({"receiver": receiver_name, "channel": ch,
                                "ok": sr.delivered, "error": sr.error})
            except Exception as e:
                results.append({"receiver": receiver_name, "channel": ch,
                                "ok": False, "error": f"{type(e).__name__}: {e}"})
    pool = request.app.state.store.pool
    await A.audit(pool, user_email=user["email"], action="alerts.test",
                  payload={"route_index": route_idx, "results": results})
    ok_overall = bool(results) and all(r["ok"] for r in results)
    return {"ok": ok_overall, "results": results,
            "step_receivers": list(step.receivers)}


# ---------------------------------------------------------------------------
# Audit log viewer
# ---------------------------------------------------------------------------

@router.get("/audit")
async def list_audit(
    request: Request, user: Annotated[dict, Depends(require_admin)],
    limit: int = 200,
):
    if limit < 1 or limit > 1000:
        raise HTTPException(400, "limit must be 1..1000")
    pool = request.app.state.store.pool
    rows = await pool.fetch(
        "SELECT id, at, user_email, action, target, payload "
        "FROM admin_audit ORDER BY at DESC LIMIT $1",
        limit,
    )
    return [
        {
            "id":         r["id"],
            "at":         r["at"].isoformat(),
            "user_email": r["user_email"],
            "action":     r["action"],
            "target":     r["target"],
            "payload":    r["payload"],
        }
        for r in rows
    ]
