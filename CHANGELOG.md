# Changelog

All notable changes to AQPI Sentinel are documented here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Versioning

Sentinel uses `vMAJOR.MINOR.PATCH`:

- **MAJOR** (`vX.0.0`) — **Production release.** Stable, recommended for
  deployment by teams outside the core maintainers. Breaking changes
  bump the major.
- **MINOR** (`v0.X.0`) — **Milestone / feature drop.** New views, new
  check types, new admin surface. Backward-compatible within a major.
- **PATCH** (`v0.0.X`) — **Bug fix / small change.** Copy tweaks, UX
  polish, performance fixes that don't introduce features.

Pre-1.0 the major stays at 0; minor bumps signal feature drops, patch
bumps signal fixes.

GHCR images are tagged correspondingly: pushing `v0.1.0` publishes
`ghcr.io/jkmesches/sentinel-{backend,frontend}:v0.1.0` plus floating
`:0.1`, `:0`, and `:latest`.

---

## [Unreleased]

### Added

- **Map: per-radar tilt selection (Phase 1).** When exactly one X-band
  radar is active, a "Tilt" dropdown appears in the Layers panel listing
  that radar's scan elevations (e.g. XSWR: 2.5/3.5/4.5/5.5°). Picking an
  elevation overlays the corresponding PPI from the CSU Web Radar Display
  (radardisplay.engr.colostate.edu), replacing radarca's single
  pre-rendered sweep for that radar. radar-display only carries Z / V /
  ρhv, so Zdr + ΦDP tabs disable while a tilt is engaged. Phase 1 shows
  the newest frame only; scrubbing is Phase 2. New backend proxies
  `/api/upstream/tilt_steps` + `/api/upstream/tilt_image.png` (dedicated
  verify=False client — radar-display's TLS cert is expired). CBAND +
  NEXRAD aren't in that directory, so the control never appears for them.

- **Map: Stream gauges (NWM USGS sites).** Fourth toggle in the
  Geography section. Renders the 468 USGS sites parsed from radarca's
  `stream_data.csv`, with two tiers: R-status (52 real-time sites,
  larger filled blue circles) and B-status (416 basic sites, smaller
  faint dots). Click a marker → MapLibre popup with the COMID; for
  R-status sites it fires the per-COMID forecast + observed fetches
  and patches the popup body in once the values land. New backend
  endpoints `/api/upstream/stream_gauges` (parsed CSV, 1h
  server-side cache) and `/api/upstream/stream_data` (proxy for the
  per-COMID time-series).
- **Map: Geographic reference layers.** Two new toggles in the Live
  map's Layers panel under a new "Geography" section, both persisted
  per browser:
  - **Watersheds** — HUC-8 subbasin outlines for NorCal, sourced from
    the USGS Watershed Boundary Dataset, simplified to ~1 MB and
    served as a static GeoJSON asset.
  - **Reservoirs** — fifteen flood-relevant NorCal dams (Shasta,
    Oroville, Folsom, New Bullards Bar, Don Pedro, Berryessa,
    Trinity, New Melones, Camanche, New Hogan, Englebright, Indian
    Valley, Whiskeytown, Black Butte, San Luis) rendered as labeled
    point markers.
- **Map: Terrain hillshade toggle.** Third Geography toggle —
  hillshade from AWS Open Data terrarium-format DEM tiles. Inserted
  below the dynamic raster overlays so CoSMoS water-depth composites
  render on top of terrain shading, giving an inundation-vs-topography
  view. Free tiles; no API key.
- **Map: CoSMoS composites.** The Composite picker grows a new
  "CoSMoS (Bay)" group with Water Depth · Water Level · Max Water
  Depth · Max Water Level. The picker's per-composite extent table
  already supported the Bay-only extent; just had to surface the four
  hydro products there.

### Changed

- **Sparkline: hybrid value + flow representation.** The pure
  count-per-bucket rewrite from 2026-05-19 fixed the outage-spoofing
  failure mode but homogenized every check's trace under normal
  operation (all sparklines became the same low-amplitude wave). The
  bucket loop now plots per-bucket mean of `value` for non-empty
  buckets, with empty buckets still dropping to baseline. Per-check
  signal returns; outage-detection behavior preserved.

## [0.1.2] — 2026-05-19

**Severity model reshape.** The status→severity mapping was lossy:
warn-status (degraded), fail-status (broken), and error-status (check
crashed) all opened at severity=warn, making `severity_floor` unable
to distinguish "degraded" from "broken." Reshaped so the three
routing tiers map onto operational priority.

### Changed

- **Status → severity mapping**:
  - `warn` → `info` (was `warn`) — "attention required, degraded but not broken"
  - `fail` → `warn` → `critical` after 30 min — "action required, broken"
  - `error` → `warn` → `critical` after 30 min (was warn with no promote) — same tier as fail; check itself crashed is also a broken state
- **`severity_floor` semantics** now align with operational tiers:
  - `info` = notify on everything
  - `warn` = notify on broken only (fail / error)
  - `critical` = notify only on long-running outages (broken > 30 min)
- **Per-check verdict adjustments** (`docs/93-severity-audit.md`):
  - `layer0.website.root_notfound`: fail → warn. Marker miss means upstream restructured the not-found page; service still works.
  - L1 product `parity` sub-check: fail → warn. Upstream HRRR pipeline glitches are data-quality issues, not outages.
- **Push routing editor copy** in both surfaces now reads with the new vocabulary: "All / Broken only / Long outages" rather than "info+ / warn+ / critical only."
- **`/admin/alerts` cheat-sheet** rewritten to surface the three tiers (Action / Attention / Informational) directly.

### Added

- **`docs/93-severity-audit.md`** — full per-check classification
  table. Source of truth for "what tier does this check land in
  when it trips."
- **"My devices" link** in the admin sidebar (mirrors the auth-chip
  link). Users were looking in admin first; the redundancy wins.
- **Click-friendly match-pattern picker** on `/settings/devices`.
  Ports the mobile editor's chip grid: pre-populated radar +
  product chips, click to toggle. Products grouped by category
  (Radar Data / Atmospheric Forecast / CoSMoS / NWM) to match the
  home page.
- **Custom pattern fallback** retained as a collapsible
  `+ add custom pattern` details block in the chip picker.

### Notes for operators

Existing alarm rows keep their `severity` value — the reshape
applies to **newly-opened** alarms only. Existing routing rules
that filter on `status` continue to work unchanged.

If you previously set `severity_floor: warn` expecting "all alarms"
(because pre-v0.1.2 everything opened at warn), you now have
"broken only" behavior. Switch to `severity_floor: info` for the
old all-alarms behavior, or keep the new default if you only want
to be paged on broken states.

## [0.1.1] — 2026-05-19

Patch release. Comprehensive documentation site, smart-delay push
notifications, and a handful of UX polish fixes that fell out of
real operation against the live deploy.

### Added

- **MkDocs Material documentation site** at
  `https://jkmesches.github.io/SentinelProject/`. Thirteen docs
  organized into Operate (getting-started, deployment,
  administration, maintenance, troubleshooting, porting), Develop
  (architecture, extending-checks, extending-api, extending-ui,
  alarm-engine), and Reference (env-vars, glossary,
  release-process, changelog). Builds + deploys on every push to
  `main` via `.github/workflows/docs-publish.yml`.
- **Screenshot capture script** (`scripts/capture_admin_screenshots.py`)
  using Playwright. Logs in via the standard auth flow + snapshots
  every admin page on desktop + the mobile shell. Re-runnable
  whenever the UI shifts.
- **Smart-delay push semantics.** `delay_s` now re-checks the alarm
  state before firing — if the alarm self-resolves or is acked
  during the wait, the notification is dropped. Was previously
  unconditional, which paged operators for transient flaps that
  had already cleared.
- **Version display in footer** on both desktop and mobile shells.
  Single source of truth at `backend/_version.py`, exposed via a
  new `/api/version` endpoint.
- **Devices link** in the desktop top-right auth chip — surfaces
  `/settings/devices` (per-device push routing) without users
  having to type the URL.
- **CHANGELOG cross-reference** in the docs site Reference section.

### Changed

- **FastAPI auto-docs** moved from `/docs` + `/redoc` to
  `/api/docs` + `/api/redoc` + `/api/openapi.json`. Routes
  consistently through the `/api/*` reverse-proxy convention.
- **Footer link** swapped from `github.com/jkmesches/SentinelProject`
  (the repo is private — link 404'd for everyone except the owner)
  to the public documentation site.
- **Push routing copy** in both editor surfaces (mobile +
  /settings/devices) updated to reflect smart-delay behavior:
  *"If the alarm self-resolves or is acknowledged during the wait,
  the notification is dropped."*
- **MAINTENANCE doc** gained four new sections: performance tuning,
  rolling back a release, upgrading between versions, monitoring
  Sentinel itself, writing a one-shot data migration.

### Fixed

- **Category status colors** on the mobile home page and desktop
  stage strip fell through to `'pass'` (green) when every row was
  skip — e.g. all L1 products cascade-demoted from a single L0
  failure made the L1 dot misleadingly green. Now falls through to
  `'skip'` (gray) when nothing is actually healthy.
- **Mobile sticky header** drifted as content scrolled because
  `.mob-shell` used `min-height: 100vh` instead of fixed
  height — letting the shell grow past the viewport so the body
  scrolled instead of `.mob-main`. Now fixed-height with internal
  scroll; the header tracks correctly.
- **MkDocs strict-build** would have failed on `pygments 2.20.0`
  due to a known incompatibility with `pymdownx.highlight`. Pinned
  `pygments<2.20` in `docs-requirements.txt`.
- **Screenshot capture** initially landed `[ADMIN ONLY · sign in]`
  stubs for every admin page because the script wrote the auth
  token under the wrong localStorage key (`sentinel-token` vs the
  frontend's `sentinel.token`). Fixed; also switched mobile
  captures to viewport-only so timeline/history don't produce
  127-megabyte full-page PNGs.

### Security

- **Force-pushed history rewrite** of commit `48495a5` to remove
  un-pixelated admin screenshots containing user emails + check
  identifiers. Pixelated versions replaced them in `9ef56af`. The
  unreferenced blobs are awaiting GC on GitHub's side.

## [0.1.0] — 2026-05-19

First tagged release. Sentinel is feature-complete for the
radarca.engr.colostate.edu monitoring scope.

### Added

- **38-check monitoring inventory** spanning five stages:
  - L0 Connectivity — Origin reachable, Public dashboard page, Root
    URL (404 check), TLS certificate, Sentinel Internet, Sentinel DNS.
  - L1 Product Freshness — image manifests, scan counts, freshness
    thresholds, sub-check verdicts for 12 product types.
  - L2 Radar Scans — per-radar reconciliation between declared status
    and observed imagery; ghost-up + confirmed-down + stuck-flag detection.
  - L3 Map Overlays — Playwright-driven page-load probe of the overlay
    pipeline with cross-product parity checks.
  - L4-T1T2 Image Quality — per-radar + per-mosaic captured-PNG QC
    (coverage, autocorrelation, content extent).
- **Alarm engine** with routing rules, escalation policies, recipient
  groups (with weekly/biweekly schedules + one-off and recurring
  downtime), email + push + webhook sinks, dependency-chain suppression,
  per-step recipient dedup, ack/unack lifecycle, alarm-promote on
  duration.
- **Admin surface**:
  - `/admin/thresholds` — DB-backed threshold registry with retroactive
    reprocess (re-classifies historical check_runs under new thresholds).
  - `/admin/groups` — schedule editor (weekly / biweekly / always +
    one-off + recurring downtime), parent-chain AND-merge of group
    schedules.
  - `/admin/alerts` — routing rules editor, recipient table with
    group-based dispatch.
  - `/admin/silences` — UTC↔Local toggle, custom matcher presets, edit
    flow.
- **Web Push notifications** with per-device routing:
  - severity floor, product patterns (with L0/canary always-passed),
    on-duty schedule, delay window.
  - Settings UI at `/m/push-settings` (mobile) + `/settings/devices`
    (desktop).
  - VAPID auto-generated + persisted on first use.
- **Mobile shell** at `/m/*`:
  - Status (`/m`), Timeline (`/m/timeline`), Uptime
    (`/m/uptime` with focus mode + cascade-aware `↑` badges), Alarms
    (`/m/alarms`), History (`/m/history`), More (`/m/more`), Push
    settings (`/m/push-settings`).
  - **iOS + Android parity**: Add-to-Home-Screen on iOS Safari;
    programmatic install on Android Chrome via `beforeinstallprompt`
    capture; both platforms get the same notification + drilldown +
    uptime grid experience.
  - Service worker scoped to `/m/`, manifest with maskable icons.
- **Cascade-demote**: when an upstream dependency is unhealthy,
  downstream checks demote to `skip` with a friendly summary
  (`Upstream "X" unhealthy`) instead of painting cells red
  independently. Race-condition guards: topological stagger on cold
  start (`rank * 2s`) plus per-tick await-upstream-settled gate
  (bounded 10s).
- **Time-based sparklines**: x-axis is wall-clock time, y-axis is
  sample-arrival rate per cadence-sized bucket. When data flow stops,
  the trace falls to zero rather than freezing. Inline label
  `N/window` adapts to each check's cadence.
- **Captured-image drilldowns**: every mobile drilldown (Status,
  Timeline, Uptime, History) surfaces the most-recent L4 PNG in
  context, with a lazy-loading guard.
- **History data model** persists original status + summary under
  `payload.original_*` whenever a row is demoted, so the raw
  observation stays inspectable in the drilldown.
- **CI**: GitHub Actions workflow builds + publishes backend +
  frontend images to GHCR on every push to `main` and on every
  `vX.Y.Z` tag. Matrix build, GHA buildx cache, no manual secret
  setup. Companion `ops/docker-compose.ghcr.yml` for pull-based
  deploys.

### Changed

- **Backend error messages humanized.** `ConnectTimeout: ` → `Connection
  timed out`, `gaierror` → `DNS lookup failed`, `SSLError` → `TLS
  handshake failed`. Applied to scheduler, upstream proxy routes, and
  every check module. Original class names preserved in
  `payload.exception` for diagnostics.
- **L0 dependency tree** so a single root failure cascades cleanly:
  ```
  Sentinel Internet ─┐
                     ├─→ Origin reachable ──┬─→ Public dashboard page
  Sentinel DNS ──────┘                      ├─→ Root URL (404 check)
                                            ├─→ TLS certificate
                                            └─→ (all L1/L2/L4)
  ```
- **`/admin/alerts` clarified**: severity-vs-status cheat-sheet block,
  dropdown for custom matchers, time-of-day field hints, UTC labeling
  on every time picker.
- **Drilldown footer convention**: each mobile view's drilldown
  surfaces the two "broader" lenses (Show in Timeline / Uptime /
  History), suppressing the self-link. Cross-jumps have per-context
  time windows.
- **Desktop home left rail**: Site panel now sits above Radars, so the
  root-cause tier is at eye level.

### Fixed

- **Missed push notifications during cascading outages**: per-device
  `product_patterns` allowlists silently dropped L0 connectivity
  alarms — exactly the alarm class operators most need. L0 + canary
  alarms now bypass pattern filtering unconditionally.
- **WebGL context loss on mobile**: iOS Safari freely drops the
  context (backgrounding, memory pressure); subsequent `getLayer()`
  calls threw `this.style is undefined`. Now caught with a
  user-visible "Reload map" CTA on both mobile + desktop maps.
- **Race conditions in cascade-demote** closed via topological
  stagger + await-upstream-settled gate.
- **`/api/upstream/product_steps`** returned `500` with an httpx
  traceback on upstream timeouts; now returns a clean `502
  {"detail":"Upstream unavailable: Connection timed out"}`.
- **Day-separator on `/m/uptime`** was horizontally clipped when the
  grid scrolled; now `position: sticky; left: 0` so the
  TODAY/YESTERDAY label tracks viewport-left.
- **Time-input labeling**: every datetime-local + time picker across
  the admin surface now explicitly shows `(UTC)`.
- **Orphaned check_ids** (e.g. `layer0.net.control` after the
  Internet/DNS split) no longer ghost-appear in `/api/status`; the
  rollup now filters by the live registry. Timeline history keeps
  all rows.

### Migrations (one-shot)

- `scripts/humanize_history.py` — rewrote 2,294 `check_runs.summary`
  + 70 `alarms.message` rows that were generated under the old
  exception-class-name format. Originals stashed in
  `payload.raw_summary` / `payload.raw_message`; idempotent via
  `payload.humanized_v=1`.
- `scripts/cascade_retro.py` — applied cascade-demote to 3,212
  historical fail/error rows by walking the dep tree at each row's
  `finished_at`. Originals preserved in `payload.original_status` /
  `payload.original_summary`; idempotent via
  `payload.cascade_retro_v=1`.

[Unreleased]: https://github.com/jkmesches/SentinelProject/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/jkmesches/SentinelProject/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/jkmesches/SentinelProject/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jkmesches/SentinelProject/releases/tag/v0.1.0
