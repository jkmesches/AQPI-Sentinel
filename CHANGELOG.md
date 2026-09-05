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

GHCR images are tagged correspondingly, **without the leading `v`** —
`docker/metadata-action` strips it. Pushing the git tag `v0.1.0` publishes
`ghcr.io/jkmesches/sentinel-{backend,frontend}:0.1.0` plus floating
`:0.1`, `:0`, and `:latest`. Pulling `:v0.1.0` fails with `manifest
unknown`.

---

## [Unreleased]

### Fixed

- **Nightly backups had been aborting since 2026-09-03.** Hardening the backup
  script in v0.2.0 moved `SENTINEL_BACKUP_DIR`'s default from the NFS share to
  `/var/backups/sentinel`, and `SENTINEL_BACKUP_MOUNT_ROOT`'s default from the
  share to `$DEST`. The cron line carried no environment and relied entirely on
  those defaults, so from the moment the change deployed the mount guard
  correctly refused to write to a non-mount — a path the same commit had just
  repointed at local disk. The guard worked exactly as designed; the defaults
  it was paired with did not. Two nights of backups were lost.

  Three things were wrong, and each would have caused this alone:

  - **`MOUNT_ROOT` defaulted to `$DEST`.** A share is mounted at a root and
    dumps live in a subdirectory beneath it, so the directory you name is
    almost never a mountpoint itself. It now resolves the filesystem `$DEST`
    actually lands on, and rejects the root filesystem explicitly — `/` is a
    mountpoint, so testing for one is not enough to catch a detached share.
  - **The guard was armed by default while the default destination was local
    disk.** Those two defaults contradict each other: a fresh install would
    abort every night with nothing misconfigured. Naming a directory now means
    "verify my storage is attached"; taking the default means "local disk is
    fine".
  - **`--check` wrote `status.json`.** It exits through the same `EXIT` trap
    that records outcomes, so the documented config-validation command wrote
    `status=ok` with a fresh timestamp and an empty artifact — and
    `layer0.self.backup`, which keys on status and age, reported *"last backup
    0.0h ago"*. Running `--check` reset the staleness clock, so an operator
    validating their config while real backups failed would have been told
    everything was fine indefinitely. `status.json` now records backup
    attempts only.

- **`layer0.self.backup` was watching nothing.** The check exists to make
  exactly this failure loud, and reported `skip — backup monitoring not
  configured` throughout, because no compose file mounted the backup directory
  at `/data/backups`. Both deploy stacks now bind it read-only via
  `SENTINEL_BACKUP_HOST_PATH` / `SENTINEL_BACKUP_PATH`, and the shipped example
  env files say to set it to the same path as the cron job.

  The `.env.deploy.example` cron recipe was also self-defeating under the new
  rule — it named `/var/backups/sentinel` explicitly, which arms the guard on a
  root-filesystem path. Rewritten to show the local-disk and network-share
  forms separately.

- **`ops/backup.sh` now has tests.** `validation_tests/test_backup_guard.sh`
  covers the guard matrix and the status-file discipline, and is verified to
  fail against each of the three regressions above.

## [0.2.1] — 2026-09-05

Alert-fidelity release. Three bugs that all pointed the same way: Sentinel
was reporting recoveries that had not happened, and re-announcing outages it
had already told you about. The lab team flagged alert frequency as their
main concern before deploying, and this is the answer to it.

Nothing here changes what Sentinel *detects* — every check, threshold and
timeline cell is untouched. It changes what counts as news.

### Fixed

- **Un-acking an alarm did not restore push paging.** `is_acked()` filters
  `revoked_at IS NULL`, but the deferred-push liveness re-check did a bare
  `EXISTS` on `alarm_acks`. An ack revoked during the delay window still read
  as acked, so the operator asked to be paged again and nothing arrived. The
  two paths now agree.

  Ack semantics are otherwise confirmed correct and are now covered by tests:
  `alarm_acks` is keyed on `alarm_id` with no user scoping, so an ack by
  anyone silences that alarm for everyone; and `engine._process` checks
  `is_acked` *before* resolving a route, so an acked alarm dispatches nothing
  through email, console or webhook — including `repeat_interval` re-sends,
  regardless of severity.

  Worth knowing, because it is not obvious: an ack binds to the alarm ROW,
  not to the check/target. Anything that closes and re-opens an alarm
  discards it. Production had acked `layer2.radar.XEBY` twice (alarms 27484
  and 27386); both were closed by a demoted skip, and all five subsequent
  re-opens arrived unacked and paged again. That is fixed upstream by the
  demoted-skip change above — with the alarm staying a single row, the ack
  now sticks.

