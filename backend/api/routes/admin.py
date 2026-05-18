"""Admin endpoints: settings (SMTP, alert routing), test-email,
audit log viewer.

All endpoints require an admin session.
"""
from __future__ import annotations
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request

import asyncio
from datetime import datetime, timezone

from ... import auth as A
from ... import thresholds as _thresholds
from ... import reprocess_engine as _reproc
from ... import groups as _groups
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


# ---------------------------------------------------------------------------
# Thresholds — admin-managed knobs for every check evaluator.
#
# GET returns the persisted blob + the seed defaults so the UI can show
# "current vs default" inline without a separate round-trip. PUT validates,
# writes, and refreshes the in-process cache; the next check tick picks up
# the new values without a process restart.
# ---------------------------------------------------------------------------

@router.get("/thresholds")
async def get_thresholds(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
):
    pool = request.app.state.store.pool
    persisted = await _thresholds.fetch_blob(pool)
    return {
        "value":      persisted["value"],
        "defaults":   _thresholds.seed_defaults(),
        "updated_at": persisted["updated_at"],
        "updated_by": persisted["updated_by"],
        "version":    _thresholds.current_version(),
    }


def _validate_thresholds(blob: object) -> dict:
    """Light structural validation. The UI is the primary line of defense;
    this just rejects obvious type errors that would corrupt the cache."""
    if not isinstance(blob, dict):
        raise HTTPException(400, "thresholds body must be an object")
    for section in ("products", "radars", "l4", "globals"):
        if section in blob and not isinstance(blob[section], dict):
            raise HTTPException(400, f"thresholds.{section} must be an object")
    # Per-section value sanity. None entries are allowed (= "use default").
    for pid, p in (blob.get("products") or {}).items():
        if not isinstance(p, dict):
            raise HTTPException(400, f"products.{pid} must be an object")
        for k, v in p.items():
            if v is None: continue
            if k in ("max_freshness_s", "min_png_bytes", "expected_steps", "cadence_s"):
                if not isinstance(v, (int, float)):
                    raise HTTPException(400, f"products.{pid}.{k} must be a number")
    for rid, r in (blob.get("radars") or {}).items():
        if not isinstance(r, dict):
            raise HTTPException(400, f"radars.{rid} must be an object")
        if "silent_fail_s" in r and r["silent_fail_s"] is not None:
            if not isinstance(r["silent_fail_s"], (int, float)) or r["silent_fail_s"] <= 0:
                raise HTTPException(400, f"radars.{rid}.silent_fail_s must be positive number")
    for pid, p in (blob.get("l4") or {}).items():
        if not isinstance(p, dict):
            raise HTTPException(400, f"l4.{pid} must be an object")
        for k, v in p.items():
            if v is None: continue
            if k in ("extreme_threshold", "frozen_min_cov_pct"):
                if not isinstance(v, (int, float)):
                    raise HTTPException(400, f"l4.{pid}.{k} must be a number")
            elif k in ("skip_frozen", "skip_range_ring"):
                if not isinstance(v, bool):
                    raise HTTPException(400, f"l4.{pid}.{k} must be a boolean")
    g = blob.get("globals") or {}
    if not isinstance(g, dict):
        raise HTTPException(400, "globals must be an object")
    for k, v in g.items():
        if v is None: continue
        if not isinstance(v, (int, float)):
            raise HTTPException(400, f"globals.{k} must be a number")
    return blob  # type: ignore[return-value]


@router.put("/thresholds")
async def put_thresholds(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    body: dict = Body(...),
):
    """Persist a full threshold blob. Pass {"value": {...}}; partial updates
    are NOT supported — the UI sends the full validated blob so a stale
    section can't silently revert.

    On success the in-process cache is refreshed and the next check tick
    picks up the new values.
    """
    blob = body.get("value")
    if blob is None:
        raise HTTPException(400, "missing 'value'")
    validated = _validate_thresholds(blob)
    pool = request.app.state.store.pool
    await _thresholds.save_blob(pool, validated, updated_by=user["email"])
    await A.audit(
        pool, user_email=user["email"], action="thresholds.put",
        target="settings:thresholds",
        payload={"sections": sorted(validated.keys())},
    )
    return {
        "ok":         True,
        "updated_at": (await _thresholds.fetch_blob(pool))["updated_at"],
        "version":    _thresholds.current_version(),
    }


# ---------------------------------------------------------------------------
# Retroactive reprocess — re-classify historical check_runs under current
# thresholds. Spawned as a background asyncio task; admin polls status by
# job_id and can request cancellation.
# ---------------------------------------------------------------------------

def _parse_iso(s: str | None, fallback: datetime) -> datetime:
    if not s:
        return fallback
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(400, f"bad ISO datetime: {s!r}")


