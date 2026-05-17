# Sentinel Architecture

High-level map of how the pieces fit together. The companion
[MAINTENANCE.md](MAINTENANCE.md) covers operations; this one is for
understanding the codebase well enough to make non-trivial changes.

---

## Big picture

```
                     ┌──────────────────────────────┐
                     │   radarca.engr.colostate.edu │  (the system we monitor)
                     └──────────────┬───────────────┘
                                    │ HTTP probes,
                                    │ image fetches,
                                    │ Playwright JS-render
                                    ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  backend (FastAPI, asyncio)                                        │
   │                                                                    │
   │  ┌──────────┐    ┌──────────┐    ┌──────────────┐                  │
   │  │ Scheduler│───▶│ Check.run│───▶│ CheckResult  │                  │
   │  │ (asyncio │    │ (any of  │    │ envelope     │                  │
   │  │  task    │    │  36 imp- │    │              │                  │
   │  │  per     │    │  lement- │    └──────┬───────┘                  │
   │  │  check)  │    │  ations) │           │                          │
   │  └──────────┘    └──────────┘           ▼                          │
   │       │                          ┌────────────┐                    │
   │       │                          │ AlarmEngine│                    │
   │       │                          │ (open/close│                    │
   │       │                          │  +route +  │                    │
   │       │                          │  dispatch) │                    │
   │       ▼                          └─────┬──────┘                    │
   │  ┌──────────┐                          │                           │
   │  │  Store   │◀─────────── persist runs ┘ + alarms                  │
   │  │ (asyncpg │                                                      │
   │  │  pool)   │       ┌──────────────────┐                           │
   │  └────┬─────┘       │ ConnectionManager│                           │
   │       │             │ (WebSocket fan-  │                           │
   │       │             │  out, transition-│                           │
   │       │             │  only events)    │                           │
   │       │             └─────────┬────────┘                           │
   │       │                       │                                    │
   │       │           ┌───────────┴────────────┐                       │
   │       ▼           ▼                        ▼                       │
   │  ┌──────────────────────────────────────────────┐                  │
   │  │   FastAPI routes  /api/{status, alarms,      │                  │
   │  │     timeline, history, upstream, auth,       │                  │
   │  │     admin/*, ws, _debug/stats}               │                  │
   │  └────────────────────────┬─────────────────────┘                  │
   └───────────────────────────┼────────────────────────────────────────┘
                               │ HTTP + WebSocket
                               ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │  frontend (SvelteKit + Svelte 5)                                   │
   │                                                                    │
   │   stores/state.svelte.ts  ◀──── REST poll (5s) + WS push           │
   │       │  (rollup, alarms, metrics, dedupe & microtask coalesce)    │
   │       ▼                                                            │
   │   routes/+page.svelte (Live: map + radars + products + alarms)     │
   │   routes/timeline/+page.svelte (state-over-time grid + cell        │
   │     detail panel with explainRun + LazyImage)                      │
   │   routes/history/+page.svelte (filtered alarms + check_runs)       │
   │   routes/admin/* (auth-gated: users, email, alerts, silences,      │
   │     audit)                                                         │
   │                                                                    │
   │   lib/origin.ts: API_BASE detection + global fetch monkey-patch    │
   │     (attaches Authorization: Bearer header from localStorage)      │
   └────────────────────────────────────────────────────────────────────┘
```

---

## The single most important pattern: Check → CheckResult

Every probe in the system — site-alive HTTP ping, per-radar reconciliation,
image-stats heuristic, anything future — implements the same one-method
interface:

```python
# backend/checks/base.py
class Check:
    id:         str               # unique key, e.g. "layer1.product.qpe_1hr"
    target:     str               # what this check is about, e.g. "qpe_1hr"
    stage:      str               # "L0" | "L1" | "L2" | "L3" | "L4-T1T2"
    cadence_s:  int               # seconds between runs
    depends_on: list[str] = []    # other check ids — used for alarm suppression

    async def run(self, ctx: CheckContext) -> CheckResult: ...

@dataclass
class CheckResult:
    check_id, target, stage, status   # status: pass|warn|fail|skip|error
    started_at, finished_at, summary
    payload:  dict = {}      # arbitrary diagnostic JSON
    metrics:  dict = {}      # numeric time-series (sparkline data)
    artifacts: list = []
```

`CheckContext` is the injected transport bundle (HTTP client, Playwright
browser, asyncpg pool, network monitor). One context per process,
passed to every `run()`.

