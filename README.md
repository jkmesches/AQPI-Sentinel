# Sentinel

> Continuous, hierarchical health monitoring for **[radarca.engr.colostate.edu](https://radarca.engr.colostate.edu/public)** — the CSU-CHILL X-band radar network dashboard.

Sentinel watches the upstream dashboard from five complementary angles, surfaces anomalies as alarms with email + Web Push + console + webhook escalation, and exposes everything through a CSU-themed operations console (desktop) plus an iPhone-native mobile companion (`/m/*`).

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  L0  Connectivity         site / TLS / origin / public dashboard │
   │  L1  Product Freshness    per-product freshness · image · parity │
   │                           (timestamp OR step-index contiguity)   │
   │  L2  Radar Scans          per-radar reconciliation + GHOST_UP    │
   │                           detection via newest-filename ts       │
   │  L3  Map Overlays         JS overlay timestamp parity (Playwright)│
   │  L4  Image Quality        image stats + tier-2 heuristics        │
   │                           (extreme · speckle · ring · frozen)    │
   └──────────────────────────────────────────────────────────────────┘
                  │                            │
                  ▼                            ▼
              alarm engine                  WebSocket
            (routes, groups,                   │
             escalation steps,                 ▼
             ack/silences,                SvelteKit dashboard
             group expansion +            (Live · Timeline ·
             per-step email                History · Admin)
             dedup)
                  │
                  ▼
         email / Web Push / webhook / console
         (each with per-device routing,
          severity floor, quiet hours,
          on-duty schedules)
```

This repo holds the entire stack — Postgres schema, Python backend (FastAPI + scheduler + alarm engine + Playwright + image processing + threshold registry + retroactive reprocess engine), SvelteKit frontend (desktop + mobile PWA), dev scripts, ops Dockerfiles + compose, and the planning/characterization docs that describe radarca's API surface and the monitoring strategy.

---

## Status

Shipped and running 24×7 against live radarca data at `https://aqpi.local.shirejoe.com`.

- **38 checks** across 5 stages (L0 / L1 / L2 / L3 / L4-T1T2), self-registered via `@register`.
- **Postgres 16** schema — 18 tables: check_runs / metric_samples / alarms / alarm_acks / silences / image_archive / image_index / image_observations / users / sessions / password_reset_tokens / admin_audit / push_subscriptions / groups / group_members / settings / + image-stats helpers. Full DDL in `backend/db/schema.sql`, idempotent + auto-applied on startup.
- **Alarm engine** with routes + receivers + escalation policies + acks + silences + suppression DAG. Receivers carry `group_ids` lists; dispatch expands active groups (schedule-gated) into member emails and dedupes overlap so a person reachable through both a group and a direct recipient gets one message, not two.
- **Auth** — argon2id passwords, UUID Bearer tokens, admin/user roles, invite via email-able reset link, audit log of every admin action.
- **Admin surface** (`/admin/*`):
  - `/admin/thresholds` — table editor for every detection knob (per-product freshness, per-radar ghost-up timeout, L4 image-QC parameters, global tolerances). Diff-confirmation on save. In-process cache reloads atomically; next check tick reads new values. **Retroactive reprocess** panel walks `check_runs` in a chosen window and re-classifies under current thresholds — cancellable, with live progress.
  - `/admin/groups` — bundles of users with notification schedules (always / weekly / biweekly with anchor). Multiple time windows, one-off downtime, recurring quiet hours (overnight wrap supported), parent-group inheritance.
  - `/admin/alerts` — recipient table with chip-based group multi-select, dropdown routing-rule matchers (populated from the registry), custom-matcher dropdown editor, escalation steps with direct group references.
  - `/admin/silences` — preset matchers (All / All Radars / All Products / All Website) + custom form, UTC↔Local toggle, edit existing.
  - `/admin/email`, `/admin/users`, `/admin/audit`.
- **Frontend** — CSU-themed dark/light dashboard with descriptor stage names (Connectivity / Product Freshness / Radar Scans / Map Overlays / Image Quality), filled-pie status icons, multi-select chip filters on /history, drill-down modal with reasoning trail + captured L4 image, Export Report CSV from /timeline, programmatic map rotation (northernmost radar pins top-left), 10-product composite dropdown grouped by source, NEXRAD playback with prefetched blob cache (flicker-free).
- **Mobile site** (`/m/*`) — PWA-installable on iPhone Safari, 4-tab bottom nav (Status · Timeline · Alarms · More), MobileDrillDown with deep verify-yourself surfaces, composite picker + play/scrub on the mobile map, ack/unack on alarms, dedicated `/m/push-settings/edit/[id]` editor for per-device push routing.
- **Web Push** — per-device routing config: severity floor, product-pattern matching, async delay, on-duty schedule (same shape as groups schedules). Silence-aware. iOS 16.4+ install-before-push respected.
- **Production deploy** — Docker compose on a home-cluster LXC, fronted by user-owned Traefik with TLS. Backup + restore recipes in `docs/MAINTENANCE.md`. `pg_dump` snapshot + image-archive tarball cover the full state.

**Roadmap** (deferred and documented in `docs/MAINTENANCE.md` / memory):
1. L4 Tier 3 — cross-radar consistency check (X-band vs NEXRAD over overlap regions). Next functional slice.
2. L4 Tier 4–5 — dual-pol consistency + learned "weird artifact this isn't normal" classifier.
3. L3A overlay parity beyond `fcst_total_precip` (validates radarca's JS overlay timestamps for more than one product).
4. L4 range_ring per-radar center config (low priority).
5. Durable push delay scheduling (currently in-memory only).
6. Threshold-update WS broadcast for live admin tab refresh.

---

## Quickstart

Prereqs: **Docker** (for Postgres), **Python 3.12**, **Node 20+**.

```bash
git clone <this-repo> sentinel
cd sentinel

# 1. backend deps + Playwright Chromium (~250 MB one-time)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium

# 2. frontend deps
( cd frontend && npm install )

# 3. dev env
cp .env.example .env

# 4. everything else from here is `make`
make dev
```

After ~10 s you'll see:

```
sentinel up
  backend  : http://127.0.0.1:8000  (log: /tmp/sentinel-be.log)
  frontend : http://127.0.0.1:5173  (log: /tmp/sentinel-fe.log)
```

Open `http://localhost:5173`. The first wave of checks fires within 1 second and the dashboard populates immediately with real upstream conditions — at the time of writing, two of the X-band radars typically run in a `WARN`/`GHOST_UP` state, and several forecast products move on multi-hour cadences. Those are real upstream conditions, not Sentinel bugs.

First admin user: set `SENTINEL_ADMIN_EMAIL` + `SENTINEL_ADMIN_PASSWORD` in `.env`. On a fresh `users` table the backend bootstraps them automatically on first start. Already running and locked out? See `docs/MAINTENANCE.md` § "Create / disable a user".

### Dev command reference

```bash
make dev          # postgres + backend + frontend
make status       # ●/○ per process + endpoints
make logs         # tail backend + frontend, color-prefixed
make stop         # stop be + fe (postgres stays up)
make restart      # restart be + fe
make restart-fe   # vite-only: kills it, clears .svelte-kit/ + .vite/, restarts
make restart-be   # backend-only
make pg-shell     # psql straight into the dev database
make pg-stop      # stop the Postgres container
```

`make restart-fe` is the one you'll reach for most. Vite's HMR drifts out of sync after structural Svelte edits and the browser starts seeing `Loading failed for the module …` errors; `make restart-fe` wipes the dep cache + generated SvelteKit tsconfig and resolves it in ~6 s. Hard-refresh the tab afterward.

`make restart-be` for any backend change. Schema changes are picked up automatically — `Store.connect()` re-applies `schema.sql` idempotently on every backend startup.

### Production deploy

Single command after rsync. The full recipe lives in `docs/MAINTENANCE.md` § "Deploy a code update":

```bash
rsync -avz --delete \
  --exclude='.git/' --exclude='.venv/' \
  --exclude='frontend/node_modules/' --exclude='frontend/.svelte-kit/' \
  --exclude='frontend/build/' \
  --exclude='data/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='.env' --exclude='ops/.env.prod' \
  ./ aqpisentinel:/srv/sentinel/

ssh aqpisentinel 'cd /srv/sentinel && \
  docker compose -f ops/docker-compose.prod.yml --env-file ops/.env.prod \
  up -d --build backend frontend'
```

The `--exclude='ops/.env.prod'` is **load-bearing** — that file holds the postgres password and is gitignored locally. See "Common pitfalls" in `MAINTENANCE.md`.

---

## How it works

### Architecture (extensibility-first)

```
backend/
  registry.py            # @register populates a global dict; nothing else
                         # knows what checks exist
  scheduler.py           # one asyncio task per registered check. Two
                         # DNS-flake demote paths (raw exception via
                         # _is_local_dns_error, in-check-catch summary
                         # via _maybe_downgrade_for_dns_summary).
  thresholds.py          # admin-managed knob registry — single jsonb
                         # blob at settings.thresholds, in-process cache,
                         # three-tier fallback (DB → config.py → default).
  stages.py              # canonical L0..L4-T1T2 → descriptor map for
                         # backend-rendered text (emails, push titles).
  groups.py              # notification schedule evaluator —
                         # always / weekly / biweekly + downtime +
                         # recurring downtime, parent-chain AND-merge.
  reprocess_engine.py    # retroactive reprocess job machinery
                         # (_reverdict_l1/l2/l4 + cancellable progress).
  push.py                # Web Push dispatch with per-device filters
                         # (severity / pattern / schedule / async delay).
  checks/
    base.py              # Check, CheckContext, CheckResult — universal envelope
    helpers.py           # parse_filename_ts + parse_filename_step_idx,
                         # worst_of, status rank, derive_check_cadence
    transports/          # http / browser today; ssh/fs/s3/snmp drop in here
    layer0_website.py    # one file per layer; @register at the bottom
    layer0_network.py    # local-network blame shield
    layer1_product.py    #   ↳ parameterized — registers one Check per product
                         #     Reads thresholds.* for every knob.
                         #     L3B parity fold-in: timestamp OR step-index.
    layer1_vector.py     # static vector overlays (flowlines, watersheds)
    layer1_stream.py     # stream-gauge feeds (NWM Stream Reach)
    layer2_radar.py      # per-radar reconciliation; silent_fail_s +
                         # hysteresis read live from thresholds.
    layer3_overlay.py    # Playwright-driven JS overlay parity
    layer4_image.py      # image-QC tier 1+2; routes through
                         # thresholds.get_l4 for extreme / frozen /
                         # range_ring profile knobs.
  alarms/
    engine.py            # state machine + ticker. _expand_receiver_groups
                         # for schedule gate + group expansion. Per-step
                         # email dedup across receivers.
    models.py            # Receiver(group_ids), EscalationStep(group_ids),
                         # AlertsConfig, Route, Condition, Silence
    router.py            # routes → policy + escalation
    suppression.py       # walk depends_on graph
    conditions.py        # time_of_day_in, duration_at_severity_min, etc.
    silences.py
    sinks/               # console / email / webhook (pluggable)
    templates/           # Jinja2 plain-text + HTML email (descriptor
                         # subject, technical L# in footer)
  api/
    app.py               # FastAPI app + lifespan (thresholds.init,
                         # alarm engine, web-push listener with silence
                         # gate)
    ws.py                # WebSocket fan-out
    routes/              # status, checks, alarms, silences, history,
                         # radars, upstream, admin (groups, thresholds,
                         # alerts, users, email, audit), push, debug
  db/
    schema.sql           # full DDL (~18 tables). Idempotent.
    store.py             # asyncpg pool + CRUD helpers
  config.py              # settings + radarca knowledge (products, radars,
                         # moments, RADAR_SILENT_FAIL_S defaults).
                         # Used as the seed for the threshold blob.
  main.py                # uvicorn entry

frontend/src/
  lib/
    format.ts            # stageLabel / stageTechCode / stageTooltip +
                         # productLabel + productCategory + the four-
                         # bucket PRODUCT_CATEGORY map (Radar Data /
                         # Atmospheric Forecast / CoSMoS / NWM)
    api.ts               # typed REST client. installFetchPrefix() in
                         # origin.ts attaches Authorization: Bearer.
    origin.ts            # API_BASE detection (port-3000 heuristic) +
                         # fetch monkey-patch
    stores/state.svelte.ts  # rollup + alarms + metrics + WS handler
    components/          # MultiSelectChips, PieStatus, HistoryDetailModal,
                         # ReportExportModal, SilenceMatcherPicker,
                         # PushRoutingEditor, StatusDot, Sparkline,
                         # SectionHeader, MapView, TimeControls, LazyImage
    components/mobile/   # MobileNav, MobileStatusMap (composites +
                         # playback), MobileDrillDown
  routes/
    +layout.svelte       # desktop chrome (header w/ clocks + tally,
                         # stage strip, footer attribution) — gated by
                         # isMobile so /m/* renders its own shell
    +page.svelte         # Live: map + radars + Products grouped by
                         # category + Site + alarm stream
    timeline/+page.svelte    # state-over-time grid + cell detail panel
                         # with explainRun (per-check verify-yourself
                         # reasoning + LazyImage)
    history/+page.svelte # multi-select chip filters + click-to-modal
    admin/               # config, groups, thresholds, alerts, silences,
                         # users, email, audit (auth-gated by layout)
    m/                   # mobile shell (sticky AQPI SENTINEL + UTC
                         # clock header, sticky bottom nav, safe-area)
      +page.svelte       # Status — stage rollup + tappable rows
      timeline/+page.svelte    # vertical alarm event list with filters
      alarms/+page.svelte      # open alarms with ack/unack
      more/+page.svelte        # push enable + theme + "customize
                               # routing →" + install hint + version
      push-settings/+page.svelte           # device list
      push-settings/edit/+page.svelte      # full per-device editor
    settings/devices/+page.svelte          # desktop mirror of /m/push-settings
  hooks.server.ts        # UA-sniff redirect (/ → /m on phone UA)
  static/                # manifest.webmanifest, sw.js, icons
```

A new check requires **one new file** under `backend/checks/`, a `@register` line at the bottom, and (optionally) an import in `backend/checks/__init__.py`. The scheduler, store, alarm engine, API, and frontend all consume the generic `CheckResult` envelope — no central list to update. Stages are just labels: adding `INFRA` for storage-cluster heartbeats works the same way as adding L5. Transports are equally pluggable (HTTP and headless-browser shipped; SSH / S3 / SNMP / filesystem / subprocess each drop in as a single adapter file).

Full design rationale and the cross-cutting architecture (threshold registry, groups, reprocess engine, etc.) live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Operational runbook in [`docs/MAINTENANCE.md`](docs/MAINTENANCE.md).

### Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12, FastAPI, asyncpg, httpx, Playwright (for L3A), PIL + numpy + imagehash (for L4 imaging), argon2-cffi (auth), aiosmtplib (email), pywebpush (Web Push) |
| Storage | Postgres 16 (single docker container), JSONB for check payloads + settings, content-addressed PNG archive on disk |
| Real-time | WebSocket on FastAPI; transition-only broadcast |
| Frontend | SvelteKit 2 + Svelte 5 (runes), Tailwind v4, MapLibre GL JS + Stadia Maps tiles |
| Mobile | PWA — manifest scoped to `/m/`, service worker for offline shell, iOS install-before-push respected |
| Alarms | Config in DB (or `alerts.yaml` fallback), Jinja2 email templates, aiosmtplib for SMTP, pywebpush for Web Push |
| Auth | argon2id passwords, UUID Bearer tokens in `localStorage`, sessions table |
| Orchestration | `make` for dev, `docker compose` for prod |
| Reverse proxy | User-owned Traefik in prod with TLS; direct compose works on plain HTTP LAN |

### Live data sources Sentinel reads

- `/api/radar-status/` — declared UP/DOWN per radar
- `/api/productDetail?file=…/details*.json` — per-product step manifest
- `/api/imageData?file=…` — PNG bytes
- `/api/xbandRadarImages/?radarFolder=…&productPrefix=…` — per-radar scan manifest
- `/api/get_{stream,observed_stream}_data/<comid>/<…>` — stream canaries
- `/geojson/*.geojson`, `/data/stream_data.csv` — static vector assets
- `https://mesonet.agron.iastate.edu/...` — NEXRAD ground-truth (live tiles + WMS-T historical)
- `https://tiles.stadiamaps.com/...` — basemap styles

All radarca paths and their conventions are reverse-engineered in [`docs/radarca-public-characterization.md`](docs/radarca-public-characterization.md).

---

## Frontend tour

A CSU-themed operations console. Two main routes plus an admin surface plus a mobile sibling.

### `/` — Live

- **Header** — `AQPI SENTINEL · radarca.engr.colostate.edu`, two clocks (UTC + your local with TZ abbrev), pass/warn/fail/skip rollup, liveness pulse, sign-in chip, theme picker (Light / Auto / Dark, persisted to localStorage).
- **Stage strip** — pass-status dot + descriptor + tally per stage (`Connectivity 5/5 · Product Freshness 16/17 · Radar Scans 4/6 1 F · …`). Hover for the canonical L# code.
- **Left rail** — six radar rows (status dot, ID, UP/DOWN/WARN pill, last-hour scan sparkline, scans/h). Below: Site checks (L0 only — TLS, origin liveness, public page, root 404).
- **Center (hero)** — MapLibre map with Stadia dark/light basemap (auto-swapped on theme change). Radar icons split: **center dot = worst-case verdict (red for warn AND fail), halo = staleness mode (yellow ring for ghost-up, red ring for hard-down)** — readable at a glance. Range rings, click-to-toggle per-radar overlays, programmatic bearing so northernmost radar pins top-left.
- **Right rail** — **Products grouped by category** (Radar Data / Atmospheric Forecast / CoSMoS / National Water Model). Each row has status dot, friendly product name (`Total Precip · 15 min QPE`, not `qpe_15min`), signed compound age (`+4m20s`, `-5d12h37m`), 30-pt sparkline of `age_s`.
- **Layers panel** (top-right of map) — composite picker (10 products via `<optgroup>`: Radar Data + Atmospheric Forecast), NEXRAD toggle, moment tabs (`Z V Zdr ΦDP ρhv`, with a full-name readout below), quick-filter text links (`all · x-band · down · clear`), per-radar checklist with solo-on-hover, overlay opacity.
- **Time strip** (below map) — big timestamp, transport (`⏮ ⏪ ▶ ⏩ ⏭`), tick-marked scrubber, per-step activity bars (height = non-empty pixel fraction). Composite + NEXRAD (via WMS-T, prefetched into a blob cache for flicker-free playback) + every active per-radar scan all advance together when you scrub.
- **Alarm row** — severity pill, descriptor stage, alarm id, target, `opened_at` clock, age, message, **ack** / **unack** button, "Open in Timeline" deeplink.
- **Footer** — attribution.

### `/timeline`

State-over-time grid pivoted on `(check × time bucket)`. Bucket grain selectable from `1m / 5m / 15m / 1h / 6h / 1d`; visible buckets fall into the canvas extents. Per-tab pie status icons. Newest column always labelled + **NOW** pill. Cell tooltip shows status + count; click opens a per-bucket detail panel with explainRun (thresholds vs observed, verify-yourself URLs, "how to replicate" recipe) and a LazyImage of any captured L4 frame in the bucket. Products tab subdivides by the four category groups. **Export Report** modal builds a bucketed CSV (long format).

### `/history`

Multi-select chip filters for stage / target / severity / status — populated from the registry, with `add custom…` for unknown values. CSV export honors every filter. Row click opens a drill-down modal with full reasoning trail (L1 sub_status, L2 reconcile bundle, L4 tier1/tier2 verdicts) and the captured image. Deeplinkable via URL params (every drilldown carries an `Open in History` button).

### `/admin/*` (auth-gated)

- **Thresholds** — every detection knob editable. In-process cache reloads; next check tick uses new values. **Retroactive reprocess** panel re-classifies historical rows under the new thresholds (cancellable, with live progress).
- **Groups** — bundle users + a notification schedule (weekly with weekday mask, biweekly with anchor, one-off downtime, recurring quiet hours). Parent-group inheritance. Live "next 5 on-windows" preview.
- **Alerts** — recipient table with chip-based group multi-select; routing-rule matchers via registry-populated dropdowns; custom matcher dropdown (key + value); escalation steps with direct group references; per-step overlap-dedup explainer.
- **Silences** — preset matchers + custom form, UTC↔Local datetime toggle, edit existing.
- **Users**, **Email/SMTP**, **Audit log**.

### `/m/*` — Mobile (PWA-installable on iPhone)

- **Header** — `AQPI SENTINEL` + live UTC clock.
- **Status** (`/m`) — current stage rollup, expandable per-stage rows, status map with composite chip strip + play/scrub. Tap any check row for a full-screen drilldown.
- **Timeline** (`/m/timeline`) — vertical day-grouped alarm-event list with the pie status header. Accepts URL filters; widens lookback to 7 d when filtered.
- **Alarms** (`/m/alarms`) — open alarms grouped by severity, with ack + unack.
- **More** (`/m/more`) — enable Web Push, "customize routing →" link, theme picker, switch-to-desktop link, version.
- **Push routing** (`/m/push-settings`) — per-device editor at `/m/push-settings/edit/[id]` with severity floor, chip-based pattern picker (predefined radar IDs + product chips), delay, on-duty schedule, recurring quiet hours.

The footer attribution renders on both shells.

### Spurious-emission workflow (the original motivating use case)

1. Pick a radar in the Layers panel checklist (or click it on the map).
2. Toggle NEXRAD Composite on.
3. Press play in the time strip.
4. Watch whether the X-band echoes line up with NEXRAD's reflectivity. Use the activity bars to see whether the radar is producing data even when the visible PNG looks empty. Switch the moment tab between Z / V / Zdr / ΦDP / ρhv to inspect different signatures.
5. When you find a frame that looks wrong, the timestamp + radar id are at the top of the time strip — paste them into a bug report.

---

## API surface (key endpoints)

The OpenAPI surface auto-renders at `http://<host>:8000/docs` (Swagger UI) and `/redoc` — prefer that for exploration. The shapes below are a quick orientation.

```
# live state
GET  /api/status
GET  /api/stages
GET  /api/checks
GET  /api/checks/{id}/latest
GET  /api/checks/{id}/history?since=…&limit=…
GET  /api/checks/{id}/metrics?metric=…&limit=…
GET  /api/radars/meta

# alarms + acks + silences
GET  /api/alarms?status=open|closed|all
GET  /api/alarms/{id}                      # full + ack state + notification log
POST /api/alarms/{id}/ack                  # body: {note}
POST /api/alarms/{id}/unack
GET  /api/silences
POST /api/silences                         # body: {id, matchers, starts, ends, reason}
PUT  /api/silences/{id}                    # edit
DELETE /api/silences/{id}

# history — every list endpoint accepts comma-separated stage/target/etc.
GET  /api/history/alarms?since=…&until=…&stage=…&target=…&severity=…&check_id=…
GET  /api/history/checks?since=…&until=…&stage=…&target=…&status=…&check_id=…
GET  /api/history/timeline?bucket=5m&until=…&limit=120&stage=…&target=…
GET  /api/history/runs?check_id=…&target=…&since=…&until=…
GET  /api/history/export.csv?type=alarms|checks&… # detail-row export
GET  /api/history/report.csv?bucket=…&since=…&until=…&stage=…&target=…
                                                  # long-format bucketed report

# upstream proxies (radarca with local archive fall-through)
GET  /api/upstream/product_latest.png?product_id=…
GET  /api/upstream/product_image.png?product_id=…&step=N
GET  /api/upstream/product_steps?product_id=…
GET  /api/upstream/xband_scan.png?radar=…&moment=…&time=ISO
GET  /api/upstream/radar_steps?radar=…&moment=…
GET  /api/upstream/activity?product_id=…|radar=…&moment=…
GET  /api/upstream/image_by_source.png?source=…

# admin (require_admin)
GET  /api/admin/audit?limit=…&since=…
GET  /api/admin/users + invite/disable/role flow at /api/users
GET  /api/admin/email + POST /api/admin/email/test
GET  /api/admin/alerts                     # alerts config blob
PUT  /api/admin/alerts                     # save + engine.reload
GET  /api/admin/groups                     # full list with expanded members
POST /api/admin/groups
PUT  /api/admin/groups/{gid}
DELETE /api/admin/groups/{gid}
POST /api/admin/groups/{gid}/preview       # next-N on-windows from draft schedule
GET  /api/admin/thresholds                 # blob + seed defaults + version
PUT  /api/admin/thresholds
POST /api/admin/thresholds/reprocess       # body: {since, until, only_stages, confirm:"APPLY"}
GET  /api/admin/thresholds/reprocess/status?job_id=…
POST /api/admin/thresholds/reprocess/cancel

# auth
POST /api/auth/login                       # body: {email, password}
POST /api/auth/logout
GET  /api/auth/me
POST /api/auth/reset/{token}               # complete invite or password reset

# Web Push
GET  /api/push/vapid_public
POST /api/push/subscribe
DELETE /api/push/unsubscribe
GET  /api/push/status                      # subscribed? + count
GET  /api/push/subscriptions               # own devices
PUT  /api/push/subscriptions/{id}/routing  # per-device filters + schedule

# real-time + diag
WS   /api/ws?token=…                       # {type: hello|run|alarm_*|ping}
GET  /api/_debug/stats                     # loop lag, pool, WS, scheduler, archive
```

---

## Configuring alarms

Two surfaces:

1. **`/admin/alerts`** (preferred) — recipients, escalation policies, routes; live-reloaded by the engine without restart.
2. **`alerts.yaml`** at the repo root — disk fallback. Used on first boot when the DB doesn't yet have an `alerts_config` row. Once you save via the admin UI, the DB row wins forever; the YAML stays around as initial template / fallback.

YAML shape (still useful for bootstrap):

```yaml
smtp:                              # optional; also configurable from /admin/email
  host: smtp.fastmail.com
  port: 587
  starttls: true
  username: ${SMTP_USER}            # env-interpolated
  password: ${SMTP_PASS}
  from_addr: sentinel@yourdomain.example
  from_name: AQPI Sentinel

receivers:
  - name: console                   # ships by default
    console: true
  # - name: oncall
  #   email: ["you@example.com"]
  #   group_ids: [3]                # also page the on-call rotation group
  # - name: chat
  #   webhook: ${DISCORD_WEBHOOK_URL}

escalation_policies:
  - name: standard
    steps:
      - delay: 0m
        receivers: [console]
      # - delay: 15m
      #   receivers: [oncall]
      #   group_ids: [2]            # add the storm-watch group at this step
      # - delay: 60m
      #   receivers: [oncall, chat]

routes:                             # first match wins
  - match: {stage: L0}              # site/origin down — page hard
    severity_floor: critical
    policy: standard
    repeat_interval: 10m
  - match: {stage: L2}              # any radar misalignment
    policy: standard
    repeat_interval: 1h
  - match: {stage: L1}              # any product issue
    policy: standard
    repeat_interval: 1h
  - match: {}                       # catch-all
    policy: standard
    repeat_interval: 4h
```

Detection thresholds are NOT in this YAML — they live in `settings.thresholds` (jsonb), editable at `/admin/thresholds` or seeded from `backend/config.py` on a fresh DB. See [`docs/MAINTENANCE.md`](docs/MAINTENANCE.md) § "Edit detection thresholds".

Groups + their notification schedules are editable at `/admin/groups` and stored in the `groups` + `group_members` tables. See [`docs/MAINTENANCE.md`](docs/MAINTENANCE.md) § "Define a notification group".

---

## Validating against the upstream

`validation_tests/` keeps the original throw-away scripts that reverse-engineered radarca's behavior (Layer 0–4 probes used during the characterization phase). They're frozen — the production code paths live in `backend/checks/`. Run them only when re-validating after radarca changes:

```bash
./.venv/bin/python validation_tests/test_layer0_website.py
./.venv/bin/python validation_tests/test_layer1_endpoints.py
# … etc
```

---

## Documentation in `docs/`

| File | What's inside |
|---|---|
| `ARCHITECTURE.md` | System map + check lifecycle walkthrough + "why does X look like that" rationale + threshold registry / groups / reprocess / push-routing internals. **Start here if you're new to the codebase.** |
| `MAINTENANCE.md` | Operational runbook: restart/redeploy, log locations, backup/restore, common-failure recipes, the `?diag=` bisect harness, threshold + group + push-routing editing walkthroughs, gotchas. **Start here when something's broken or you need to change config.** |
| `radarca-public-characterization.md` | Full reverse-engineering of radarca: every API endpoint, every product, image extents, JS mappings, response semantics. Read this when you need to know what an upstream URL does. |
| `radarca-monitoring-plan.md` | Pre-implementation strategy: what each layer monitors, recommended cadences, alarm logic, scoping decisions. Historical context. |
| `radarca-implementation-plan.md` | The original system design: architecture, stack, data model, API, frontend, phasing, extensibility, alerting subsystem, auth/RBAC, history, Layer 4 Tier 1–5. Useful for understanding planning intent vs. shipped reality. |

For interactive API exploration, also hit:
- `http://<host>:8000/docs` — Swagger UI for every endpoint
- `http://<host>:8000/redoc` — alternative API browser

---

## Constraints + gotchas

- **Vite dev server is fragile** under heavy edits. `make restart-fe` is the answer. In Docker (prod) this disappears.
- **The dashboard at `/public` is permanently cached** at radarca's edge (`s-maxage=31536000`, `x-nextjs-cache: HIT`). The only origin-liveness signal is hitting any `/api/*` endpoint — `layer0.origin.alive` does exactly that.
- **`GET /` to radarca returns HTTP 200 with the Next.js not-found page body**, not 404. The L0 check asserts via body markers.
- **The stream API error message lies**: it says `Use YYYYMMDD_HHMM` but actually accepts `YYYYMMDD_HH`. Worked around in `layer1_stream.py`.
- **X-band radars publish no RhoHV imagery** across all five (CBAND is the only one). This is a real upstream gap that L2 reconciliation catches as `dead_moments: ['RhoHV']` — filtered via `EXPECTED_ABSENT_MOMENTS` in `layer2_radar.py`.
- **Per-radar overlay bboxes are computed** from `lat ± range/111` (cosine-corrected) — close to but not identical to radarca's hand-tuned per-radar `imageExtent`. The composite/NEXRAD layers use the actual extracted extents.
- **Forecast products have negative `max_freshness_s`** intentionally — their latest step is FUTURE-dated, so `age_s = now - latest_ts` is negative. See `docs/MAINTENANCE.md` for the full rationale.
- **DNS-flake demote has two paths** — `_is_local_dns_error` (raw exceptions) and `_maybe_downgrade_for_dns_summary` (in-check-catch errors). New checks that wrap upstream calls in `try/except` MUST surface the error in the summary string so the post-result pass can match.
- **Push delay scheduling is in-memory** — backend restart cancels pending delays. Fine for "snooze me 5 minutes"; durable scheduling is a future slice.

The full list lives in [`docs/MAINTENANCE.md`](docs/MAINTENANCE.md) § "Common pitfalls".

---

## License

Internal CSU/CHILL project. Not currently licensed for redistribution.

Built by [Joseph Mesches](https://github.com/jkmesches) for [Dr. V. Chandrasekar's](https://chill.colostate.edu/) AQPI program at CSU CIRA / ECE.
