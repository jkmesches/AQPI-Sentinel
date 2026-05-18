"""Web Push subscription endpoints + VAPID public key exposure.

Endpoints (all require a logged-in user except the public key):
  GET    /api/push/vapid_public      raw VAPID public key (b64url)
  POST   /api/push/subscribe         persist a browser subscription
  DELETE /api/push/unsubscribe       remove by endpoint (idempotent)
  GET    /api/push/status            does this user have any subscriptions?
"""
from __future__ import annotations
import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ... import push as P
from .auth import require_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/push")


@router.get("/vapid_public")
async def vapid_public(request: Request):
    """Return the VAPID public key as base64url. Browser passes this
    to PushManager.subscribe({ applicationServerKey: ... })."""
    keys = await P.get_vapid(request.app.state.store.pool)
    return {"public_key_b64": keys["public_raw_b64"]}


@router.post("/subscribe")
async def subscribe(
    request: Request,
    user: Annotated[dict, Depends(require_user)],
    body: dict = Body(...),
):
    """Persist a browser subscription. `body` is the JSON-encoded
    PushSubscription from the client — { endpoint, keys: { p256dh, auth } }.
    Endpoint is unique; re-subscribing the same endpoint just updates the
    user_id + key fields (covers logout/login on same device)."""
    endpoint = (body.get("endpoint") or "").strip()
    keys = body.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        raise HTTPException(400, "missing endpoint or keys")
    ua = request.headers.get("user-agent", "")
    pool = request.app.state.store.pool
    await pool.execute(
        """
        INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (endpoint) DO UPDATE
          SET user_id    = EXCLUDED.user_id,
              p256dh     = EXCLUDED.p256dh,
              auth       = EXCLUDED.auth,
              user_agent = EXCLUDED.user_agent,
              created_at = now()
        """,
        user["id"], endpoint, p256dh, auth, ua[:200],
    )
    return {"ok": True}


@router.delete("/unsubscribe")
async def unsubscribe(
    request: Request,
    user: Annotated[dict, Depends(require_user)],
    body: dict = Body(...),
):
    """Remove a subscription by endpoint. Idempotent — missing rows
    don't error so the client can call this even if the local
    subscription was already revoked."""
    endpoint = (body.get("endpoint") or "").strip()
    if not endpoint:
        raise HTTPException(400, "missing endpoint")
    pool = request.app.state.store.pool
    # Restrict to subscriptions owned by the calling user so one user
    # can't yank another's subscription by guessing the endpoint.
    await pool.execute(
        "DELETE FROM push_subscriptions WHERE endpoint = $1 AND user_id = $2",
        endpoint, user["id"],
    )
    return {"ok": True}


@router.get("/status")
async def status(
    request: Request,
    user: Annotated[dict, Depends(require_user)],
):
    """Returns whether the current user has any active push
    subscriptions. The frontend uses this to set the toggle state."""
    pool = request.app.state.store.pool
    n = await pool.fetchval(
        "SELECT count(*) FROM push_subscriptions WHERE user_id = $1",
        user["id"],
    )
    return {"subscribed": bool(n), "count": int(n or 0)}


def _ser_sub(row: dict) -> dict:
    rc = row.get("routing_config") or {}
    if isinstance(rc, str):
        import json
        rc = json.loads(rc)
    return {
        "id":             row["id"],
        "endpoint":       row["endpoint"],
        "user_agent":     row.get("user_agent"),
        "label":          row.get("label"),
        "routing_config": rc if isinstance(rc, dict) else {},
        "created_at":     row["created_at"].isoformat() if row.get("created_at") else None,
        "last_used_at":   row["last_used_at"].isoformat() if row.get("last_used_at") else None,
    }


@router.get("/subscriptions")
async def list_subscriptions(
    request: Request,
    user: Annotated[dict, Depends(require_user)],
):
    """List the calling user's push subscriptions — one per device.
    Admins still see only their own; cross-user lookups aren't supported."""
    pool = request.app.state.store.pool
    rows = await pool.fetch(
        "SELECT id, endpoint, user_agent, label, routing_config, "
        "       created_at, last_used_at "
        "FROM push_subscriptions WHERE user_id = $1 "
        "ORDER BY created_at DESC",
        user["id"],
    )
    return [_ser_sub(dict(r)) for r in rows]


def _validate_routing(rc: object) -> dict:
    if rc is None or rc == {}:
        return {}
    if not isinstance(rc, dict):
        raise HTTPException(400, "routing_config must be an object")
    out: dict = {}
    if "severity_floor" in rc and rc["severity_floor"]:
        if rc["severity_floor"] not in ("info", "warn", "critical"):
            raise HTTPException(400, "severity_floor must be info|warn|critical")
        out["severity_floor"] = rc["severity_floor"]
    if "product_patterns" in rc and rc["product_patterns"]:
        pats = rc["product_patterns"]
        if not isinstance(pats, list) or not all(isinstance(p, str) for p in pats):
            raise HTTPException(400, "product_patterns must be list of strings")
        out["product_patterns"] = [p for p in pats if p.strip()]
    if "delay_s" in rc and rc["delay_s"]:
        d = rc["delay_s"]
        if not isinstance(d, (int, float)) or d < 0 or d > 3600:
            raise HTTPException(400, "delay_s must be a number in [0, 3600]")
        out["delay_s"] = int(d)
    if "schedule" in rc and rc["schedule"]:
        # Schedule shape mirrors backend/groups.py — reuse its validation
        # philosophy: accept anything dict-shaped, the evaluator no-ops on
        # unknown keys.
        if not isinstance(rc["schedule"], dict):
            raise HTTPException(400, "schedule must be an object")
        out["schedule"] = rc["schedule"]
    return out


@router.put("/subscriptions/{sub_id}/routing")
async def put_routing(
    sub_id: int, request: Request,
    user: Annotated[dict, Depends(require_user)],
    body: dict = Body(...),
):
    """Update per-device routing: severity floor / product patterns /
    delay / on-duty schedule. Body fields can include `label` too."""
    pool = request.app.state.store.pool
    row = await pool.fetchrow(
        "SELECT id FROM push_subscriptions WHERE id = $1 AND user_id = $2",
        sub_id, user["id"],
    )
    if row is None:
        raise HTTPException(404, "subscription not found")
    rc = _validate_routing(body.get("routing_config"))
    label = (body.get("label") or "").strip() or None
    await pool.execute(
        "UPDATE push_subscriptions SET routing_config = $1, label = $2 WHERE id = $3",
        rc, label, sub_id,
    )
    return {"ok": True, "id": sub_id}
