"""Admin: user management.

  GET    /api/admin/users           list all
  POST   /api/admin/users           create (returns one-time reset link)
  PATCH  /api/admin/users/{id}      update role / display_name / disabled
  POST   /api/admin/users/{id}/reset  issue a fresh reset link
  DELETE /api/admin/users/{id}      hard delete (and kill sessions)

Plus public reset endpoints under /api/auth so a freshly-invited user
can set their first password from the link without being logged in.

The "reset link" is just a URL the admin can copy to the user; if SMTP
is configured we'll later add an option to email it automatically.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ... import auth as A
from .auth import require_admin, current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/users")
public_router = APIRouter(prefix="/api/auth/reset")


# ---------------------------------------------------------------------------
# Admin-only endpoints
# ---------------------------------------------------------------------------

def _ser(row: dict) -> dict:
    return {
        "id":            row["id"],
        "email":         row["email"],
        "role":          row["role"],
        "display_name":  row.get("display_name"),
        "created_at":    row["created_at"].isoformat() if row.get("created_at") else None,
        "last_login_at": row["last_login_at"].isoformat() if row.get("last_login_at") else None,
        "disabled_at":   row["disabled_at"].isoformat() if row.get("disabled_at") else None,
    }


@router.get("")
async def list_users(
    request: Request,
    me: Annotated[dict, Depends(require_admin)],
):
    pool = request.app.state.store.pool
    rows = await pool.fetch(
        "SELECT id, email, role, display_name, created_at, last_login_at, disabled_at "
        "FROM users ORDER BY id ASC",
    )
    return [_ser(dict(r)) for r in rows]


@router.post("")
async def create_user(
    request: Request,
    me: Annotated[dict, Depends(require_admin)],
    body: dict = Body(...),
):
    email = (body.get("email") or "").strip().lower()
    role = body.get("role") or "user"
    display_name = body.get("display_name") or None
    if not email or "@" not in email:
        raise HTTPException(400, "email required")
    if role not in ("admin", "user"):
        raise HTTPException(400, "role must be admin|user")
    pool = request.app.state.store.pool
    existing = await pool.fetchval("SELECT id FROM users WHERE lower(email) = $1", email)
    if existing:
        raise HTTPException(409, "user with this email already exists")
    row = await pool.fetchrow(
        """
        INSERT INTO users (email, password_hash, role, display_name)
        VALUES ($1, NULL, $2, $3) RETURNING id
        """,
        email, role, display_name,
    )
    user_id = row["id"]
    token = await _issue_reset_token(pool, user_id, hours=72)
    await A.audit(pool, user_email=me["email"], action="user.create",
                  target=f"user:{user_id}",
                  payload={"email": email, "role": role})
    return {
        "ok": True,
        "id": user_id,
        "email": email,
        "reset_token": token,
        "reset_url": f"/reset/{token}",
        "expires_hours": 72,
    }


@router.patch("/{user_id}")
async def patch_user(
    user_id: int, request: Request,
    me: Annotated[dict, Depends(require_admin)],
    body: dict = Body(...),
):
    pool = request.app.state.store.pool
    row = await pool.fetchrow("SELECT id, email, role FROM users WHERE id = $1", user_id)
    if row is None:
        raise HTTPException(404, "no such user")
    fields: list[tuple[str, object]] = []
    if "role" in body:
        if body["role"] not in ("admin", "user"):
            raise HTTPException(400, "role must be admin|user")
        # Don't let the last admin demote themselves.
        if row["role"] == "admin" and body["role"] != "admin":
            n_admins = await pool.fetchval(
                "SELECT count(*) FROM users WHERE role='admin' AND disabled_at IS NULL"
            )
            if n_admins <= 1:
                raise HTTPException(400, "cannot demote the last active admin")
        fields.append(("role", body["role"]))
    if "display_name" in body:
        fields.append(("display_name", body["display_name"]))
    if "disabled" in body:
        if body["disabled"]:
            # Same protection against locking yourself out.
            n_admins = await pool.fetchval(
                "SELECT count(*) FROM users WHERE role='admin' AND disabled_at IS NULL"
            )
            if row["role"] == "admin" and n_admins <= 1:
                raise HTTPException(400, "cannot disable the last active admin")
            fields.append(("disabled_at", datetime.now(timezone.utc)))
            await A.destroy_all_sessions_for_user(pool, user_id)
        else:
            fields.append(("disabled_at", None))
    if not fields:
        return {"ok": True, "id": user_id, "noop": True}
    set_clause = ", ".join(f"{name} = ${i + 2}" for i, (name, _) in enumerate(fields))
    args = [user_id] + [v for _, v in fields]
    await pool.execute(f"UPDATE users SET {set_clause} WHERE id = $1", *args)
    await A.audit(pool, user_email=me["email"], action="user.patch",
                  target=f"user:{user_id}",
                  payload={k: v.isoformat() if isinstance(v, datetime) else v for k, v in fields})
    return {"ok": True, "id": user_id}


@router.post("/{user_id}/reset")
async def admin_issue_reset(
    user_id: int, request: Request,
    me: Annotated[dict, Depends(require_admin)],
):
    pool = request.app.state.store.pool
    if not await pool.fetchval("SELECT 1 FROM users WHERE id = $1", user_id):
        raise HTTPException(404, "no such user")
    token = await _issue_reset_token(pool, user_id, hours=72)
    await A.audit(pool, user_email=me["email"], action="user.reset_issued",
                  target=f"user:{user_id}")
    return {"ok": True, "reset_token": token, "reset_url": f"/reset/{token}", "expires_hours": 72}


@router.delete("/{user_id}")
async def delete_user(
    user_id: int, request: Request,
    me: Annotated[dict, Depends(require_admin)],
):
    pool = request.app.state.store.pool
    row = await pool.fetchrow("SELECT role, email FROM users WHERE id = $1", user_id)
    if row is None:
        raise HTTPException(404, "no such user")
    if row["role"] == "admin":
        n_admins = await pool.fetchval(
            "SELECT count(*) FROM users WHERE role='admin' AND disabled_at IS NULL"
        )
        if n_admins <= 1:
            raise HTTPException(400, "cannot delete the last active admin")
    if user_id == me["id"]:
        raise HTTPException(400, "cannot delete your own account")
    await pool.execute("DELETE FROM users WHERE id = $1", user_id)
    await A.audit(pool, user_email=me["email"], action="user.delete",
                  target=f"user:{user_id}", payload={"email": row["email"]})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Public reset endpoints — accessed without a session, using a one-time token
# ---------------------------------------------------------------------------

async def _issue_reset_token(pool, user_id: int, *, hours: int) -> str:
    token = A.new_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    await pool.execute(
        "INSERT INTO password_reset_tokens (token, user_id, expires_at) "
        "VALUES ($1, $2, $3)",
        token, user_id, expires,
    )
    return token


@public_router.get("/{token}")
async def reset_check(token: str, request: Request):
    """Look up an unused token. Returns the target user's email so the
    reset form can show "Setting password for <email>"."""
    pool = request.app.state.store.pool
    row = await pool.fetchrow(
        "SELECT t.user_id, t.expires_at, t.used_at, u.email "
        "FROM password_reset_tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token = $1", token,
    )
    if row is None:
        raise HTTPException(404, "invalid reset link")
    if row["used_at"] is not None:
        raise HTTPException(410, "this link has already been used")
    if row["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(410, "this link has expired")
    return {"email": row["email"]}


@public_router.post("/{token}")
async def reset_apply(
    token: str, request: Request,
    body: dict = Body(...),
):
    password = body.get("password") or ""
    if len(password) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    pool = request.app.state.store.pool
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT user_id, expires_at, used_at FROM password_reset_tokens WHERE token = $1",
            token,
        )
        if row is None:
            raise HTTPException(404, "invalid reset link")
        if row["used_at"] is not None:
            raise HTTPException(410, "this link has already been used")
        if row["expires_at"] < datetime.now(timezone.utc):
            raise HTTPException(410, "this link has expired")
        await conn.execute(
            "UPDATE users SET password_hash = $1 WHERE id = $2",
            A.hash_password(password), row["user_id"],
        )
        await conn.execute(
            "UPDATE password_reset_tokens SET used_at = now() WHERE token = $1",
            token,
        )
        await A.destroy_all_sessions_for_user(conn, row["user_id"])
    await A.audit(pool, user_email=f"user:{row['user_id']}",
                  action="user.password_reset",
                  target=f"user:{row['user_id']}")
    return {"ok": True}
