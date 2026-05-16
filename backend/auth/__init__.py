"""Authentication: argon2 password hashing, session cookies, audit logging.

Schema lives in db/schema.sql (`users`, `sessions`, `password_reset_tokens`,
`admin_audit`). This module owns the verification + session-management logic;
routes live in api/routes/auth.py.

Conventions:
  * Passwords hashed with argon2id (good default tuning, no pepper).
  * Session id is a UUID stored in `sessions.id`. Set on the client as
    an HttpOnly, SameSite=Lax cookie named `sentinel_session`.
  * Sessions are sliding-window: every authenticated request touches
    `last_seen_at` (cheap) and extends `expires_at` if the remaining
    lifetime drops below SLIDING_RENEW_THRESHOLD.
"""
from __future__ import annotations
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError

log = logging.getLogger(__name__)

# Cookie + session tuning.
COOKIE_NAME = "sentinel_session"
SESSION_LIFETIME = timedelta(days=14)
SLIDING_RENEW_THRESHOLD = timedelta(days=7)

# argon2 defaults are reasonable for an internal tool. Bump time_cost on a
# beefier host if needed.
_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(stored_hash: str, candidate: str) -> bool:
    try:
        _ph.verify(stored_hash, candidate)
        return True
    except (VerifyMismatchError, InvalidHashError, Exception):
        return False


def needs_rehash(stored_hash: str) -> bool:
    return _ph.check_needs_rehash(stored_hash)


def new_token(nbytes: int = 32) -> str:
    """Random URL-safe token for password resets / one-shot links."""
    return secrets.token_urlsafe(nbytes)


# ---------------------------------------------------------------------------
# Session helpers (DB-backed). All take an asyncpg pool.
# ---------------------------------------------------------------------------

async def fetch_user_by_email(pool, email: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT id, email, password_hash, role, display_name, disabled_at "
        "FROM users WHERE lower(email) = lower($1)",
        email.strip(),
    )
    return dict(row) if row else None


async def fetch_user_by_id(pool, user_id: int) -> dict | None:
    row = await pool.fetchrow(
        "SELECT id, email, role, display_name, disabled_at, last_login_at "
        "FROM users WHERE id = $1",
        user_id,
    )
    return dict(row) if row else None


async def create_session(pool, user_id: int, *, user_agent: str | None,
                         ip: str | None) -> str:
    """Insert a new session row, return its UUID as string."""
    now = datetime.now(timezone.utc)
    expires = now + SESSION_LIFETIME
    row = await pool.fetchrow(
        """
        INSERT INTO sessions (user_id, created_at, last_seen_at, expires_at,
                              user_agent, ip)
        VALUES ($1, $2, $2, $3, $4, $5::inet)
        RETURNING id
        """,
        user_id, now, expires, user_agent, ip,
    )
    await pool.execute(
        "UPDATE users SET last_login_at = $1 WHERE id = $2", now, user_id,
    )
    return str(row["id"])


async def fetch_session(pool, sid: str) -> dict | None:
    """Look up + validate a session by id. Touches last_seen_at and may
    extend expires_at (sliding window). Returns the joined user row or None
    if the session is unknown, expired, or the user is disabled."""
    try:
        sid_uuid = UUID(sid)
    except ValueError:
        return None
    row = await pool.fetchrow(
        """
        SELECT s.id AS session_id, s.expires_at,
               u.id, u.email, u.role, u.display_name, u.disabled_at
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.id = $1 AND s.expires_at > now() AND u.disabled_at IS NULL
        """,
        sid_uuid,
    )
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    expires = row["expires_at"]
    if (expires - now) < SLIDING_RENEW_THRESHOLD:
        new_expires = now + SESSION_LIFETIME
        await pool.execute(
            "UPDATE sessions SET last_seen_at = $1, expires_at = $2 WHERE id = $3",
            now, new_expires, sid_uuid,
        )
    else:
        await pool.execute(
            "UPDATE sessions SET last_seen_at = $1 WHERE id = $2", now, sid_uuid,
        )
    return dict(row)


async def destroy_session(pool, sid: str) -> None:
    try:
        sid_uuid = UUID(sid)
    except ValueError:
        return
    await pool.execute("DELETE FROM sessions WHERE id = $1", sid_uuid)


async def destroy_all_sessions_for_user(pool, user_id: int) -> None:
    """Used when changing a user's password or disabling them."""
    await pool.execute("DELETE FROM sessions WHERE user_id = $1", user_id)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

async def audit(pool, *, user_email: str, action: str,
                target: str | None = None, payload: dict | None = None) -> None:
    """Best-effort write to admin_audit. Failures are logged but never
    raise — losing an audit line shouldn't fail the user's action."""
    try:
        import json
        await pool.execute(
            "INSERT INTO admin_audit (user_email, action, target, payload) "
            "VALUES ($1, $2, $3, $4)",
            user_email, action, target,
            json.dumps(payload) if payload is not None else None,
        )
    except Exception:
        log.exception("admin_audit write failed: action=%r target=%r", action, target)


# ---------------------------------------------------------------------------
# Bootstrap: create the first admin from env vars if no users exist.
# ---------------------------------------------------------------------------

async def bootstrap_admin(pool, *, email: str, password: str,
                          display_name: str | None = None) -> bool:
    """If `users` is empty, create an admin from the given credentials.
    Returns True if a user was created, False otherwise. Idempotent.
    """
    count = await pool.fetchval("SELECT count(*) FROM users")
    if count and count > 0:
        return False
    if not email or not password:
        return False
    await pool.execute(
        """
        INSERT INTO users (email, password_hash, role, display_name)
        VALUES ($1, $2, 'admin', $3)
        """,
        email.strip().lower(), hash_password(password), display_name or "Admin",
    )
    log.info("bootstrapped admin user %s", email)
    return True
