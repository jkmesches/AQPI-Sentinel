"""Login / logout / me + a `current_user` dependency for protected routes.

Cookie-based sessions: a UUID stored in `sessions`, set as an HttpOnly
SameSite=Lax cookie. Reads of the cookie + session look-up live in this
module; protected routes Depend(require_user) or Depend(require_admin).
"""
from __future__ import annotations
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response

from ... import auth as A

router = APIRouter(prefix="/api/auth")


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

async def current_user(
    request: Request,
    sentinel_session: Annotated[str | None, Cookie()] = None,
) -> dict | None:
    """Return the user row attached to the session cookie, or None."""
    if not sentinel_session:
        return None
    pool = request.app.state.store.pool
    return await A.fetch_session(pool, sentinel_session)


async def require_user(user: Annotated[dict | None, Depends(current_user)]) -> dict:
    if user is None:
        raise HTTPException(401, "not authenticated")
    return user


async def require_admin(user: Annotated[dict, Depends(require_user)]) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(403, "admin only")
    return user


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/login")
async def login(request: Request, response: Response, body: dict):
    """Verify credentials, set session cookie. Body: {email, password}."""
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    if not email or not password:
        raise HTTPException(400, "email and password required")
    pool = request.app.state.store.pool
    u = await A.fetch_user_by_email(pool, email)
    if u is None or not u.get("password_hash") or u.get("disabled_at") is not None:
        # Same error message for missing user / wrong password / disabled —
        # don't leak which one.
        raise HTTPException(401, "invalid credentials")
    if not A.verify_password(u["password_hash"], password):
        raise HTTPException(401, "invalid credentials")
    if A.needs_rehash(u["password_hash"]):
        await pool.execute(
            "UPDATE users SET password_hash = $1 WHERE id = $2",
            A.hash_password(password), u["id"],
        )
    sid = await A.create_session(
        pool, u["id"],
        user_agent=request.headers.get("user-agent"),
        ip=(request.client.host if request.client else None),
    )
    response.set_cookie(
        A.COOKIE_NAME, sid,
        httponly=True, samesite="lax",
        max_age=int(A.SESSION_LIFETIME.total_seconds()),
    )
    await A.audit(pool, user_email=u["email"], action="login")
    return {
        "id": u["id"], "email": u["email"],
        "role": u["role"], "display_name": u.get("display_name"),
    }


@router.post("/logout")
async def logout(
    request: Request, response: Response,
    sentinel_session: Annotated[str | None, Cookie()] = None,
):
    if sentinel_session:
        await A.destroy_session(request.app.state.store.pool, sentinel_session)
    response.delete_cookie(A.COOKIE_NAME, samesite="lax")
    return {"ok": True}


@router.get("/me")
async def me(user: Annotated[dict | None, Depends(current_user)]):
    """Return the current user info or 401. The frontend hits this on load
    to determine whether to show the login form vs. the admin nav."""
    if user is None:
        raise HTTPException(401, "not authenticated")
    return {
        "id": user["id"], "email": user["email"],
        "role": user["role"], "display_name": user.get("display_name"),
    }