- **A route keyed on `status_at_open` matched at open time and silently
  matched nothing at dispatch time.** `status_at_open` is written into the
  `alarms.payload` JSONB, but `_matches` does a flat `alarm.get(k)`. The two
  call sites disagreed about shape: `evaluate()` builds a flat dict to resolve
  the hold-down (matched correctly), while `_process()` passes the alarms row
  straight through (never matched). `_process` reads `route is None` as "no
  route configured" and returns, so the failure had no error, no warning and
  no log line — the only symptom was an empty `notification_log`.

  Production is configured with exactly one route, `{status_at_open: error}`.
  537 matching alarms opened in the 7 days to 2026-09-05 and **zero**
  notifications were dispatched; the newest row in `notification_log` predates
  the config change by months. `compute_severity` reads the same field, so
  duration-promotion to `critical` was dead for the same reason.

  Row and payload are now merged into the match shape once, in
  `flatten_alarm`. Real columns win over payload keys of the same name, so a
  stale payload can never steer a route keyed on `stage` or `severity`.

  **Operators upgrading past this fix should re-read their routes before
  restarting.** Any route keyed on `status_at_open` has been inert and starts
  firing — including its `repeat_interval`, which for a long-running outage
  means one notification per interval for as long as the alarm stays open.

- **A target that never recovers was re-alerting every few hours.** The
  scheduler demotes `fail`/`error` to `skip` when a dependency is unhealthy,
  so one upstream fault doesn't paint 37 downstream cells red. Three places
  then read that row as *good news*: `evaluate()` closed the alarm,
  `non_pass_streak_start()` reset the hold-down clock, and the stale-ACK sweep
  counted it as a clean run. A demoted skip is a decision **not to judge**,
  not an observation of recovery.

  The result was a cycle that looked exactly like flapping on things that were
  not flapping at all: alarm opens → upstream blips → alarm *closes*
  ("resolved!") → blip ends → hold-down re-arms from zero → a brand-new alarm
  opens and escalation restarts at step 1. Because Web Push fires on
  `alarm_open`, every lap through that loop was another notification.

  Measured over the 7 days to 2026-09-05, on targets that never came back:

  | check | alarms opened | closes caused by a demoted skip |
  |---|---|---|
  | `layer2.radar.XEBY` | 18 | 17 of 17 |
  | `layer1.product.water_depth` | 3 | 2 of 2 |
  | `layer1.product.max_water_depth` | 3 | 2 of 2 |
  | `layer1.product.water_level` | 3 | 1 of 1 |

  Not one of those closes was an actual `pass`. XEBY had been continuously
  non-pass since **2026-07-18** — seven weeks — while the hold-down query
  believed its problem had started 21 hours ago, because a dependency blip had
  reset the clock. Fleet-wide there were 3,274 demoted skips in the window.

  The ratio is the tell: a check that genuinely recovers sometimes has a low
  false-close rate (`XSCW` 6 of 93), while a permanently-down target has
  **100%** of its closes falsified. So the noise landed exactly where it was
  least informative and least welcome — on the things the operator already
  knew were broken.

  Recovery latency is unchanged. A `pass` still closes instantly, and so does
  an *intrinsic* skip — a check that ran and legitimately had nothing to
  assess. Only the three demote reasons (`upstream_unhealthy`,
  `local_dns_error`, `local_network_offline`) are now inconclusive, and they
  are declared once in `alarms.suppression` with a test asserting the
  scheduler still emits exactly those strings.


## [0.2.0] — 2026-09-03

