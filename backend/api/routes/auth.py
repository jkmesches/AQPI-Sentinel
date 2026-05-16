"""Login / logout / me + a `current_user` dependency for protected routes.

Bearer-token sessions: a UUID stored in `sessions`, returned in the
login response. The frontend stores it in localStorage and sends it on
every protected request as `Authorization: Bearer <token>`. We still
set the value as a cookie too for browser convenience in dev — either
source authenticates a request.

Why not cookies alone? In the production no-proxy deployment the
frontend (:3000) and backend (:8000) are cross-origin, and browsers
won't send SameSite=Lax cookies cross-origin. SameSite=None would
require HTTPS, which we don't have on the LAN. Bearer tokens sidestep
both constraints.
"""
from __future__ import annotations
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response

from ... import auth as A

router = APIRouter(prefix="/api/auth")


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def _extract_token(authorization: str | None, cookie: str | None) -> str | None:
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
            return parts[1].strip()
    if cookie:
        return cookie
    return None


async def current_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    sentinel_session: Annotated[str | None, Cookie()] = None,
) -> dict | None:
    """Return the user row attached to the session token, or None."""
    token = _extract_token(authorization, sentinel_session)
    if not token:
        return None
    pool = request.app.state.store.pool
    return await A.fetch_session(pool, token)


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
    # Cookie is still set for dev convenience (same-origin via Vite proxy).
    # Production uses the returned `token` via Authorization: Bearer.
    response.set_cookie(
        A.COOKIE_NAME, sid,
        httponly=True, samesite="lax",
        max_age=int(A.SESSION_LIFETIME.total_seconds()),
    )
    await A.audit(pool, user_email=u["email"], action="login")
    return {
        "token":        sid,
        "id":           u["id"],
        "email":        u["email"],
        "role":         u["role"],
        "display_name": u.get("display_name"),
    }


@router.post("/logout")
async def logout(
    request: Request, response: Response,
    authorization: Annotated[str | None, Header()] = None,
    sentinel_session: Annotated[str | None, Cookie()] = None,
):
    token = _extract_token(authorization, sentinel_session)
    if token:
        await A.destroy_session(request.app.state.store.pool, token)
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