Adding a new check = subclass `Check`, implement `run()`, decorate with
`@register` (see existing `backend/checks/layer*_*.py` for templates).
The scheduler auto-discovers anything registered by import-time.

---

## The lifecycle of one check

Trace it through the codebase by following a single `layer1.product.qpe_1hr`
check from radarca → your browser:

1. **Schedule** (`backend/scheduler.py`)
   - On startup, one `asyncio.Task` per registered check.
   - Each task loop: `await check.run(ctx)` → `await store.write_check_run(result)`
     → `await engine.evaluate(result)` → `on_result(result)` (broadcasts WS event) →
     `sleep(cadence - elapsed)`.
   - Errors inside the check become a `status="error"` `CheckResult` rather
     than crashing the loop.
   - If `NetworkMonitor` says local connectivity is down, `fail`/`error`
     gets downgraded to `skip` so we don't fire false alarms.

2. **Run** (`backend/checks/layer1_product.py`)
   - Fetches `productDetail` JSON via `ctx.http`.
   - Computes 8 sub-checks (`A_api_up`, `B_schema`, …, `H_image_hash`).
   - Folds them into one overall status via `worst_of(*sub_status.values())`.
   - Returns a `CheckResult` with metrics like `age_s` and `image_bytes`
     for the dashboard sparklines.

3. **Persist** (`backend/db/store.py`)
   - One row in `check_runs` (with `payload JSONB` for full detail).
   - One row per metric in `metric_samples` (time-series).
   - On Sentinel startup, `Store.connect()` applies `schema.sql` idempotently
     — no separate migration step.