Deployability release. v0.1.x was a system its author could run; this is
the first version another team can stand up, keep running, and trust the
alerts from. Every change below came from operating it or from auditing it
against an outside operator who has none of the context.

### Added

- **Upstream slow-episode correlation (`layer0.origin.episode`).** radarca does
  not fail to respond — 112 live probes of `/api/radar-status/` all returned
  HTTP 200. It answers slowly: steady-state p90 8–13s and rising, with episodes
  roughly half-hourly pushing the tail to 26–42s. Requests still waiting when
  an episode lands raise `ReadTimeout`, and they arrive in bursts: 153 of 268
  read timeouts over 7 days fell inside 20 minutes where 4+ checks timed out
  together, one burst covering 15 checks in a single minute. The new check is
  the single point of blame for those, so the operator gets one page naming the
  scope instead of fifteen. Nothing is hidden — every check keeps its own
  verdict and timeline cell; only the alarm collapses, via
  `alarm_only_depends_on`. `layer0.net.*` and `layer0.self.*` are exempt, so our
  own network and disk faults can never be excused by upstream.
- **`read_timeouts` counter** on `HttpClient`, exposed in `/api/_debug/stats`.
  Watch it against `total_requests`: a sustained rise means upstream's latency
  tail has moved and `DEFAULT_TIMEOUT_S` wants revisiting.

- **`docs/04-faq.md` — FAQ for the lab team.** Ten questions for people who
  use Sentinel's readings rather than its code: what it watches (public
  radarca APIs only — no privileged access), `fail` vs `error`, why per-radar
  thresholds differ, why five simultaneous red radars is one incident, whether
  history can change, and an explicit "what Sentinel does not do".



- **Upstream API errors were inflating the outage picture.** Investigation on
  2026-08-25 found 14.2% of check runs in a fail/error state, and traced it to
  three separate mechanisms rather than actual radar downtime.

  **Upstream latency changed regime on 2026-08-16.** Our own `latency_ms` p95
  went from ~2.4 s to 5.9–8.4 s and stayed there; a 25-probe live sample of
  `/api/radar-status/` measured p50 3.4 s, **p90 17.5 s**, max >25 s. The HTTP
  client's 12 s timeout — tuned when p95 was 2.4 s — sat *below* upstream's
  p90, so successful-but-slow responses became error ticks: 2–31/day before
  Aug 16, then 1,500–1,800/day. Timeout raised to 20 s, and idempotent GETs now
  retry once with jittered backoff on transport errors and 429/5xx (28% of all
  errors were isolated single ticks that the next run already recovered from).
  4xx other than 429 are not retried.

  **Six checks were fetching the same URL.** Each of the six radar checks
  independently `GET`s `/api/radar-status/` every cycle — 4,320 calls/day where
  720 suffice — so one slow response produced six simultaneous "radar
  unreachable" errors. Now a single short-TTL, single-flight memo shared across
  the fleet. Also, `_declared()` returned `None` both when the API failed *and*
  when it answered fine but omitted a radar, labelling both "radar-status API
  unreachable"; those are now distinct messages.

  **`error` was rendered as `fail`.** Both ranked equally in the history
  aggregation and shared `--color-fail`, and the header strips summed
  `fail + error` into a single "F". "We could not measure it" is not "it is
  broken." `error` now has its own rank (below `fail`), its own token
  (`--color-error`), and its own counter.

