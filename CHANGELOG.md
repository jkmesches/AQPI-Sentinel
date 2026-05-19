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

_Nothing pending._

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

[Unreleased]: https://github.com/jkmesches/SentinelProject/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jkmesches/SentinelProject/releases/tag/v0.1.0
