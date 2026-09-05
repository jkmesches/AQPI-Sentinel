# Environment variable reference

Every `SENTINEL_*` and `POSTGRES_*` env var Sentinel reads, in one
table. For the narrative version with "when you'd change it"
guidance, see [`02-deployment.md` § Environment
variables](02-deployment.md#4-environment-variables).

Canonical source is `ops/.env.prod.example` — if this table drifts
from that file, the file wins.

---

## Postgres

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `POSTGRES_USER` | no | `sentinel` | Database role. |
| `POSTGRES_PASSWORD` | **yes** | — | DB password. Compose fails fast without it. Use 48+ random chars. |
| `POSTGRES_DB` | no | `sentinel` | Database name. |

The first compose start writes these into the Postgres container.
**Changing `POSTGRES_PASSWORD` after first start** requires either
restoring from backup with the new password or rotating it manually
inside Postgres.

---

## Backend runtime

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SENTINEL_DB_URL` | no | computed from `POSTGRES_*` | asyncpg connection string. Override only for non-standard setups (external DB, custom auth). |
| `SENTINEL_BASE` | no | `https://radarca.engr.colostate.edu` | The monitored upstream's root URL. |
| `SENTINEL_API_HOST` | no | `0.0.0.0` | Listen address inside the container. |
| `SENTINEL_API_PORT` | no | `8000` | Listen port inside the container. The compose `ports:` block maps this to the host. |
| `SENTINEL_LOG_LEVEL` | no | `INFO` | Python log level. `DEBUG` is loud (every probe). |
| `SENTINEL_ARCHIVE_ENABLED` | no | `1` | Persist L4 captured images. `0` to disable. |
| `SENTINEL_ARCHIVE_ROOT` | no | `/data/archive` | Mount path. Volume-mapped from `sentinel_archive`. |
| `SENTINEL_ARCHIVE_RETENTION_DAYS` | no | (blank = permanent) | Delete archived PNGs older than N days. |
| `SENTINEL_PREWARM_ENABLED` | no | `0` | Continuously capture **every** published moment and tilt, rather than only what someone has looked at. See the warning below before enabling. |
| `SENTINEL_PREWARM_INTERVAL_S` | no | `300` | Seconds between sweeps of all streams. Must stay below the origin's rolling-window length or frames are missed permanently — radar-display keeps 7 frames at ~140 s (~16 min). |
| `SENTINEL_PREWARM_CONCURRENCY` | no | `3` | Simultaneous in-flight prewarm fetches, bounded independently of operator traffic. |

!!! warning "`SENTINEL_PREWARM_ENABLED` is the only setting that creates upstream load nobody asked for"
    Every other request Sentinel makes is a scheduled check or an operator
    looking at something. Prewarm is neither, so it ships off.

    Measured on the reference deployment (2026-09-05), enabling it takes the
    archive from **~4,000 images/day to ~49,000**, and disk from ~280 MB/day
    to **~1.1 GB/day** — about 34 GB/month, or 12 years on a 4.9 TB share. The
    request load is roughly 8/min against radarca and 33/min against
    radar-display.

    Both are someone else's production systems, and radarca is slow even when
    idle (p50 3.4 s, p90 8.3 s). Prewarm reuses the same LRU, archive and
    single-flight as operator traffic, so a stream it already holds costs
    nothing — steady-state cost is one fetch per genuinely new frame, not one
    per sweep. Watch `prewarm.fetched` vs `prewarm.already_had` in
    `/api/_debug/stats`: a sweep that is mostly `fetched` means the interval
    is too long for the origin's window and frames are being lost.

---

## Admin bootstrap

Read on first start when the `users` table is empty. **Ignored
afterwards.**

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SENTINEL_ADMIN_EMAIL` | first-start only | — | First admin's email. |
| `SENTINEL_ADMIN_PASSWORD` | first-start only | — | First admin's password. |
| `SENTINEL_ADMIN_DISPLAY_NAME` | no | — | Shown in the admin UI. |

---

## Public URL

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SENTINEL_PUBLIC_URL` | no | — | URL users actually visit. Embedded in alert emails. Blank = no "Open in dashboard" link in emails. |

This is **not** a listen address. It's purely the URL operators
see when an email lands.

---

## Ports (host-side mapping)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `BACKEND_PORT` | no | `8000` | Host-side port mapped to the backend container. |
| `FRONTEND_PORT` | no | `3000` | Host-side port mapped to the frontend container. |

To bind to localhost only (recommended when a reverse proxy is in
front), edit `ops/docker-compose.ghcr.yml`'s `ports:` block:

```yaml
ports:
  - "127.0.0.1:${BACKEND_PORT:-8000}:8000"
```

---

## Frontend build args

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `PUBLIC_SENTINEL_API_BASE` | no | blank → resolve at runtime from `window.location.hostname:8000` | Bake a backend origin into the frontend image. Set when the frontend and backend live at different origins. |

This is consumed at **image build time** (frontend Dockerfile build
arg), not at container runtime. To change it on an existing image,
rebuild the frontend with `docker compose build frontend`.

---

## GHCR pull (compose-time)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SENTINEL_TAG` | no | `latest` | Which image tag to pull from GHCR. Pin to `vX.Y.Z` for production. |

Used by `ops/docker-compose.ghcr.yml`. Has no effect with the
source-build compose (`docker-compose.prod.yml`).

---

## Database location overrides

If you're running Postgres **outside** the compose stack (managed
service, separate VM), the backend connects via `SENTINEL_DB_URL`
only — the `POSTGRES_*` vars are unused. Set:

```bash
SENTINEL_DB_URL=postgresql://user:pass@external-host:5432/sentinel
```

The backend's `Store.connect()` applies `schema.sql` on every
start, so an empty external database will be initialized
automatically.

---

## Diagnostic / debug flags

These don't normally need to be set — included for completeness.

| Variable | Default | Purpose |
|---|---|---|
| `SENTINEL_TLS_DAYS_FLOOR` | `30` | Warn when TLS cert has less than this many days left. |
| `SENTINEL_HTTPX_TIMEOUT_S` | `12` | Total timeout for outbound HTTP probes. |

---

## What's NOT an env var

Some configuration deliberately lives in code or DB, not env:

- **Detection thresholds** — at `settings.thresholds` (jsonb), edited
  via `/admin/thresholds`. Override `config.py` defaults are the only
  knob, not env-var-controlled.
- **Routing rules + recipients** — at `settings.alerts_config` (jsonb)
  or `alerts.yaml`, edited via `/admin/alerts`.
- **SMTP credentials** — at `settings.smtp` (jsonb), edited via
  `/admin/email`. (Or `alerts.yaml`'s `smtp:` block as fallback.)
- **VAPID keys** — auto-generated at `settings.vapid_keys`.
- **Group definitions** — `groups` + `group_members` tables.

The pattern: anything an operator might change after deploy lives
in the database with an admin UI. Env vars are reserved for
deploy-time concerns (where to listen, where the DB is, what URL
to embed in emails).
