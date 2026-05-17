"""Web Push (RFC 8030 / VAPID) for the mobile PWA.

Lifecycle:
  - VAPID keys are auto-generated on first use and stored in the
    `settings` table under key `vapid_keys`. No env var or manual setup
    required — first call to get_vapid() creates them.
  - The browser subscribes via the PushManager API using the VAPID
    public key, posts the subscription to /api/push/subscribe, and
    the row lands in `push_subscriptions`.
  - When an alarm opens, the engine's listener calls dispatch_push()
    which fans out to every subscription via pywebpush (sync, wrapped
    in asyncio.to_thread).

iOS 16.4+ only delivers pushes to PWAs the user has Added to Home
Screen first — see /m/more for the install prompt.
"""
from __future__ import annotations
import asyncio
import base64
import json
import logging
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from .config import SETTINGS

log = logging.getLogger(__name__)


VAPID_SETTING_KEY = "vapid_keys"
# Subject in the JWT claims — required by some push services; can be
# either a mailto: URL or an https URL identifying the application.
def _vapid_subject() -> str:
    if SETTINGS.public_url:
        return SETTINGS.public_url
    return "mailto:noreply@aqpisentinel.local"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _generate_vapid_pair() -> dict[str, str]:
    """Create a fresh P-256 keypair in the formats pywebpush expects."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key()
    raw_priv = priv.private_numbers().private_value.to_bytes(32, "big")
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    # PEM is what pywebpush accepts as the private key argument.
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return {
        "private_pem":      pem,
        "private_raw_b64":  _b64url(raw_priv),
        "public_raw_b64":   _b64url(pub_bytes),
    }


async def get_vapid(pool) -> dict[str, str]:
    """Return the VAPID key dict, creating + persisting if missing."""
    row = await pool.fetchrow(
        "SELECT value FROM settings WHERE key = $1", VAPID_SETTING_KEY
    )
    if row is not None:
        val = row["value"]
        if isinstance(val, str):
            val = json.loads(val)
        if isinstance(val, dict) and "public_raw_b64" in val:
            return val
    keys = _generate_vapid_pair()
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at, updated_by) "
        "VALUES ($1, $2, now(), $3) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
        "updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by",
        VAPID_SETTING_KEY, keys, "system:auto-vapid",
    )
    log.info("generated + persisted new VAPID keypair")
    return keys


def _send_one(subscription: dict, payload: dict, vapid: dict) -> tuple[bool, str | None, int | None]:
    """Synchronous single-subscription send (called via to_thread)."""
    try:
        resp = webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=vapid["private_pem"],
            vapid_claims={"sub": _vapid_subject()},
            ttl=300,
        )
        return True, None, getattr(resp, "status_code", None)
    except WebPushException as e:
        # 404/410 → subscription expired and should be deleted.
        status = getattr(e.response, "status_code", None) if e.response is not None else None
        return False, f"{type(e).__name__}: {e}", status
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", None


async def dispatch_push(pool, payload: dict) -> dict[str, int]:
    """Fan-out a single push payload to every active subscription.

    Returns a count dict for logging: {sent, failed, expired}.
    Expired (404/410) subscriptions are deleted from the table so
    the next dispatch doesn't retry them.
    """
    vapid = await get_vapid(pool)
    rows = await pool.fetch(
        "SELECT id, endpoint, p256dh, auth FROM push_subscriptions"
    )
    sent = failed = expired = 0
    expired_ids: list[int] = []
    for row in rows:
        ok, err, status = await asyncio.to_thread(_send_one, dict(row), payload, vapid)
        if ok:
            sent += 1
            continue
        if status in (404, 410):
            expired += 1
            expired_ids.append(row["id"])
        else:
            failed += 1
            log.warning("push to %s failed: %s", row["endpoint"][:60], err)
    if expired_ids:
        await pool.execute(
            "DELETE FROM push_subscriptions WHERE id = ANY($1)", expired_ids,
        )
    if rows:
        await pool.execute(
            "UPDATE push_subscriptions SET last_used_at = now() "
            "WHERE id = ANY($1)",
            [r["id"] for r in rows if r["id"] not in expired_ids],
        )
    return {"sent": sent, "failed": failed, "expired": expired}
