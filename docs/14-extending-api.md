# Adding an API endpoint

Sentinel's API is FastAPI. Adding a route is a one-file change:
create a router or extend an existing one in `backend/api/routes/`
and include it in `backend/api/app.py`.

This doc covers the conventions (auth, error handling, response
shapes) so your new endpoint fits in.

**Companion**: the live API reference at `/api/docs` (Swagger UI)
shows every existing endpoint as a working example.

---

## Where routes live

All API routes are under the `/api/` prefix, organized by feature:

```
backend/api/routes/
├── auth.py        — login, logout, password reset
├── status.py      — /api/status (the rollup)
├── checks.py      — /api/checks (registry introspection)
├── alarms.py      — alarm list, ack/unack
├── history.py     — historical data + timeline
├── upstream.py    — proxy to the monitored upstream
├── admin.py       — admin actions (gated to role=admin)
├── push.py        — Web Push subscriptions
├── users.py       — user management
├── silences.py    — silence CRUD
├── audit.py       — audit log query
└── ws.py          — WebSocket endpoint
```

Each file exports a `router: APIRouter` that's included in
`create_app()`:

```python
# backend/api/app.py
from .routes import auth, status, checks, ...
app.include_router(auth.router)
app.include_router(status.router)
# ...
```

---

## Conventions

### Path prefix

All routes are mounted under `/api/`. Use:

```python
router = APIRouter(prefix="/api/myfeature")
```

