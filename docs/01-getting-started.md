# Getting started

This page walks you from "nothing" to a running Sentinel pointed at
[radarca.engr.colostate.edu](https://radarca.engr.colostate.edu). Plan
on ~30 minutes for a first install, most of it waiting for image
pulls and the backend's first scheduling tick.

The framing is **radarca-specific** because that's what Sentinel was
built to monitor. If you want to point it at a different HTTP-served
upstream, the structural changes (product list, URL conventions) live
in `backend/config.py` — `02-deployment.md` covers that path.

---

## 1. Prerequisites

You'll need:

- A Linux host that stays online. An LXC container, a small VM, a NUC,
  a Raspberry Pi 5 — anything with Docker Engine 24+ and the
  `docker compose` v2 CLI plugin. The reference deployment runs on an
  LXC with 2 vCPU / 4 GB RAM / 30 GB disk and rarely tops 1 GB resident.
- **Disk planning**: Postgres state and the captured-image archive
  live in named Docker volumes. Sentinel writes one PNG per L4 image
  check; current capture rate works out to roughly 600 MB/day worst
  case, mostly compressible. 30 GB gives you ~50 days of history
  before you start thinking about pruning.
- **Outbound network access** to:
    - `ghcr.io` — to pull the published container images.
    - `radarca.engr.colostate.edu` — the upstream Sentinel monitors.
    - `tiles.stadiamaps.com` — basemap tiles for the dashboard map.
- (Optional) A domain name + a reverse proxy in front. Caddy, nginx,
  and Traefik all work; the only requirement is forwarding the
  WebSocket upgrade headers. Without TLS the stack still works fine
  over plain HTTP on a LAN — Bearer-token auth is used precisely so
  cross-origin cookies aren't a problem.
- (Optional) An SMTP relay if you want email alerts. Web Push
  notifications need nothing external — VAPID keys generate on first
  start and persist in Postgres.

---

## 2. Pull the deploy artifacts

You don't need to clone the whole repository. Two files are enough:
the GHCR-pull compose file and the example env. From an empty
directory on the host:

```bash
mkdir -p /srv/sentinel/ops && cd /srv/sentinel
curl -fsSL https://raw.githubusercontent.com/jkmesches/AQPI-Sentinel/main/ops/docker-compose.ghcr.yml \
    -o ops/docker-compose.ghcr.yml
curl -fsSL https://raw.githubusercontent.com/jkmesches/AQPI-Sentinel/main/ops/.env.prod.example \
    -o ops/.env.prod
```

The `compose` file references images at
`ghcr.io/jkmesches/sentinel-{backend,frontend}` and parameterizes the
tag via `SENTINEL_TAG`. The env file is annotated — every variable
has a comment explaining what it controls.

> **GHCR auth.** While the images are private, a one-time login is
> required. Generate a personal access token at
> [github.com/settings/tokens](https://github.com/settings/tokens) with
> the `read:packages` scope, then:
>
> ```bash
> echo "$GHCR_PAT" | docker login ghcr.io -u <your-github-username> --password-stdin
> ```
>
> Once the project owner flips the packages to public from the GitHub
> package settings page, this step disappears.

---

## 3. Fill in `.env.prod`

Open `ops/.env.prod` and set, at minimum:

```bash
# A strong random string — this becomes the Postgres password and
# is baked into the database container on first start. Choose well;
# changing it later means a Postgres restart with a custom env.
POSTGRES_PASSWORD=<48+ random characters>

# Bootstrap admin. These are ONLY read on first startup when the
# `users` table is empty — once any user exists, Sentinel ignores
# them. Use a strong password; you'll log in with this email.
SENTINEL_ADMIN_EMAIL=you@example.com
SENTINEL_ADMIN_PASSWORD=<your password>
SENTINEL_ADMIN_DISPLAY_NAME=Your Name

# Public-facing URL embedded in alert emails ("Open in dashboard"
# button + captured-image links). Leave blank to omit those links.
SENTINEL_PUBLIC_URL=https://sentinel.your-domain.example
```

Everything else has sensible defaults — `SENTINEL_BASE` defaults to
`https://radarca.engr.colostate.edu` which is exactly what you want
for the radarca install.

---

## 4. Start the stack

Pin to the latest released version (`v0.1.0` at the time of writing)
so you have a known-good starting point:

```bash
SENTINEL_TAG=0.1.0 docker compose -f ops/docker-compose.ghcr.yml \
    --env-file ops/.env.prod pull
SENTINEL_TAG=0.1.0 docker compose -f ops/docker-compose.ghcr.yml \
    --env-file ops/.env.prod up -d
```

`SENTINEL_TAG=latest` tracks `main` — fine for staging, riskier for
production since any push to `main` redeploys silently on the next
pull. Stick with pinned versions.

Three containers come up: `sentinel-postgres`, `sentinel-backend`,
`sentinel-frontend`. The backend's `Store.connect()` applies
`schema.sql` idempotently on first start, so no manual migration
step is required. Watch the backend come online:

```bash
docker logs -f sentinel-backend
```

When you see `scheduler started with 44 checks (max topological rank 2)`
the first wave of probes is firing — that's typically 5-10 seconds
after the container starts.

---

## 5. Sign in

Open the frontend in a browser:

- `http://<host>:3000` if you went direct.
- `https://<your-domain>` if your reverse proxy is in front.

You'll get a login screen. Use the bootstrap-admin email and password
from your env file. After that the admin env vars are dead weight —
delete them or leave them; Sentinel ignores them once a user exists.

A guided tour, top to bottom:

- **Stage strip** (top of every page) — one colored dot + count per
  monitoring stage. Connectivity / Product Freshness / Radar Scans /
  Map Overlays / Image Quality. Dot turns gray (skip) when an upstream
  failure is cascade-suppressing the whole stage.
- **Left rail** — Site (L0 connectivity) on top, Radars (L2) below.
  Each row has a status dot, a sparkline of recent data-arrival rate,
  and a one-line summary.
- **Center hero** — Network map with radar pins. Click to focus, drag
  the time chip strip to scrub through historical composite imagery.
- **Right rail** — Products grouped by category (Radar Data /
  Atmospheric Forecast / CoSMoS / NWM).
- **Alarms ticker** at the bottom — most-recent open alarms, click to
  drill in.

The mobile shell at `/m` is fully featured: install it to your home
screen on iPhone Safari (Share → Add to Home Screen) or tap the
"Install app" button that appears on Android Chrome. Push
notifications + the same data, all touch-optimized.

---

## 6. Five-minute config

A few one-time touches that turn a working install into a useful one:

1. **Configure SMTP** at `/admin/email` if you want email alerts.
   The settings persist in the database; restart not required. If
   you skip this, Sentinel still writes alarms to the timeline and
   sends Web Push notifications — email is purely additive.
2. **Confirm the public URL** — if `SENTINEL_PUBLIC_URL` was blank
   when the stack started, set it in admin settings. Otherwise alert
   emails will omit the "Open in dashboard" link.
3. **Install the mobile app on a phone** + enable push from `/m/more`.
   Have at least one device subscribed so test alerts have somewhere
   to go.
4. **Send a test alert** from `/admin/alerts` → pick any routing
   rule → "Send test alert". This exercises the full
   recipient-resolve → group-schedule → email + push pipeline, end
   to end. Two-minute confidence check.
5. **Skim the threshold registry** at `/admin/thresholds`. The
   defaults are conservative; for radarca specifically the values
   in the table are what's currently in production. Edit + save
   gives you a diff confirmation modal.

---

## 7. Verify everything's healthy

Sentinel exposes its own health probe:

```bash
curl -s http://<host>:8000/api/_debug/stats | python3 -m json.tool
```

What to look for:

- `event_loop_lag_ms` should be sub-millisecond. Anything over 50ms
  means the backend is heavily loaded.
- `asyncpg.in_use` near `max_size` means the pool is saturated —
  fine in bursts, suspicious if sustained.
- `httpx.in_flight` is the count of outbound requests to radarca
  currently in flight. Sub-1 average is normal.
- `scheduler.tasks_running` should equal `tasks_total` (every check
  has a live asyncio task).
- `network_monitor.online` and `network_monitor.dns_ok` are the
  bedrock of cascade-demote — if either flips false, every
  downstream check demotes to skip.

The auto-generated API reference lives at `/api/docs` (Swagger UI)
or `/api/redoc` (ReDoc, denser layout). Every endpoint is documented
with its parameters, request/response shapes, and auth requirement.

A first-paint dashboard with **mostly-green** rows means you're done.
A solid-green stack with no alarms in the ticker = upstream radarca is
healthy and Sentinel is happily watching it.

---

## Where to go from here

- **Full deployment story** — TLS, reverse proxy configs, scaling
  knobs, off-host backups, monitoring Sentinel itself: see
  `02-deployment.md`.
- **Configuring alerts, groups, users, thresholds, silences, push
  routing**: see `03-administration.md`.
- **Day-2 operations** — log inspection, backups, rolling back to a
  previous version, performance tuning: see `04-maintenance.md`.
- **Extending Sentinel** — adding a new check, an API endpoint, a
  UI page, a custom alarm sink: the `13–16-extending-*.md` series.
- **Stuck on something?** — try `05-troubleshooting.md` first, then
  open a GitHub issue with what you see in `/api/_debug/stats`.

> **Adapting for non-radarca upstreams.** Most of Sentinel's
> structure is upstream-agnostic — it's an HTTP-monitoring kit with
> radar-specific opinions. The check inventory in `backend/checks/`
> and the product / radar inventory in `backend/config.py` are the
> two main places to teach Sentinel about a different system. The
> "Adapting Sentinel for other systems" section of `02-deployment.md`
> walks through the change set.