- **Disk-full outage: `latest_per_check()` no longer sorts the whole table.**
  Production stopped collecting from 2026-08-01 23:56 UTC to 2026-08-08
  17:44 UTC — 6 d 17 h — because the local disk filled. Root cause was a
  query/index mismatch: `Store.latest_per_check()` orders by `finished_at`,
  but the only index on `check_runs` was `idx_run_check (check_id,
  started_at DESC)`. With no usable index the planner chose Seq Scan + full
  Sort, and with `work_mem=4MB` / `temp_file_limit=-1` each call spilled
  ~1.4 GB into `base/pgsql_tmp`. The method is called from the scheduler
  tick, every alarm evaluation, and every `/api/status` poll; at 2.2 M rows
  the calls took ~2 min each, arrived faster than they drained, and stacked
  15 deep — 13 GB of temp files, disk full, Postgres wedged. At 25 k rows in
  May the same query sorted in memory in milliseconds, which is why it
  shipped unnoticed.

  Three independent defences now:
  - `idx_run_check_finished (check_id, finished_at DESC)` — the missing index.
  - The query is now a recursive loose index scan over the ~40 distinct
    `check_id`s instead of `DISTINCT ON`. Postgres has no skip scan, so
    `DISTINCT ON` reads every row and heap-fetches every tuple even with a
    perfect index — measured 6.8 s / 1.9 M buffer reads. The rewrite is
    **3.2 ms / 160 buffer hits**, and stays flat as `check_runs` grows.
  - `temp_file_limit=1GB` + `log_temp_files=64MB` on the Postgres service, so
    a future regression fails one query loudly instead of taking the host
    down.

- **Archive I/O no longer blocks the event loop.** `save_image()` and
  `lookup_by_source()` did synchronous `write_bytes`/`read_bytes`/`exists()`
  inline in async functions. Harmless on local disk; with the archive now on
  a `hard` NFS mount, a single NAS reboot would have blocked the entire
  backend — every check, the API, and the WebSocket fan-out — not just
  archiving. Both paths now go through `asyncio.to_thread`.

- **Unbounded container logs.** No `logging:` block existed in
  `docker-compose.prod.yml`, so the json-file driver grew one file forever;
  the backend's had reached 1.6 GB. All three services now rotate at
  50 MB × 3.

- **`layer2.xband.fleet` — fleet correlation check.** Over 14 days, **80.2%**
  of `GHOST_UP` runs occurred while 4–5 X-band radars were ghosting
  simultaneously; only **2.8%** were isolated. The largest episode had XSWR,
  XSCR, XSCV and XSCW entering `GHOST_UP` at `2026-08-13 12:29:52` and leaving
  at `2026-08-16 19:29:54` — sub-second alignment across four sites, 79.0 hours
  apart. Radars at separate sites do not fail in lockstep; that is one upstream
  event being counted as four multi-day radar outages.

  The check fails when ≥4 of 5 X-band radars are unhealthy at once, and the
  per-radar X-band checks now list it in `depends_on` so the existing
  dependency-suppression machinery records their alarms but suppresses the
  duplicate notifications. An isolated radar failure is unaffected and still
  pages. `layer2.radar.CBAND` deliberately does not depend on it — different
  band and site, and it stayed healthy (2,301 passes) right through the
  episodes above, which is what proved the API itself was fine.

  Note this makes the verdicts *more* accurate, not quieter for its own sake:
  the 79-hour episode was a real data outage. It was simply one of them.

- **`validation_tests/test_api_error_handling.py`** — 21 assertions over retry
  semantics, fetch sharing, and fleet correlation. The load-bearing cases are
  the negative ones: a retry that still fails must not count as a save, an
  isolated radar failure must still page, and CBAND must never be suppressed by
  an X-band event.

- **Retention / cold-storage offload** (`backend/retention.py`). A daily
  sweep at `SENTINEL_RETENTION_HOUR_UTC` (default 09:00) exports
  `check_runs` + `metric_samples` rows older than
  `SENTINEL_DB_RETENTION_DAYS` to gzipped CSV under `SENTINEL_COLD_ROOT`,
  then drops them. **Offload, not delete**: the export is fsync'd and its
  COPY row count verified against the count about to be removed, and any
  mismatch aborts before the delete. Sweeps are bounded by a snapshot taken
  before the export, so rows written mid-sweep are never in its delete
  predicate. Production: 60 days. Guarded by
  `validation_tests/test_retention_offload.py`.

- **`SENTINEL_ARCHIVE_RETENTION_DAYS` now does something.** It has been
  parsed into `Settings.archive_retention_days` since the archive feature
  shipped and read by *nothing* — a documented knob that silently did
  nothing. It now drives `retention.prune_archive()`, keyed on `last_seen_at`
  so recurring content stays live. Production leaves it unset (permanent);
  the archive lives on 2.5 TB of NFS and there is nothing to gain by pruning.