The `/api/` prefix matters because the standard reverse-proxy
convention routes `/api/*` to the backend and everything else to
the frontend (see [`02-deployment.md` § TLS + reverse
proxy](02-deployment.md#5-tls-reverse-proxy)).

### Authentication

Three patterns:

**1. Public (no auth)** — most rare. The VAPID public key endpoint
is one of the few:

```python
@router.get("/vapid_public")
async def vapid_public(request: Request):
    # ...
```

**2. Requires a logged-in user** — use the `require_user`
dependency:

```python
from .auth import require_user

@router.get("/me/something")
async def my_something(
    request: Request,
    user: Annotated[dict, Depends(require_user)],
):
    # user["id"], user["email"], user["role"] available
    ...
```

**3. Requires admin role** — use `require_admin`:

```python
from .auth import require_admin

@router.post("/admin/dangerous_thing")
async def dangerous_thing(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    body: dict = Body(...),
):
    # raises 403 if user.role != 'admin'
    ...
```

Auth flows through `Authorization: Bearer <session-id>` from the
frontend's `installFetchPrefix()` — `require_user` reads the
session row, validates expiry, returns the user dict.

### Accessing shared state

Every dependency you need is on `request.app.state`:

```python
async def my_route(request: Request, ...):
    pool = request.app.state.store.pool       # asyncpg pool
    ctx  = request.app.state.context          # CheckContext (http, network)
    ws   = request.app.state.ws_manager       # WebSocket fanout
```

Don't reach for module-level globals — they break under multi-worker
setups. The `app.state` pattern is the FastAPI-idiomatic escape hatch.

### Error handling

Raise `HTTPException` with a status code + a plain-English detail.
Use the [`humanize_error()`](https://github.com/jkmesches/SentinelProject/blob/main/backend/errors.py)
helper for upstream exceptions:

```python
from ...errors import humanize_error

try:
    r = await ctx.http.get(some_url)
except Exception as e:
    raise HTTPException(502, f"Upstream unavailable: {humanize_error(e)}")
```

Do **NOT** let raw exceptions bubble — FastAPI's default 500
response carries a stack trace in JSON that confuses users. The
goal is "every error message a non-engineer can read."

### Response shapes

Return plain dicts (FastAPI serializes them). For list responses,
return lists directly:

```python
@router.get("/things")
async def list_things():
    rows = await pool.fetch("SELECT id, name FROM things ORDER BY id")
    return [{"id": r["id"], "name": r["name"]} for r in rows]
```

For non-trivial shapes, define a Pydantic model so the auto-docs
expose the schema:

```python
from pydantic import BaseModel

class ThingOut(BaseModel):
    id:   int
    name: str
    tags: list[str]

@router.get("/things/{id}", response_model=ThingOut)
async def get_thing(id: int) -> ThingOut:
    # ...
```

The auto-docs at `/api/docs` (Swagger UI) display the model
automatically.

### WebSocket broadcasts

If your endpoint mutates state that subscribers should know about,
push a WebSocket message:

```python
async def my_route(request: Request, ...):
    # ... do the mutation ...
    await request.app.state.ws_manager.broadcast({
        "type": "thing_updated",
        "thing": {"id": id, "name": new_name},
    })
    return {"ok": True}
```

The frontend's `lib/ws.ts` already handles `type`-tagged messages
— add a handler for your new type in `lib/stores/state.svelte.ts`.

**Don't broadcast on read endpoints.** Only on writes / state
transitions.

---

## Worked example: adding a thing-counter endpoint

```python
"""backend/api/routes/things.py"""
from __future__ import annotations
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from .auth import require_user

router = APIRouter(prefix="/api/things")


@router.get("/count")
async def count_things(
    request: Request,
    user: Annotated[dict, Depends(require_user)],
):
    """Total count of things in the database. Requires login."""
    pool = request.app.state.store.pool
    n = await pool.fetchval("SELECT count(*) FROM things")
    return {"count": int(n or 0)}
```

Wire it in:

```python
# backend/api/app.py
from .routes import things
# ...
app.include_router(things.router)
```

Restart the backend. The new endpoint is live at
`/api/things/count` and appears in `/api/docs` automatically with
its parameter + response schema.

---

## Admin endpoints

Admin routes have one extra responsibility: write to the audit log.
Use the helper:

```python
from ...auth.audit import audit

@router.post("/admin/change_something")
async def change_something(
    request: Request,
    user: Annotated[dict, Depends(require_admin)],
    body: dict = ...,
):
    pool = request.app.state.store.pool
    # ... do the mutation ...
    await audit(
        pool,
        user_email=user["email"],
        action="change_something",
        target=body["thing_id"],
        details={"old": old_value, "new": new_value},
    )
    return {"ok": True}
```

The audit row shows up in `/admin/audit` with the actor, the
action, and a diff in the `details` jsonb column.

---

## Versioning + breaking changes

Sentinel doesn't version `/api/*` paths. The API is internal —
consumed only by Sentinel's own frontend, plus whatever external
monitoring you've pointed at `/api/_debug/stats`.

If you ever need to break an external-facing endpoint, the right
pattern is:

1. Add a new endpoint at a new path (`/api/things/v2/...`).
2. Keep the old one stable, mark it deprecated in the OpenAPI
   description.
3. Migrate the frontend to the new path.
4. Drop the old one in a major release after a deprecation window.

---

## What to NOT do

- **Don't add `credentials: 'include'` to fetch calls** on the
  frontend side — combined with our `allow_origins=['*']` CORS
  config, the browser rejects the response. Bearer-token auth is
  the right path.
- **Don't accept query-string bodies for write endpoints.** Use
  `Body(...)` so the request shape is in the OpenAPI doc.
- **Don't bypass the auth dependencies.** Every non-public
  endpoint should declare `require_user` or `require_admin`.
- **Don't return raw exception strings in error responses.** Use
  `humanize_error()`.
- **Don't read from app.state in module scope.** It's only
  populated by the lifespan handler — module-level access happens
  before that.

---

## Where to go from here

- **[`13-extending-checks.md`](13-extending-checks.md)** — adding
  a new monitoring check (most likely companion if you're adding
  feature-level routes).
- **[`16-alarm-engine.md`](#)** — internals of the alarm dispatch
  pipeline. Read if you're wiring a route that participates in
  alarm flow (acks, manual triggers, etc.).
- **`/api/docs`** — the live API reference. Every existing
  endpoint with its schemas, all auto-generated from the FastAPI
  decorators.
