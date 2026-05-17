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