- **`layer0.self.disk`** — Sentinel monitors its own host. Reports local-disk
  and archive-mount headroom every 5 min, warning at 75% and failing at 88%
  (`disk_warn_pct` / `disk_fail_pct` in the global thresholds). A hung NFS
  mount is reported as a warn via a 10 s statfs timeout rather than hanging
  the check. This check exists because in August 2026 every radar check was
  green while the box hosting them ran out of disk.

- **`ops/backup.sh`** — nightly `pg_dump | gzip` to the NFS share, keeping
  the newest 14, with a gzip integrity check and `.part`-then-rename so a
  truncated dump is never mistaken for a good one. Runs at 08:00 UTC, an
  hour ahead of the retention sweep, so every night's backup predates the
  offload that removes rows.

- **Data mounts are now configurable**, via `SENTINEL_ARCHIVE_HOST_PATH` /
  `SENTINEL_COLD_HOST_PATH`. Both accept a bare docker volume name (dev
  default) or an absolute host path (bind mount). Production points them at
  `/mnt/aqpi-data/{archive,cold}` on the Erebor NFS share so the container
  disk stays light. `pgdata` deliberately stays on local disk — Postgres
  needs fsync/locking semantics NFS doesn't reliably provide.

- **`docs/MAINTENANCE.md` "Disk space"** — storage layout, the full
  post-mortem above, retention/offload configuration, how to restore
  offloaded rows, backups, and what to do when `pgsql_tmp` starts growing.

