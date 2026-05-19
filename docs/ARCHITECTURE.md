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
   │   routes/history/+page.svelte (filtered alarms + check_runs        │
   │     with click-to-drilldown modal)                                 │
   │   routes/admin/* (auth-gated: users, email, alerts, silences,      │
   │     groups, thresholds, audit)                                     │
   │   routes/m/* (mobile shell — Status, Timeline, Alarms,             │
   │     push-settings, More)                                           │
   │   routes/settings/devices (desktop mirror of per-device push)      │
   │                                                                    │
   │   lib/origin.ts: API_BASE detection + global fetch monkey-patch    │
   │     (attaches Authorization: Bearer header from localStorage)      │
   │   lib/format.ts: stageLabel/productLabel/productCategory —         │
   │     single source of truth for user-facing vocabulary              │
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
   - Computes 8 sub-checks (`A_api_up`, `B_schema`, …, `H_image_hash`)
     plus an L3B parity fold-in (timestamp parity for observed
     products, step-index contiguity for forecasts — see § *L3B
     parity* below).
   - Every threshold (`max_freshness_s`, `min_png_bytes`,
     `expected_steps`, cadence tolerance, …) is read at evaluation
     time through `backend/thresholds.py`, NOT directly from
     `config.PRODUCTS` constants. See § *Threshold registry*.
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
   - Transitions back to pass (or skip) → close it.
   - Routes through the configured `Router` (rule = matchers in the
     loaded `AlertsConfig`) to receiver(s).
   - For each receiver, `_expand_receiver_groups()` schedule-gates
     and expands `group_ids` into member emails (see § *Groups*).
     `EscalationStep.group_ids` is handled the same way via a
     synthetic anonymous receiver.
   - Per-step **email dedup**: every email already covered by an
     earlier receiver in this step is dropped from later receivers'
     lists. A user reachable through both a group AND a direct
     recipient gets one message, not two.
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

The same pattern applies to **thresholds** (see § *Threshold
registry*) — `PUT /api/admin/thresholds` writes the new blob and
refreshes the in-process cache. The next check tick reads new values.

If you add more dynamic config (e.g., per-check cadence tweaking),
follow the same pattern: a `reload()` method on the owning
component, called from the admin endpoint after the DB write.

### Stage vocabulary (single source of truth)

Internal stage IDs (`L0` / `L1` / `L2` / `L3` / `L4-T1T2`) flow through
the data layer for filtering and grouping. Anything user-facing goes
through descriptors:

| ID | Descriptor |
|---|---|
| L0 | Connectivity |
| L1 | Product Freshness |
| L2 | Radar Scans |
| L3 | Map Overlays |
| L4-T1T2 | Image Quality |

Two parallel modules:

- **`frontend/src/lib/format.ts`** — `stageLabel()`, `stageTechCode()`,
  `stageTooltip()` for templates; plus `productLabel()` /
  `productCategory()` / `PRODUCT_CATEGORY_*` for the four-bucket grouping
  (Radar Data / Atmospheric Forecast / CoSMoS / National Water Model)
  used on Live, Timeline, and Mobile.
- **`backend/stages.py`** — `stage_descriptor()` and `stage_tech()` for
  email templates, push titles, and any backend-rendered text.

Both maps must be kept in sync if a new stage ever appears.

### Threshold registry — admin-managed knobs (`backend/thresholds.py`)

Every detection threshold (per-product freshness, per-radar ghost-up
timeout, L4 image-QC parameters, global hysteresis/tolerance) is read
through a single in-process cache that backs onto a `settings.thresholds`
jsonb blob:

```jsonc
{
  "products": { "qpe_15min": { "max_freshness_s": 360, … }, … },
  "radars":   { "XEBY":      { "silent_fail_s": 300 }, … },
  "l4":       { "comp_ref":  { "extreme_threshold": 0.40, … }, … },
  "globals":  { "hysteresis": 0.10, "cadence_tol": 0.10,
                "step_count_tol": 4, "silent_fail_default": 600 }
}
```

API:

- `thresholds.init(pool)` — called once in the FastAPI lifespan. Seeds
  from `config.py` defaults on a fresh DB; otherwise loads the row.
- `get_product(pid, key)` / `get_radar(rid, key)` / `get_l4(pid, key)`
  / `get_global(key)` — synchronous getters with **three-tier
  fallback**: DB override → `config.py` constant → hard-coded default.
  Fresh DB behaves identically to old all-from-config code.
- `save_blob(pool, blob, updated_by)` — persists + refreshes cache.

Every check evaluator (`layer1_product.py`, `layer2_radar.py`,
`layer4_image.py`, …) reads through these getters. Adding a new check
that has a threshold? Add the key to the seed blob in
`thresholds._seed_from_config()` and route the read through
`get_product` / `get_radar` etc.

UI: `/admin/thresholds` is a big table editor across Globals / Products /
Radars / Image QC (L4). Diff-confirmation modal on save. Empty cell =
"use default" (placeholder shows the config.py value).

### Retroactive reprocess (`backend/reprocess_engine.py`)

When the admin changes a threshold, the historical timeline still shows
verdicts under the old values. The retroactive reprocess job walks
`check_runs` in a time window and re-classifies each row under
current thresholds:

```
ReprocessJob (in-process state: n_total / n_evaluated / n_changed /
                                 n_preserved / cancel_requested)
       │
       ▼
run_reprocess(pool, job)
  ├── per row, call one of:
  │     _reverdict_l1 → recompute C_freshness, E_step_count,
  │                     G_image_size sub-checks from saved payload
  │     _reverdict_l2 → recompute HEALTHY↔GHOST_UP from primary
  │                     newest_ts + current silent_fail_s + hysteresis
  │     _reverdict_l4 → lift extreme/frozen tier-2 verdicts under
  │                     current extreme_threshold / frozen_min_cov_pct
  │                     / skip_frozen / skip_range_ring
  │
  └── batched UPDATEs (CHUNK=200) with await sleep(0) every 50 rows
      so the API stays responsive during long jobs
```

**Preserves** rows where `payload.reason in {local_dns_error,
transport_error}` (transport failures don't get reclassified as
heuristic verdicts) and rows with original `status in {skip, error}`.

UI panel at the bottom of `/admin/thresholds`: range picker + per-stage
checkboxes + `APPLY` confirm input + live progress bar polled at 1 Hz.
Cancellable.

Standalone scripts (`backend/reprocess_l4.py`, `reprocess_l1_forecasts.py`,
`reprocess_l2_dead_moments.py`, `reprocess_dns_errors.py`) still exist
for one-off CLI runs and serve as templates for new reprocess jobs.

### Groups + notification schedules (`backend/groups.py`)

Notification routing now supports **groups** — bundles of users with a
shared on-duty schedule. Schedules can be `always`, `weekly` (weekday
mask + time windows), or `biweekly` (with an anchor date that fixes
the on-week parity). Both one-off `downtime` ranges and
**recurring downtime** (e.g., "every night 22:00–06:00") layer on top.

Schema:
- `groups` (id, name, description, **parent_group_id** self-FK,
  schedule jsonb, …)
- `group_members` (group_id, user_id, …)

Evaluator API:
- `is_active_at(schedule, when)` — synchronous single-schedule check.
- `effective_is_active(pool, group_id, when)` — walks the parent chain
  (capped at 8 levels for cycle safety) and ANDs every schedule's
  verdict. A child group inherits its parent's on-windows + downtime.
- `next_on_windows(schedule, start, count)` — returns the next N
  on-windows from a given time. Used by the `/admin/groups` preview.

Groups thread through dispatch via:
- `Receiver.group_ids: list[int]` — at alarm dispatch,
  `engine._expand_receiver_groups()` schedule-gates each group, fetches
  active members' emails, and merges them into the receiver's email
  list. **All groups inactive → receiver skipped entirely**.
- `EscalationStep.group_ids: list[int]` — same machinery, just one
  level higher in the route hierarchy. The engine synthesizes an
  anonymous Receiver per group_id.

Per-step **email dedup** runs after each receiver's expansion. The first
receiver to cover a given email "owns" it; later receivers drop it from
their lists. Webhook and console aren't deduped (no addressee identity).

UI: `/admin/groups` is a master/detail page with full schedule editor —
weekday button mask, multiple time windows, biweekly anchor date with
live preview, one-off and recurring downtime sections.

### Per-device push routing

The `push_subscriptions` table has two extra columns:
- `label TEXT` — user-friendly device name.
- `routing_config jsonb` — per-device filter blob:
  ```jsonc
  {
    "severity_floor":   "info" | "warn" | "critical",
    "product_patterns": ["XSCV", "qpe_15min", "fcst_*"],
    "delay_s":          120,
    "schedule":         { /* same shape as groups schedule */ }
  }
  ```

`backend/push.py:dispatch_push()` filters each subscription by:
1. `severity_floor` — drop notifications below the chosen rank.
2. `product_patterns` — fnmatch substring against the payload's
   `tag` / `title` / `body`. Any match passes.
3. `schedule` — evaluated via `groups.is_active_at()`. Off-duty = skip.
4. `delay_s` — if > 0, deferred via
   `asyncio.create_task(_send_delayed(…))`. **In-memory only**;
   restart cancels pending delays.

Returns `{sent, failed, expired, filtered, deferred}` for logging.

UI: `/m/push-settings` lists the user's devices; tapping routes to
`/m/push-settings/edit?id=…` (a real page, NOT a drilldown sheet — the
previous variant trapped scroll under iOS body-scroll-lock). Pattern
picker is chip-based (predefined radar IDs + product chips + `<details>`
for custom patterns). Schedule editor with recurring quiet-hours toggle.
Desktop mirror at `/settings/devices`.

### Map composites + playback

The desktop `MapView` carries a 10-product composite dropdown (grouped
via `<optgroup>` into Radar Data and Atmospheric Forecast) and a
playback strip. NEXRAD has its own time scrubber backed by the Iowa
Mesonet WMS-T endpoint.

Two pieces worth knowing:

- **Programmatic map bearing** — `defaultBearingFromRadars()` finds
  the northernmost + southernmost xband/cband radars, computes the
  south→north bearing, and sets `map.bearing = ang + 45` so that
  vector aligns with the screen's top-left → bottom-right axis.
  With the current roster: XSCW pins top-left, XSCR pins bottom-right.
  Same algorithm runs on `MobileStatusMap` so mobile and desktop
  share orientation.
- **NEXRAD prefetch** — `prefetchNexradFrames()` background-warms the
  blob cache for every synthesized step URL the moment NEXRAD toggles
  on. With blobs already in memory, `syncNexradTime` swaps via
  `src.updateImage({url: blobURL})` instantly — no per-tick HTTP
  fetch, no flicker. Latest synthesized frame is offset 6 min back from
  wall-clock to dodge WMS publication lag (asking for a frame that
  hasn't been published yet returns an empty tile).

`MobileStatusMap.svelte` also carries a composite chip strip + play/
pause/step buttons + range slider — same backing endpoints
(`/api/upstream/product_steps`, `/api/upstream/product_image.png`).

### L3B parity — two modes

`backend/checks/layer1_product.py` folds the L3B parity check into the
L1 product result. It picks one of two parsers per step:

- **Timestamp parity** (`parse_filename_ts`) for observed products
  whose filenames encode a time — `comp_ref` → `20260518_0028.png`,
  X-band → `scwa_CorrReflectivity_20260518-2336.png`, QPE →
  `…_20260518_193000_rainfall.nc.png`. Compared against the manifest's
  `timestamp` field (±60 s tolerance).
- **Step-index contiguity** (`parse_filename_step_idx`) for forecast
  products whose filenames encode a step index —
  `C_hrrr_<prod>_step<N>.png`. The starting index varies by product
  (`fcst_total_precip` starts at step0, `fcst_precip_rate` at step1),
  so position-in-manifest isn't a meaningful invariant. We verify
  monotonic contiguity instead: every step's parsed index = previous + 1.
  Catches gaps, duplicates, out-of-order serving.

Each step lands in exactly one bucket; mode is reported in
`payload.parity.mode` (`timestamp` / `step_index` / `mixed` / `none`).
Verdict: `skip` when neither parser fires, `fail` on any mismatch,
`pass` otherwise.

### DNS-flake demote (two-path)

Local DNS hiccups must NEVER fire alarms — they're our infra, not
upstream. Two paths cover the two ways a DNS error can surface:

- **Raw bubbled exceptions** — caught in `scheduler._loop`'s
  `except Exception` block. `_is_local_dns_error(e)` walks the
  `__cause__`/`__context__` chain looking for `socket.gaierror` or
  any of the EAI errno markers (`[Errno -2]`, `[Errno -3]`,
  `[Errno -5]`, name-not-known strings). Match → demote to
  `status=skip, reason=local_dns_error`.

- **In-check-catch errors** — layer1_product / layer3_overlay /
  layer4_image evaluators catch their own transport exceptions and
  return `CheckResult(status=error, summary="A_api transport: [Errno -5]
  No address associated with hostname")`. The scheduler catch never
  sees the exception. `_maybe_downgrade_for_dns_summary()` runs as a
  post-result pass: if status is fail/error AND summary matches any
  DNS marker, demote with the same `reason=local_dns_error`.

`layer0.net.*` (the network control check) is exempt from both paths —
it MUST surface real DNS state.

### Timeline bucket alignment (snap UP, not DOWN)

`/api/history/timeline` snaps `until` to a bucket boundary so the dense
grid aligns cleanly. **Snap UP** — the previous logic snapped DOWN and
silently truncated up to (bucket_s − 1) seconds of the in-flight
bucket. At 5 m grain with wall-clock now=23:10, the snap was a no-op;
at 15 m grain the snap rounded to 23:00 → failures between 23:00 and
23:10 disappeared from the grid on a grain switch.

The dense-bucket loop already tolerates a partial in-flight bucket;
empty cells during a partial fill are normal.

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

## API surface (key endpoints)

| Method + path | What |
|---|---|
| `GET /api/status` | Current rollup of every check |
| `GET /api/alarms[?status=open]` | Open / closed alarms |
| `POST /api/alarms/{id}/ack` / `unack` | Ack / unack |
| `GET /api/checks` | Registry (used by the admin routing-rule dropdowns) |
| `GET /api/history/{alarms,checks}` | List with multi-select filters (comma-sep values via `ANY($n::text[])`) |
| `GET /api/history/timeline` | Bucketed state-over-time grid |
| `GET /api/history/runs` | Full payload + artifacts for one (check_id, target) window |
| `GET /api/history/export.csv` | Detail-row CSV honouring every filter |
| `GET /api/history/report.csv` | Long-format bucketed CSV — Timeline → Export Report dialog |
| `GET /api/upstream/{product_steps,product_image.png,radar_steps,xband_scan.png,image_by_source.png}` | Thin proxy to radarca with archive fall-through |
| `GET /api/silences` / `POST` / `PUT /{sid}` / `DELETE /{sid}` | Silence CRUD; PUT for edit |
| `GET /api/admin/groups` etc. | Full group CRUD + `/preview` (next on-windows) |
| `GET/PUT /api/admin/thresholds` | Threshold blob editor |
| `POST /api/admin/thresholds/reprocess[/status|/cancel]` | Retroactive reprocess |
| `GET/POST/PUT /api/admin/alerts` | Alert routing config |
| `GET /api/users` | List (extended with `groups[]` per row) |
| `GET /api/push/{vapid_public,subscribe,unsubscribe,status,subscriptions,subscriptions/{id}/routing}` | Web Push |
| `GET /api/ws[?token=…]` | WebSocket stream (transition events only) |
| `GET /api/_debug/stats` | Runtime diagnostic snapshot |

OpenAPI is live at `http://localhost:8000/docs` (Swagger UI) and
`/redoc` — preferred over grepping `routes/*.py`.

---

## Stack at a glance

| Layer | Tech | Notes |
|---|---|---|
| DB | Postgres 16 | One database; ~18 tables. Uses pgcrypto for UUIDs. |
| Backend | Python 3.12, FastAPI, uvicorn | asyncio throughout. asyncpg, httpx, Playwright. |
| Image processing | Pillow + numpy + imagehash | CPU work runs in `asyncio.to_thread`. |
| Email | aiosmtplib | Async SMTP; config via `/admin/email`. |
| Auth | argon2-cffi + UUID sessions | Bearer-token in prod, cookie supported in dev. |
| Frontend | SvelteKit 2 + Svelte 5 (runes) | adapter-node in prod, vite dev in dev. |
| Mobile shell | `/m/*` routes + `hooks.server.ts` UA-sniff | Phone UA hitting `/` → 302 to `/m`. `manifest.webmanifest` + service worker scoped to `/m/`. PWA-installable on iOS. |
| Styling | Tailwind v4 | The `@theme` block in `app.css` defines tokens. |
| Map | MapLibre GL JS | Stadia Maps for basemap (free tier; domain-allowlisted). |
| Build / deploy | Docker + docker compose | Two Dockerfiles in `ops/`. |
| Reverse proxy (optional) | Traefik (or anything) | Frontend at `/`, backend at `/api/*`, WS at `/api/ws`. |