@router.post("/thresholds/reprocess")
async def start_reprocess(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    body: dict = Body(...),
):
    """Start a retroactive reprocess job. Returns the job_id immediately;
    poll /thresholds/reprocess/status?job_id=… for progress."""
    confirm = body.get("confirm") or ""
    if confirm != "APPLY":
        raise HTTPException(400, "confirm phrase 'APPLY' required")
    now = datetime.now(timezone.utc)
    since = _parse_iso(body.get("since"), now)
    until = _parse_iso(body.get("until"), now)
    if since >= until:
        raise HTTPException(400, "since must be before until")
    only_stages = body.get("only_stages")
    if only_stages and not isinstance(only_stages, list):
        raise HTTPException(400, "only_stages must be a list of stage IDs")

    pool = request.app.state.store.pool
    job_id = _reproc.new_job_id()
    job = _reproc.ReprocessJob(job_id, since, until, only_stages)
    _reproc._jobs[job_id] = job
    asyncio.create_task(_reproc.run_reprocess(pool, job))
    await A.audit(
        pool, user_email=user["email"], action="thresholds.reprocess",
        target=f"job:{job_id}",
        payload={"since": since.isoformat(), "until": until.isoformat(),
                 "only_stages": only_stages},
    )
    return {"job_id": job_id, "state": job.state}


@router.get("/thresholds/reprocess/status")
async def reprocess_status(
    job_id: str,
    user: Annotated[dict, Depends(require_admin)],
):
    job = _reproc.get_job(job_id)
    if job is None:
        raise HTTPException(404, "unknown job_id")
    return job.to_dict()


@router.post("/thresholds/reprocess/cancel")
async def reprocess_cancel(
    user: Annotated[dict, Depends(require_admin)],
    body: dict = Body(...),
):
    job_id = body.get("job_id")
    if not job_id:
        raise HTTPException(400, "missing job_id")
    job = _reproc.get_job(job_id)
    if job is None:
        raise HTTPException(404, "unknown job_id")
    job.cancel_requested = True
    return {"ok": True, "job_id": job_id, "state": job.state}


# ---------------------------------------------------------------------------
# Groups — bundle users + a notification schedule. Used by alert routing
# (Group 10) to gate recipient dispatch by time-of-day / weekday / biweekly.
# Schema in db/schema.sql:groups + group_members; evaluator in backend/groups.py.
# ---------------------------------------------------------------------------

def _validate_schedule(s: object) -> dict:
    if s is None or s == {}:
        return {}
    if not isinstance(s, dict):
        raise HTTPException(400, "schedule must be an object")
    kind = s.get("kind")
    if kind not in (None, "always", "weekly", "biweekly"):
        raise HTTPException(400, f"schedule.kind must be always|weekly|biweekly, got {kind!r}")
    wd = s.get("weekdays")
    if wd is not None:
        if not isinstance(wd, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in wd):
            raise HTTPException(400, "schedule.weekdays must be a list of ints in [0,6]")
    tw = s.get("time_windows")
    if tw is not None:
        if not isinstance(tw, list):
            raise HTTPException(400, "schedule.time_windows must be a list")
        for pair in tw:
            if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair)):
                raise HTTPException(400, "schedule.time_windows entries must be [HH:MM, HH:MM]")
    if "anchor_date" in s and s["anchor_date"] is not None:
        if not isinstance(s["anchor_date"], str):
            raise HTTPException(400, "schedule.anchor_date must be YYYY-MM-DD string")
    if "downtime" in s and s["downtime"] is not None:
        if not isinstance(s["downtime"], list):
            raise HTTPException(400, "schedule.downtime must be a list of {start, end}")
    return s