- **Map: per-radar tilt selection.** When exactly one X-band radar is
  active, a "Tilt" dropdown appears in the Layers panel listing that
  radar's scan elevations (e.g. XSWR: 2.5/3.5/4.5/5.5°). Picking an
  elevation overlays the corresponding PPI from the CSU Web Radar Display
  (radardisplay.engr.colostate.edu), replacing radarca's single
  pre-rendered sweep for that radar. radar-display only carries Z / V /
  ρhv, so Zdr + ΦDP tabs disable while a tilt is engaged. The time strip
  rebinds to radar-display's 7-frame loop (~14-min window, ~2-min
  cadence) so play + scrub animate the chosen tilt; engaging a tilt
  turns the composite off since the two run on different timebases. New
  backend proxies `/api/upstream/tilt_steps` (all 7 frame timestamps,
  parallel-fetched) + `/api/upstream/tilt_image.png`, behind a dedicated
  verify=False client (radar-display's TLS cert is expired). CBAND +
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

- **Read timeouts are no longer retried.** A `ReadTimeout` means upstream took
  the connection and went quiet — alive but saturated. We have already cost it
  a full timeout, and the retry lands while it is still struggling; measured
  counters said it rescued about 1 in 9 while doubling our wall cost per cycle
  (20s → 40.5s on a 120s cadence). `ConnectTimeout`, `ConnectError` and 5xx
  stay retryable — those cost upstream nothing.
- **The check fleet is de-correlated.** Checks looped on a fixed period, so any
  set starting together stayed in lockstep forever: 10–15 checks landing in the
  same second was routine and 33–34 happened. Added ±5%-of-cadence zero-mean
  per-cycle jitter (floor 2s), and widened the initial topological stagger from
  2s to 8s per rank with the within-rank spread filling the whole step. Ranks
  still start strictly in order. Measured in production, excluding the boot
  transient: seconds carrying 8 or more concurrent checks fell from **17.0% to
  1.3%**, and the mean from 3.44 to 1.50 checks/second. The *worst* single
  second is unchanged (13 → 14) and that is expected — jitter makes phases
  drift, so occasional coincidences still happen; what it removes is the
  *sustained* lockstep, which is what was converting upstream's slow episodes
  into our timeouts.


- **Sparkline: hybrid value + flow representation.** The pure
  count-per-bucket rewrite from 2026-05-19 fixed the outage-spoofing
  failure mode but homogenized every check's trace under normal
  operation (all sparklines became the same low-amplitude wave). The
  bucket loop now plots per-bucket mean of `value` for non-empty
  buckets, with empty buckets still dropping to baseline. Per-check
  signal returns; outage-detection behavior preserved.

### Fixed

- **An image-fetch read timeout was reported as a broken product.** The image
  fetch set `F_image_exists=fail` on *any* exception, rolling the product up to
  `fail` — a red cell asserting the image is missing when all we knew was that
  upstream did not answer in time. The manifest fetch on the same check already
  returned `error` for the identical cause, so the verdict depended only on
  which of the two fetches the slowness happened to land on. Now returns
  `error` + `reason=upstream_api`. Applied retroactively to 121 rows spanning
  2026-06-18 → 2026-08-31; the as-observed verdict is preserved under
  `payload.original`, and re-running the job changes nothing.
- **The episode detector undercounted**, seeing 10 of 14 concurrent timeouts,
  because product checks handle their own image-fetch errors and never reached
  the scheduler's handler. Both product fetch paths now feed it.
- **`payload.image_error` recorded an empty string** — `str()` on an httpx
  `ReadTimeout` is `""`. The exception class is captured alongside it.

- **GHOST_UP thresholds were measuring the wrong quantity.** The check gates
  on `now - newest_published_timestamp`, which includes upstream's
  **publication lag** (~300–500 s on this fleet), but `RADAR_SILENT_FAIL_S`
  was calibrated from *inter-scan cadence* (~120 s). Several thresholds were
  therefore impossible to satisfy: XSWR scans every 120 s and delivers ~27
  images per poll — a healthy radar — yet reported `GHOST_UP` on **96%** of
  runs in the 24 h to 2026-08-26, because its 240 s threshold sat below the
  publication lag alone.

  Recalibrated from 1.5× the observed p99 of `primary_age_s` over 24 h of
  healthy operation: XSCV 600→660, XSCW 720 (kept), XSCR 300→**780**,
  XSWR 240→**660**, CBAND 600→**1080**, XEBY 300 (kept).

  **This cannot hide an outage.** A radar publishing no images is not-fresh
  regardless of threshold (`primary_n == 0`), and 5,042 of XSWR's 7,950
  GHOST_UPs over 14 days were exactly that. Thresholds govern only the
  "images present but stale" case, where the cost is detection latency — a
  frozen XSWR feed is now caught in 11 minutes instead of 4.

  Applied retroactively: 231,430 rows evaluated, **6,406 reclassified**
  (all `GHOST_UP` → `HEALTHY`; XSWR 2,900, XSCR 2,401, CBAND 1,056, XSCW 43,
  XSCV 5, XEBY 1). Verified after: zero-image ghosts 37,872 before and after,
  no rows deleted, all originals preserved, and the 79-hour 2026-08-13 fleet
  episode still recorded in full across all five radars.

- **`tune_silent_fail` perpetuated the same error.** It recommended from max
  inter-scan gap while computing — and printing — the newest-image age it then
  ignored. Worse, it recommended 480 s for CBAND, *below* CBAND's observed age
  p99 of 717 s, which would have started false-firing a healthy radar. Now
  recommends from `max(gap, age)` and flags `[lag-dominated]` radars.

- **L2 reprocessing was a silent no-op.** `_reverdict_l2` read
  `payload["reconcile"]`, a key `layer2_radar` has never emitted, so every row
  returned `None` — which is where the belief that "historical L2 can't be
  reprocessed" came from. Verified: 0 of 231,356 rows carry `reconcile`,
  212,572 carry `observed`. Rewritten against the real shape, preserving the
  original verdict/status/threshold under `payload.original` (first one wins
  across repeated runs) and refusing outright to touch a zero-image row.

- **L2 payload recorded the wrong threshold.** It stored `self.silent_fail_s`
  (the constructor default) rather than the live threshold the run was gated
  on — the field read 240 while `silent_fail_band` read `[594, 726]`. That
  disagreement would have fed a wrong "original" threshold into the reprocess
  audit trail.

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

[Unreleased]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jkmesches/AQPI-Sentinel/releases/tag/v0.1.0
