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
    """Synchronous single-subscription send (called via to_thread).

    Note on the VAPID private key: pywebpush detects a literal PEM by
    looking for "------BEGIN " in the string and routes that to
    py_vapid.Vapid.from_pem, which (in 1.9.4) is broken — it strips
    the PEM armor and then calls from_raw on the base64 body, but
    from_raw expects a base64url-encoded RAW 32-byte private value,
    not a PKCS8 body. The fix is to pass the raw value directly so
    pywebpush routes it to from_raw.
    """
    try:
        resp = webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=vapid["private_raw_b64"],
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


_SEVERITY_RANK = {"info": 0, "warn": 1, "critical": 2}


def _subscription_matches(routing: dict, payload: dict) -> bool:
    """Per-device filter check.

    routing keys:
      severity_floor:    e.g. "warn" — drop notifications below this rank
      product_patterns:  list[str] — glob-style match against check_id or
                         target. Empty/missing = match anything.

    Schedule gating is handled separately via backend.groups.is_active_at
    (see dispatch_push) because schedule lookup is wall-clock aware.
    """
    import fnmatch
    floor = (routing.get("severity_floor") or "").lower()
    if floor:
        floor_rank = _SEVERITY_RANK.get(floor, 0)
        sev = (payload.get("severity") or payload.get("title", "")).lower()
        # Normalize "[WARN] Foo" → "warn" if needed.
        for k in _SEVERITY_RANK:
            if k in sev:
                sev = k
                break
        sev_rank = _SEVERITY_RANK.get(sev, 0)
        if sev_rank < floor_rank:
            return False

    patterns = routing.get("product_patterns") or []
    if patterns:
        haystack = " ".join([
            str(payload.get("tag") or ""),
            str(payload.get("title") or ""),
            str(payload.get("body") or ""),
        ]).lower()
        matched = any(fnmatch.fnmatch(haystack, f"*{p.lower()}*") for p in patterns)
        if not matched:
            return False
    return True


async def dispatch_push(pool, payload: dict) -> dict[str, int]:
    """Fan-out a single push payload to every active subscription.

    Each subscription is filtered by its own routing_config:
      - severity_floor drops notifications below the chosen rank
      - product_patterns require at least one glob match against the payload
      - schedule (group-schedule shape) gates by wall-clock; off-duty = skip

    delay_s is honored by deferring the send through asyncio.create_task —
    in-process only, lost across restarts. Acceptable for the common
    "snooze me for 5 min" case; durable scheduling can be added later.

    Returns a count dict for logging: {sent, failed, expired, filtered, deferred}.
    """
    from . import groups as _groups
    from datetime import datetime, timezone
    vapid = await get_vapid(pool)
    rows = await pool.fetch(
        "SELECT id, endpoint, p256dh, auth, routing_config FROM push_subscriptions"
    )
    sent = failed = expired = filtered = deferred = 0
    expired_ids: list[int] = []
    now = datetime.now(timezone.utc)
    for row in rows:
        routing = row["routing_config"] or {}
        if isinstance(routing, str):
            routing = json.loads(routing)
        if not isinstance(routing, dict):
            routing = {}

        # Filter: severity floor + product patterns.
        if not _subscription_matches(routing, payload):
            filtered += 1
            continue

        # Schedule gate.
        schedule = routing.get("schedule") or {}
        if schedule and not _groups.is_active_at(schedule, now):
            filtered += 1
            continue

        # Optional delay. Defer via background task; if the device falls
        # off the subscription list before the delay elapses, the send
        # quietly fails when the row is gone.
        delay_s = routing.get("delay_s") or 0
        if isinstance(delay_s, (int, float)) and delay_s > 0:
            deferred += 1
            asyncio.create_task(_send_delayed(pool, dict(row), payload, vapid, float(delay_s)))
            continue

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
    return {"sent": sent, "failed": failed, "expired": expired,
            "filtered": filtered, "deferred": deferred}


async def _send_delayed(pool, subscription: dict, payload: dict, vapid: dict, delay_s: float) -> None:
    """Sleep `delay_s` then attempt to send. Re-checks the row still exists
    so a deleted subscription doesn't get a delayed phantom notification."""
    try:
        await asyncio.sleep(delay_s)
        row = await pool.fetchrow(
            "SELECT id FROM push_subscriptions WHERE id = $1", subscription["id"]
        )
        if row is None:
            return
        ok, err, status = await asyncio.to_thread(_send_one, subscription, payload, vapid)
        if ok:
            await pool.execute(
                "UPDATE push_subscriptions SET last_used_at = now() WHERE id = $1",
                subscription["id"],
            )
        elif status in (404, 410):
            await pool.execute(
                "DELETE FROM push_subscriptions WHERE id = $1", subscription["id"]
            )
        elif err:
            log.warning("delayed push to %s failed: %s", subscription["endpoint"][:60], err)
    except Exception:
        log.exception("delayed push send raised")