async def _serialize_group(pool, row: dict) -> dict:
    """Expand a group row with member emails + parent name for the UI."""
    sched = row["schedule"] or {}
    if isinstance(sched, str):
        sched = json.loads(sched)
    members = await pool.fetch(
        "SELECT u.id, u.email, u.display_name FROM group_members gm "
        "JOIN users u ON u.id = gm.user_id WHERE gm.group_id = $1 "
        "ORDER BY u.email",
        row["id"],
    )
    parent_name = None
    if row["parent_group_id"]:
        p = await pool.fetchrow("SELECT name FROM groups WHERE id = $1", row["parent_group_id"])
        if p:
            parent_name = p["name"]
    return {
        "id":              row["id"],
        "name":            row["name"],
        "description":     row["description"],
        "parent_group_id": row["parent_group_id"],
        "parent_name":     parent_name,
        "schedule":        sched,
        "members":         [{"id": m["id"], "email": m["email"],
                              "display_name": m["display_name"]} for m in members],
        "created_at":      row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at":      row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.get("/groups")
async def list_groups(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
):
    pool = request.app.state.store.pool
    rows = await pool.fetch(
        "SELECT id, name, description, parent_group_id, schedule, "
        "       created_at, updated_at FROM groups ORDER BY name"
    )
    return [await _serialize_group(pool, dict(r)) for r in rows]


@router.post("/groups")
async def create_group(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    body: dict = Body(...),
):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    description = body.get("description") or ""
    parent_id = body.get("parent_group_id")
    schedule = _validate_schedule(body.get("schedule") or {})
    pool = request.app.state.store.pool
    try:
        row = await pool.fetchrow(
            "INSERT INTO groups (name, description, parent_group_id, schedule, created_by) "
            "VALUES ($1, $2, $3, $4, $5) "
            "RETURNING id, name, description, parent_group_id, schedule, created_at, updated_at",
            name, description, parent_id, schedule, user["email"],
        )
    except Exception as e:
        raise HTTPException(400, f"create failed: {e}")
    member_ids = body.get("member_ids") or []
    if isinstance(member_ids, list) and member_ids:
        await pool.executemany(
            "INSERT INTO group_members (group_id, user_id, added_by) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING",
            [(row["id"], int(uid), user["email"]) for uid in member_ids],
        )
    await A.audit(pool, user_email=user["email"], action="groups.create",
                  target=f"group:{row['id']}", payload={"name": name})
    return await _serialize_group(pool, dict(row))


@router.put("/groups/{gid}")
async def update_group(
    gid: int, request: Request,
    user: Annotated[dict, Depends(require_admin)],
    body: dict = Body(...),
):
    pool = request.app.state.store.pool
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    parent_id = body.get("parent_group_id")
    if parent_id == gid:
        raise HTTPException(400, "group cannot inherit from itself")
    schedule = _validate_schedule(body.get("schedule") or {})
    description = body.get("description") or ""
    row = await pool.fetchrow(
        "UPDATE groups SET name = $1, description = $2, parent_group_id = $3, "
        "  schedule = $4, updated_at = now() "
        "WHERE id = $5 "
        "RETURNING id, name, description, parent_group_id, schedule, created_at, updated_at",
        name, description, parent_id, schedule, gid,
    )
    if row is None:
        raise HTTPException(404, "group not found")
    # Replace member set if supplied. Absent => leave membership alone.
    if "member_ids" in body and isinstance(body["member_ids"], list):
        await pool.execute("DELETE FROM group_members WHERE group_id = $1", gid)
        if body["member_ids"]:
            await pool.executemany(
                "INSERT INTO group_members (group_id, user_id, added_by) VALUES ($1, $2, $3) "
                "ON CONFLICT DO NOTHING",
                [(gid, int(uid), user["email"]) for uid in body["member_ids"]],
            )
    await A.audit(pool, user_email=user["email"], action="groups.update",
                  target=f"group:{gid}", payload={"name": name})
    return await _serialize_group(pool, dict(row))


@router.delete("/groups/{gid}")
async def delete_group(
    gid: int, request: Request,
    user: Annotated[dict, Depends(require_admin)],
):
    pool = request.app.state.store.pool
    res = await pool.execute("DELETE FROM groups WHERE id = $1", gid)
    if res == "DELETE 0":
        raise HTTPException(404, "group not found")
    await A.audit(pool, user_email=user["email"], action="groups.delete",
                  target=f"group:{gid}", payload={})
    return {"ok": True, "id": gid}


@router.post("/groups/{gid}/preview")
async def preview_schedule(
    gid: int, request: Request,
    user: Annotated[dict, Depends(require_admin)],
    body: dict = Body(default={}),
):
    """Return the next N on-windows (default 5) from a given start time
    (default = now) using the schedule in the request body — letting the
    admin UI preview a draft before saving. If `body.schedule` is missing,
    uses the persisted schedule for `gid`.
    """
    pool = request.app.state.store.pool
    schedule = body.get("schedule")
    if schedule is None:
        row = await pool.fetchrow("SELECT schedule FROM groups WHERE id = $1", gid)
        if row is None:
            raise HTTPException(404, "group not found")
        schedule = row["schedule"] or {}
        if isinstance(schedule, str):
            schedule = json.loads(schedule)
    _validate_schedule(schedule)
    from datetime import datetime as _dt, timezone as _tz
    start = _dt.now(_tz.utc)
    if body.get("start"):
        try:
            start = _dt.fromisoformat(str(body["start"]).replace("Z", "+00:00"))
        except Exception:
            raise HTTPException(400, "bad start datetime")
    count = int(body.get("count") or 5)
    windows = _groups.next_on_windows(schedule, start, count=count)
    return {
        "windows": [
            {"start": s.isoformat(), "end": e.isoformat()}
            for (s, e) in windows
        ],
        "active_now": _groups.is_active_at(schedule, start),
    }