4. **Evaluate alarms** (`backend/alarms/engine.py`)
   - First time a check goes non-pass → open an `alarms` row.
   - Transitions back to pass → close it.
   - Routes through the configured `Router` (rule = matchers in the
     loaded `AlertsConfig`) to receiver(s).
   - Dispatcher emits to console / email / webhook based on receiver type.
   - Suppresses alarms whose `depends_on` chain has an upstream alarm
     already open (e.g., don't fire L1 alarms if L0 is down).

5. **Broadcast** (`backend/api/app.py`)
   - The scheduler's `on_result` callback is wired to `ws.broadcast(...)`.
   - **Only transitions** are broadcast (`prev_status != new_status`).
     On a steady-state idle system this drops WS volume from
     ~30 events/min to near zero — the fix that stopped the recurring
     "tab freezes after 10-15 min" issue.

6. **Frontend store** (`frontend/src/lib/stores/state.svelte.ts`)
   - On startup: REST `GET /api/status` populates `rollup` + `GET /api/alarms`
     populates `alarms` + WebSocket subscription opens.
   - WS run events flow through `handleWs` → `mergeRun` → `pendingMerge`
     map → microtask `flushMerge` (coalesces bursts into one atomic
     rollup replacement).
   - Polling refresh fires every 5s as a backstop (JSON-fingerprint
     dedupes when nothing changed).
   - Visibility-gated: hidden tabs drop WS mutations + pause polling.

7. **Reactive UI** (`frontend/src/routes/+page.svelte`)
   - `$derived(sentinel.rollup?.stages?.L1 ?? [])` filters into product rows.
   - Each row shows a sparkline + status dot. The dot pulses on status
     transition (via `pulseTick` map).
   - User clicks "ack" on an alarm → `api.ack(id)` → `POST /api/alarms/{id}/ack`
     with Bearer token → `require_user` dep checks auth → audit log
     written → alarm row re-renders with the "acked" badge.

That's the whole loop. Everything else is variations on this pattern.

---

## Why some things look the way they do

### The frontend doesn't talk to the backend on the same origin in production

We chose plain HTTP on LAN with no reverse proxy by default. That means
`http://host:3000` for the frontend and `http://host:8000` for the
backend. Cross-origin. Browsers won't send cookies cross-origin without
HTTPS + `SameSite=None; Secure`. So **auth is Bearer-token, not cookie**.
The flow:

1. POST `/api/auth/login` returns `{token, user}` (token = session UUID).
2. Frontend stores token in `localStorage`.
3. `installFetchPrefix()` monkey-patches `window.fetch` to attach
   `Authorization: Bearer <token>` to every `/api/*` request and to
   prepend `API_BASE` when needed.
4. WebSocket can't set custom headers, so the token rides on a
   `?token=...` query param.

The cookie path is still maintained server-side so dev (Vite proxy,
same origin) just works, but production never uses it.

### `lib/origin.ts` has a "port is exactly 3000?" check

That's how we detect "direct docker compose, no proxy" vs. "behind a
reverse proxy". Direct compose binds frontend on 3000 and backend on
8000 separately; behind a proxy everything lives at one origin and
`/api/*` is routed internally. The frontend uses one heuristic to
serve both:

- port 3000 → `API_BASE = http://<hostname>:8000`
- everything else → `API_BASE = ''` (relative URLs; the proxy routes them)

Override with `PUBLIC_SENTINEL_API_BASE` if you need something
specific.

### The check registry is import-side-effect-driven

`backend/checks/__init__.py` does nothing but `from . import …` for
each module. Each module ends with `register(MyCheck(...))` calls.
`backend/main.py` imports `backend.checks` early in startup, which
triggers all those registrations. After that, `CHECKS` in
`backend/registry.py` has all 37 entries.

The pattern is dumb-but-explicit on purpose: adding a check means
either editing an existing module or creating a new module and adding
one `from . import …` line.

### `alerts.yaml` is the disk fallback, the DB row wins

`backend/alarms/models.py:load_config_from_db_or_yaml()` checks the
`settings` table for a row with key `alerts_config` first. If found,
that JSON is parsed as the `AlertsConfig`. Otherwise the file
`alerts.yaml` at the project root is used.

This means a fresh deploy reads alerts.yaml; once you save anything
via `/admin/alerts`, the DB row wins forever. The YAML stays around
as the initial template / fallback.

### Image archive lives both in Postgres and on disk

`image_archive` table: one row per unique `sha256` (deduped — many
captures hash-identically).
`image_index` table: maps `source` path → `sha256` for fast lookup
when serving images.
`data/archive/<sha[:2]>/<sha>.png`: the bytes on disk
(mounted as `sentinel_archive` named volume in prod).

The `/api/upstream/image_by_source.png` endpoint tries the local
archive first, falls back to upstream radarca. That's how the
timeline cell-detail panel keeps showing images even after radarca
rotates its ~2h window.

### The scheduler doesn't restart on config changes

`AlertsConfig` is loaded once at startup. To pick up changes you'd
have to restart. **Except for routing config**: the
`/api/admin/alerts` PUT endpoint calls `engine.reload(new_cfg)` which
atomically swaps `router` + `sinks` in-place. No restart needed for
alert routing changes.

If you add more dynamic config (e.g., per-check cadence tweaking),
follow the same pattern: a `reload()` method on the owning
component, called from the admin endpoint after the DB write.

---

## What's NOT in the codebase (deliberately)

- **An ORM**. Direct asyncpg + hand-rolled SQL throughout. We have
  ~10 tables and queries are simple; an ORM would be more rope.
- **A migration tool**. `schema.sql` is idempotent; that's the
  contract. If you need destructive changes, write a one-off script.
- **Automated tests** beyond `validation_tests/` (which validate
  upstream characterization, not our code). This is the biggest
  technical debt — the next maintainer should add Playwright e2e
  tests for at least the auth + ack + admin flows.
- **A logging framework**. Python's stdlib `logging` is configured in
  `backend/main.py`; that's it. Docker captures stdout.
- **Metrics export** (Prometheus etc.). `/api/_debug/stats` is the
  closest thing. If you stand up an observability stack later, the
  values to expose are there already.

---

## Stack at a glance

| Layer | Tech | Notes |
|---|---|---|
| DB | Postgres 16 | One database; ~16 tables. Uses pgcrypto for UUIDs. |
| Backend | Python 3.12, FastAPI, uvicorn | asyncio throughout. asyncpg, httpx, Playwright. |
| Image processing | Pillow + numpy + imagehash | CPU work runs in `asyncio.to_thread`. |
| Email | aiosmtplib | Async SMTP; config via `/admin/email`. |
| Auth | argon2-cffi + UUID sessions | Bearer-token in prod, cookie supported in dev. |
| Frontend | SvelteKit 2 + Svelte 5 (runes) | adapter-node in prod, vite dev in dev. |
| Styling | Tailwind v4 | The `@theme` block in `app.css` defines tokens. |
| Map | MapLibre GL JS | Stadia Maps for basemap (free tier; domain-allowlisted). |
| Build / deploy | Docker + docker compose | Two Dockerfiles in `ops/`. |
| Reverse proxy (optional) | Traefik (or anything) | Frontend at `/`, backend at `/api/*`, WS at `/api/ws`. |
