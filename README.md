# Sentinel

> Continuous, hierarchical health monitoring for **[radarca.engr.colostate.edu](https://radarca.engr.colostate.edu/public)** — the CSU-CHILL X-band radar network dashboard.

Sentinel watches the upstream dashboard from five complementary angles, surfaces anomalies as alarms with email-able escalation, and exposes everything through a dark operations-center UI built for a single radar engineer to glance-monitor a multi-radar network.

```
   ┌──────────────────────────────────────────────────────────────────┐
   │  L0  edge alive                                                  │
   │  L1  per-product freshness / image existence / parity            │
   │  L2  per-radar reconciliation (declared vs observed)             │
   │  L3  JS overlay timestamp parity (Playwright)                    │
   │  L4  image stats + heuristics (Tier 1–2 live; 3–5 planned)       │
   └──────────────────────────────────────────────────────────────────┘
                  │                            │
                  ▼                            ▼
              alarm engine                  WebSocket
            (YAML routes,                       │
             escalation,                        ▼
             ack, silences)              SvelteKit dashboard
                  │                       (live + history)
                  ▼
            email / webhook / console
```

This repo holds the entire stack — Postgres schema, Python backend (FastAPI + scheduler + alarm engine + Playwright + image processing), SvelteKit frontend, dev scripts, and the planning/characterization docs that describe radarca's API surface and the monitoring strategy.

---

## Status

P1.0 – P1.8 of the implementation plan complete and continuously running against live radarca data:

- **36 checks** registered across 5 stages (L0/L1/L2/L3/L4‑T1T2), self-registered via a `@register` decorator.
- **Postgres 16** schema with check_runs / metric_samples / alarms / alarm_acks / silences / image_archive / image_embeddings / image_labels / users / sessions / admin_audit (full schema in `backend/db/schema.sql`).
- **Alarm engine** with YAML-driven routing, escalation policies, acks, silences, suppression DAG, and pluggable sinks (console + webhook + SMTP-ready email).
- **Live SvelteKit dashboard** at `:5173` — header (UTC + local time + theme picker), stage strip, radar/product rails with sparklines, alarms list with `opened_at` timestamps + age, MapLibre map (Stadia dark/light) with composite/NEXRAD/per-radar overlays, time scrubber with play/pause and step controls, activity sparkline per step.
- **History** route with filterable alarms + check-run table + CSV export.
- **Dev lifecycle** behind `make`: one command starts everything, one restarts each piece cleanly when vite's HMR loses track.

Built but not yet wired: docker compose (P2), live deploy (P3), Layer 4 Tier 3 / Tier 4 / Tier 5 (P4.1–P4.4 — cross-radar, dual-pol, learned classifier). Auth tables exist; login flow per §17 of the plan is the next user-facing slice.

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

Open `http://localhost:5173`. The first wave of checks fires within 1 second and the dashboard populates immediately. Real production findings will show up — at the moment of writing, two of the six X-band radars (XSCV, EBAY) are DOWN, the atmospheric forecast pipeline is ~10 days stale on `fcst_precip_rate`, and the X-band RhoHV channel publishes 0 scans across every X-band radar (CBAND is the only radar publishing it). All of these are real upstream conditions, not Sentinel bugs.

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

---

## How it works

### Architecture (extensibility-first)

```
backend/
  registry.py            # @register populates a global dict; nothing else
                         # knows what checks exist
  scheduler.py           # one asyncio task per registered check
  checks/
    base.py              # Check, CheckContext, CheckResult — universal envelope
    transports/          # http / browser today; ssh/fs/s3/snmp drop in here
    layer0_website.py    # one file per layer; @register at the bottom
    layer1_product.py    #   ↳ parameterized — registers one Check per product
    layer1_vector.py
    layer1_stream.py
    layer2_radar.py
    layer3_overlay.py
    layer4_image.py
  alarms/
    engine.py            # state machine + ticker
    router.py            # YAML routes → policy + escalation
    suppression.py       # walk depends_on graph
    conditions.py        # time_of_day_in, duration_at_severity_min, etc.
    silences.py
    sinks/               # console / email / webhook (pluggable)
    templates/           # Jinja2 plain-text email
  api/
    app.py               # FastAPI app + lifespan
    ws.py                # WebSocket fan-out
    routes/              # status, checks, alarms, silences, history,
                         #     radars, upstream, …
  db/
    schema.sql           # full DDL (16 tables)
    store.py             # asyncpg pool + CRUD helpers
  config.py              # settings + radarca knowledge (products, radars, moments)
  main.py                # uvicorn entry
```

A new check requires **one new file** under `backend/checks/`, a `@register` line at the bottom, and (optionally) an import in `backend/checks/__init__.py`. The scheduler, store, alarm engine, API, and frontend all consume the generic `CheckResult` envelope — no central list to update. Stages are just labels: adding `INFRA` for storage-cluster heartbeats works the same way as adding L5. Transports are equally pluggable (HTTP and headless-browser shipped; SSH / S3 / SNMP / filesystem / subprocess each drop in as a single adapter file).

Full design rationale and forward roadmap is in [`docs/radarca-implementation-plan.md`](docs/radarca-implementation-plan.md).

### Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12, FastAPI, asyncpg, httpx, Playwright (for L3A), PIL + numpy (for L4 imaging) |
| Storage | Postgres 16 (single docker container), JSONB for check payloads, content-addressed PNG archive on disk |
| Real-time | WebSocket on FastAPI |
| Frontend | SvelteKit 2 + Svelte 5 (runes), Tailwind v4, MapLibre GL JS + Stadia Maps tiles |
| Alarms | YAML config, Jinja2 email templates, aiosmtplib for SMTP |
| Orchestration (dev) | Plain `make` + the scripts in `scripts/` |
| Orchestration (prod, planned) | `docker compose` per §8 of the plan |

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

A dark operations-center dashboard, two routes:

### `/` — Live

- **Header** — `SENTINEL · radarca.engr.colostate.edu`, two clocks (UTC + your local with TZ abbrev), pass/warn/fail rollup, liveness pulse, theme picker (Light / Auto / Dark, persisted to localStorage).
- **Stage strip** — one dot + tally per stage (`L0  4/4`, `L1  13/17  3W`, …). Color follows worst-of-stage.
- **Left rail** — six radar rows (status dot, ID, UP/DOWN/WARN pill, last-hour scan sparkline, scans/h). Below: edge + static checks.
- **Center (hero)** — MapLibre map with Stadia dark/light basemap (auto-swapped on theme change), radar dots colored by status, range rings, click-to-toggle per-radar overlays.
- **Right rail** — product checks sorted by id, each with status dot, compound age (`+4m20s`, `-5d12h37m`), 30-pt sparkline of `age_s` (sawtooth ⇒ data flowing, monotonic rise ⇒ pipeline halted).
- **Layers panel** (top-right of map) — composite picker (`Off  Z  Ż  H₂O`), NEXRAD toggle, moment tabs (`Z V Zdr ΦDP ρhv`), quick-filter text links (`all · x-band · down · clear`), per-radar checklist with solo-on-hover, overlay opacity.
- **Time strip** (below map) — big timestamp, transport (`⏮ ⏪ ▶ ⏩ ⏭`), tick-marked scrubber, **per-step activity bars** (height = non-empty pixel fraction of that scan), step counter, ±60-min playback at any time. When you scrub, the composite, NEXRAD overlay (via WMS-T), and every active per-radar scan all advance together to the same wall-clock moment — the workflow for spurious-emission hunting.
- **Alarm row** — severity pill, stage, alarm id, target, `opened_at` clock, age, message, **ack** button.

### `/history`

- Date range picker, filter inputs (stage / target / severity / status), tab switch between **Alarms** and **Check Runs**, CSV export. Backed by `/api/history/*`.

### Spurious-emission workflow

1. Pick a radar in the Layers panel checklist (or click it on the map).
2. Toggle NEXRAD Composite on.
3. Press play in the time strip.
4. Watch whether the X-band echoes line up with NEXRAD's reflectivity. Use the activity bars to see whether the radar is producing data even when the visible PNG looks empty. Switch the moment tab between Z / V / Zdr / ΦDP / ρhv to inspect different signatures.
5. When you find a frame that looks wrong, the timestamp + radar id are at the top of the time strip — paste them into a bug report.

---

## API surface

```
# live state
GET  /api/status
GET  /api/stages
GET  /api/checks
GET  /api/checks/{id}/latest
GET  /api/checks/{id}/history?since=…&limit=…
GET  /api/checks/{id}/metrics?metric=…&limit=…

# alarms + acks + silences
GET  /api/alarms?status=open|closed|all
GET  /api/alarms/{id}                      # full + ack state + notification log
POST /api/alarms/{id}/ack                  # body: {user, note}
POST /api/alarms/{id}/unack
GET  /api/silences
POST /api/silences                         # body: {id, matchers, starts, ends, reason}
DELETE /api/silences/{id}

# history
GET  /api/history/alarms?since=…&until=…&stage=…&target=…&severity=…
GET  /api/history/checks?since=…&until=…&stage=…&target=…&status=…
GET  /api/history/export.csv?type=alarms|checks&…

# radar geography + upstream proxies
GET  /api/radars/meta
GET  /api/upstream/product_latest.png?product_id=…
GET  /api/upstream/product_image.png?product_id=…&step=N
GET  /api/upstream/product_steps?product_id=…
GET  /api/upstream/xband_scan.png?radar=…&moment=…&time=ISO
GET  /api/upstream/radar_steps?radar=…&moment=…
GET  /api/upstream/activity?product_id=…|radar=…&moment=…

# real-time
WS   /api/ws                               # {type: hello|run|alarm_open|alarm_close|alarm_promote|ping}
```

---

## Configuring alarms

Edit `alerts.yaml` at the repo root. Hot-reload on the API is planned (§14.6 of the implementation plan); for now restart the backend with `make restart-be`.

```yaml
smtp:                              # optional; without it, email sink is disabled
  host: smtp.fastmail.com
  port: 587
  starttls: true
  username: ${SMTP_USER}            # env-interpolated
  password: ${SMTP_PASS}
  from: "Sentinel <sentinel@yourdomain.example>"

receivers:
  - name: console                   # ships by default
    console: true
  # - name: oncall
  #   email: ["you@example.com"]
  # - name: chat
  #   webhook: ${DISCORD_WEBHOOK_URL}

escalation_policies:
  - name: standard
    steps:
      - delay: 0m
        receivers: [console]
      # - delay: 15m  receivers: [oncall]
      # - delay: 60m  receivers: [oncall, chat]

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

Full schema and routing semantics: [`docs/radarca-implementation-plan.md`](docs/radarca-implementation-plan.md) §14.

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
| `ARCHITECTURE.md` | One-page system map + check lifecycle walkthrough + "why does X look like that" rationale. **Start here if you're new to the codebase.** |
| `MAINTENANCE.md` | Operational runbook: restart/redeploy procedures, log locations, backup/restore commands, common-failure recipes, the `?diag=` bisect harness, gotchas. **Start here when something's broken.** |
| `radarca-public-characterization.md` | Full reverse-engineering of radarca: every API endpoint, every product, image extents, JS mappings, response semantics. Read this when you need to know what an upstream URL does. |
| `radarca-monitoring-plan.md` | Pre-implementation strategy: what each layer monitors, recommended cadences, alarm logic, scoping decisions. Historical context. |
| `radarca-implementation-plan.md` | The original system design: architecture, stack, data model, API, frontend, phasing, extensibility, alerting subsystem, auth/RBAC, history, Layer 4 Tier 1–5. Useful for understanding planning intent vs. shipped reality. |

For interactive API exploration, also hit:
- `http://<host>:8000/docs` — Swagger UI for every endpoint
- `http://<host>:8000/redoc` — alternative API browser

---

## Constraints + gotchas

- **Vite dev server is fragile** under heavy edits. `make restart-fe` is the answer. In Docker (P2) this disappears.
- **The dashboard at `/public` is permanently cached** at radarca's edge (`s-maxage=31536000`, `x-nextjs-cache: HIT`). The only origin-liveness signal is hitting any `/api/*` endpoint — `layer0.origin.alive` does exactly that.
- **`GET /` to radarca returns HTTP 200 with the Next.js not-found page body**, not 404. The L0 check asserts via body markers.
- **The stream API error message lies**: it says `Use YYYYMMDD_HHMM` but actually accepts `YYYYMMDD_HH`. Already worked around.
- **X-band radars publish no RhoHV imagery** across all five (CBAND is the only one). This is a real upstream gap that L2 reconciliation catches as `dead_moments: ['RhoHV']`.
- **Per-radar overlay bboxes are computed** from `lat ± range/111` (cosine-corrected) — close to but not identical to radarca's hand-tuned per-radar `imageExtent`. The composite/NEXRAD layers use the actual extracted extents.
- **No auth yet.** The dashboard is fully public, write endpoints accept whatever `user` you pass in the body. §17 of the plan is the design; not built.

---

## Next slices

In priority order:

1. **Auth + admin gating** (§17) — bcrypt sessions, public read / admin write, audit log of every admin action.
2. **P2: Dockerize** — `docker compose up` is the whole stack. Removes the vite-restart dance and makes deployment trivial.
3. **P3: Deploy** to your home cluster, Caddy + Let's Encrypt.
4. **P4.1: Layer 4 Tier 3** — cross-radar consistency check (X-band vs NEXRAD over overlap regions).
5. **P4.2–P4.4: Layer 4 Tier 4 + Tier 5** — dual-pol consistency and the learned classifier for "weird artifact this isn't normal" detection.

---

## License

Internal CSU/CHILL project. Not currently licensed for redistribution.
