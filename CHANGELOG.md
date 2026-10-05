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

`LB2` stops measuring a downstream generator and starts measuring radar
arrival; a new `LB3` stage watches the processing in between; and the radar
rail collapses a radar's several readings onto one expandable row.

### Added

- **`LB3` — Backend Processing.** LB2 says a radar's data is landing, LB1 says
  a product is publishing, and until now nothing looked in between — so a
  product could be computed from fewer radars than it claimed with both stages
  green. On 2026-10-05 XEBY aged out of the AQPI composite between 16:16Z and
  16:44Z and nothing anywhere reported it.

  - **`layer3.composite.<radar>`** — one per radar, paging. Reads the
    composite's **own per-run log** and reports whether the radar has a block
    in the latest run (`A_included`) and how old its contribution was
    (`B_fresh`).

    `GetNetCDFdim` with real dimensions means the composite opened the file and
    read its header, so a block is evidence of **ingestion** — the check says
    "in composite", not "offered to" it. That also exposes a third state:
    a block with *no* dimensions means the composite tried this file and
    failed on it, which is worse than absence and reported as `NAMED BUT NOT
    READ`. These files are written once per run, 120 s apart, so reading the
    **second-newest** is always a complete file — the torn-read problem is
    retired rather than defended against, at the cost of ~240 s of reporting
    lag against a 900 s band.

    `secondsStarttoEnd` is used as the contribution age, but its label says
    start-minus-*end-of-scan* — a duration. The evidence that it is an age is
    that one radar reaches 64,828 s, impossible for a 4-sweep volume. Sound,
    but an inference, so the check derives an age independently and **flags a
    disagreement** rather than silently banding the wrong quantity.

    The input receipt is retained as a **fallback**, tagged in the payload
    rather than substituted silently, because it supports only the weaker
    claim. It records intent, not outcome, and carries three parsing hazards
    plus a biased torn-read window — all documented in
    `docs/07-deployment-profiles.md`.

  - **`layer3.backend.drops`** — one check, **informational and non-paging**.
    Watches the QPE producer that LB2 was pointed at by mistake. Capped at
    `warn` so it cannot auto-promote, *and* routed to a non-escalating policy
    by check id — two independent statements of one intent, because one of them
    will eventually be edited by someone unaware of the other. One check for
    the whole producer, not one per folder: the folders freeze together because
    it is one process.

- **Backend fleet correlation** (`layer2.backend.fleet`). The L2 design ported
  to the filesystem side, where it is a **stronger discriminator**: CBAND
  arrives on a different tree on a different mount, so a passing CBAND is
  positive evidence — host up, NFS serving, clock sane, our own reads working —
  and localises the fault to the X-band path. The radarca-derived check cannot
  separate "the radars stopped" from "the API we ask about them stopped". The
  home page shows the backend verdict in preference to the radarca one.

  Three scopes, and the third is not a variant of the second: `xband-path`
  (CBAND arriving), `wider-than-xband` (CBAND silent too), and `unlocalised`
  (CBAND's verdict unavailable, so the fault *cannot* be localised this cycle —
  saying otherwise would be a claim we did not earn).

  Registers only where a fleet exists. On `xqpi`, with one radar, "is 3 of 1
  silent" is unreachable, so it is declined with a stated reason rather than
  rendering a permanent skip row that reads as a broken check.

- **Grouped radar rows.** Each radar now reports through up to three checks, so
  the rail collapses them onto one expandable row showing the **worst** reading
  — and the worst reading's own summary text, so expanding never contradicts
  what the collapsed row said. Expands automatically when alerting, with manual
  collapses recorded separately so a newly-alerting radar cannot overwrite a
  deliberate choice. `Expand all` / `Collapse all` on the rail header.

  The collapsed row's sparkline plots **data arrival specifically** (LB2 where
  it exists). LB3 measures a receipt written two steps later and L2 measures
  what radarca says, so plotting whichever came first would make the trace mean
  a different thing on different rows.

- **Collapsible stage blocks on the timeline**, with `collapse all` /
  `expand all`. The Radars tab now carries five stages; scanning one of them
  meant scrolling past the others.

### Changed

- **Contribution-age bands are one uniform pair (warn 600 s / fail 900 s), not
  per radar.** The first design derived them from each radar's own silence
  limit, which is principled and does not work. Measured over 1,440 composite
  runs and 8,298 contributions, that rule is **blind on XSCR** — observed max
  403 s against a 1,080 s band, on the one radar that is never late — while
  firing on 25% of XSCW's runs. The derivation inherited a mismatch:
  `RADAR_SILENT_FAIL_S` was fitted to radarca *reporting* cadence, so building
  a composite-ingestion band on it is a borrowed figure one level removed.

  A percentile band is worse — 2x p90 hands XSCW a 36-hour limit, because its
  p90 *is* the pathology.

  The decisive argument is that **the per-radar variation is the signal**: a
  band tuned to each radar's own history silences precisely the radars that
  misbehave, encoding XSCW's 18-hour staleness as normal for XSCW. Under one
  uniform band XSCR and SSCB stay quiet because they are never stale, and
  XSWR/XSCW/XSCV are loud because they are.

  The ~8% expected fire rate measures the **missing staleness guard on
  granite**, not this threshold: the composite applies none to its radar
  inputs, so an 18-hour-old volume gets blended into a composite reporting
  itself current. Add the guard and the tail disappears. Stated in the check's
  docstring so nobody tunes the detector instead of fixing the defect.

### Fixed

- **The fleet correlation checks reported `pass` from stale verdicts**, during
  exactly the incident they exist to characterise.

  `_publish_verdict` has one call site, at the end of the per-radar `run()`.
  The early return taken when radarca's `/api/radar-status/` is unavailable is
  the only return above it and never reaches it — so while the origin is
  unreachable, every per-radar check publishes **nothing**, every cycle. The
  verdict cache keeps the pre-outage entries forever; after the 300 s TTL the
  numerator (`_not_reporting_xband`, TTL-gated) empties while the denominator
  (`known`, raw membership) still counts five, so the skip floor does not
  trigger and the check reports `0/5 not reporting — pass`.

  About five minutes into a radarca outage the operator therefore saw **five
  red radar rows above a green fleet row** asserting nothing was systemic.
  Both fleet checks now gate the denominator by the same TTL as the numerator
  and report `skip` — no usable evidence, so assert nothing.

  !!! note "Fleet uptime has a step change at this commit, and it is the fix"
      Rows that read `pass` during a radarca outage now read `skip`. Any
      comparison of fleet uptime across this point is apples to oranges in
      that specific direction.

  Suppression is unaffected: the per-radar checks list the fleet in
  `alarm_only_depends_on` and `compute_suppression` keys on `fail`/`error`, so
  a fleet reporting `skip` where it reported `pass` suppresses nothing it did
  not suppress before. The fix cannot accidentally mute five radar alarms.
  Historical rows are also untouched — `_reverdict_l2` requires
  `payload["observed"]`, which fleet rows do not carry, so a reprocess pass
  preserves them rather than retroactively rewriting them.

- **The mobile status map coloured radar markers from `L2` alone.** Two
  consequences: on a profile with no radarca origin — `xqpi`, where FLOW is
  monitored entirely off the filesystem — every marker rendered in the `skip`
  colour on a working deployment; and on `aqpi` a radar could be silent on disk
  while radarca still claimed it was fine, and the map drew it green. Now the
  worst reading across `L2`, `LB2` and `LB3`.
- **`LB1`/`LB2` had no alert route** and fell through to the catch-all at 4h,
  so the *authoritative* filesystem-read observation repeated four times less
  often than the radarca-derived one it exists to outrank. An omission when
  those stages were added in v0.5.0, not a decision.
- **The docs FAQ stage table was missing `LB1` and `LB2` entirely**, and asked
  about "the five stages" when there were seven. Both surfaces now list all
  eight.

- **`LB2` measured the wrong thing on `aqpi`.** The five X-band checks read
  `PRODUCTS/DROPS/<folder>`, which is the **output** of `Gen_X-band_QPE.py` —
  one processing step downstream of the radar. So each check answered "is the
  QPE generator alive", not "is this radar delivering".

  On 2026-10-05 that generator stopped at 04:07 UTC and all five X-band
  checks failed for 12 hours. Three of the five radars — XSCR, XSCV and XSWR
  — were arriving within **one minute** the entire time. Measured at 16:25Z:

  | radar | raw arrival | `PRODUCTS/DROPS` |
  |---|---|---|
  | XEBY | 63.6 min | 746.1 min |
  | XSCR | 0.7 min | 747.1 min |
  | XSCV | 0.3 min | 747.1 min |
  | XSCW | 440.1 min | 746.1 min |
  | XSWR | 0.7 min | 746.1 min |

  Three false alarms out of five, and the two that were right (XEBY, XSCW)
  were right by coincidence — they read the same wrong directory as the other
  three. `RADAR_DATED_TREE` now maps each X-band to its own arrival tree.
  Directory names are not derivable from the radar id: XEBY's tree is `EBAY`.

  The DROPS producer is now unmonitored. That is a gap, not a fix, and it
  wants its own check at an informational severity rather than a return to
  gating radar health on it.

### Changed

- **`LB2_FRESHNESS` on `aqpi` moves from `dir_mtime` to `filename`**, for a
  different reason than `xqpi`'s. Not because mtime lies on these trees — it
  tracked declared time within half a minute — but because each arrival
  directory carries 126–149 **in-flight dotfiles**, and a newest-by-mtime read
  lands on one of those instead of a completed volume. The declared-time
  pattern is anchored at the start of the name, which a dotfile cannot match.
- **`RAW_VOLUME_TS_RE` is now a dict keyed by radar id**, not a single
  pattern. AQPI's two producers name files differently and nothing reconciles
  them — `aqpi.scvw-20261005-162541_...` against `AQPI.SSCB_20261005_162356.nc`.
  A single pattern fitted to the X-bands matched **0 of 295** CBAND files,
  which LB2 would have rendered as "no data directory for the current UTC day"
  against a directory holding 295 current files. `config.py` now raises at
  import if any radar in `RADAR_FOLDER` lacks a pattern, so a gap surfaces as
  the config error it is rather than once per cycle as a broken mount.

### Added

- **[Deployment profiles](07-deployment-profiles.md)** — the profile system had
  two live deployments and no documentation. Covers what a profile owns, how to
  add one, why a check is excluded by `register()` rather than by the import
  skip, and why `xqpi` cannot use file mtime.
- `SENTINEL_PROFILE` in the environment-variable reference, where it was
  missing.

### Changed

- **Every figure in `backend/profiles/xqpi.py` now carries its provenance** —
  `[M]` measured here, `[V]` reproduced here, `[Q]` taken on trust. Tagging
  made the untested set enumerable, which showed it was *exactly* the four
  thresholds that gate alarms: the interesting values had been checked and the
  load-bearing ones had not.

  Three of the four are now measured. `_FAIL_AGE_S` (1500 s) sits in a real gap
  — largest normal interval 16.0 min against smallest outage 28.0 min, over
  3,306 composite source files spanning 6 days. `RADAR_SILENT_FAIL_S` (1800 s)
  likewise, over 193 days and 496,022 arrivals: largest normal gap 25.7 min,
  smallest outage 30.8 min. Neither value changes. The recorded margin on the
  second is asymmetric — 4.3 min below, 0.8 min above — so the exposure is a
  false negative on a ~29-minute gap, which is a coverage gap rather than a
  defect given the distribution is empty there.

- **`min_png_bytes` 5000 → 2000 on the `xqpi` profile.** The check exists to
  catch a truncated write, and a truncated PNG is hundreds of bytes, while a
  legitimately *empty* frame is 8,496 B — a transparent raster still carries
  its IHDR. 5000 passed everything measured but sat only 1.7× under the
  smallest legitimate frame with no reasoning behind it. Inert today, since
  `layer1_product` does not register without an HTTP origin; AQPI made the same
  recalibration after the fact and needed a reprocess of stored verdicts.

### Fixed

- **A scope narrowing that was wrong.** The mtime hazard was recorded as
  confined to the raw volume tree, on the grounds that newest-by-mtime was the
  same *file* as newest-by-filename in the composite families. That is not a
  test for masking: a publisher re-touching only its newest frame satisfies it
  while mtime reads 1 minute against a declared timestamp 121 minutes old.

  AQPI's basis is unchanged but its justification is replaced, because it cited
  the same non-test. The discriminating measure is the per-frame offset: every
  K2 frame is written 2.25–2.48 min after its own declared time with 0.04–0.18
  min of spread, which is a fixed publish latency and not the signature
  re-touching leaves.

  The freshness fixture gains the case it was missing — a tree where *only* the
  newest frame is re-touched — and asserts the mtime basis calls it healthy
  while the declared basis fails it.

- **A cadence figure quoted without being checked.** "max 12 min" came from the
  backend survey and was wrong (a directory glob picking up month-old orphans);
  a 28-minute gap measured directly sat thirty lines below it in the same file,
  unreconciled. `cadence_s` and the thresholds are unaffected, since those
  encode the p50 and p90, which a single outlier does not move — the figure
  that would have exposed the bad method was the one nobody was using.

- `backend/checks/__init__.py`'s wiring instructions in *Adding a check*, which
  described a single import list that no longer exists.

## [0.6.2] — 2026-10-04

The UI side of the profile work: the interface now says which deployment it is
and offers only what that deployment has. **No behavior change for AQPI** —
same name, same products, same picker, same map.

### Fixed

- **The UI called every deployment "AQPI Sentinel".** The page title, the
  navbar, the FAQ heading, the password-reset page and the mobile more-page all
  carried it literally, and two of them named `radarca.engr.colostate.edu` as
  the data source on an instance that never contacts radarca. All now read
  `/api/version` through a single memoised fetch shared with the map, so five
  call sites cost one request. `app.html` keeps its static title as the
  pre-hydration fallback, so the correct name appears once the layout mounts.

- **Alert email would have gone out signed as AQPI.** `DEFAULT_FROM_NAME` was
  the literal string; it now follows the profile's `SITE_NAME`. This is the one
  branding slip with consequences outside the browser — a recipient has no
  other way to tell two deployments apart.

- **The composite picker offered products the deployment does not publish.** It
  was a fixed list of AQPI's thirteen, so XQPI showed CoSMoS water-level layers
  and Bay Area atmospheric forecasts while two of its own four ids were missing
  from the list entirely. It is now derived from the profile's products, and in
  every case filtered to those that can be *placed* — a product with no extent
  can only draw nothing or draw it in the wrong place. A persisted selection
  the deployment cannot place is reset rather than left rendering nothing.

- **The tilt elevation control was dead on a profile with no display tier.** It
  listed FLOW's four swept elevations and could fetch none of them: its
  "Default" option comes from radarca and its numbered options from
  radar-display, both the CSU display stack. Hidden when there is no such tier,
  and the remaining label names the deployment's own data source rather than
  the literal "radarca".

- **Composite imagery could not be fetched at all without an HTTP origin.**
  Every product path went to radarca — the manifest through
  `/api/productDetail`, the frames through `/api/imageData` — so on XQPI every
  composite returned 502. `backend/fs_products` reads both off the mounted
  tree, keyed on the *same* logical source string, so the filesystem path
  inherits `_serve_source`'s LRU, archive, single-flight and negative cache
  instead of reimplementing them. Verified against the live trinity tree: all
  four products resolve their manifests and return real frames.

  Paths are confined to the published root, since the source string is
  reachable from a query parameter. Frames must start with the PNG magic, so a
  half-written file is not cached and archived as valid. Reads carry the same
  5 s timeout the LB1 check uses, because a wedged NFS mount blocks a request a
  browser is waiting on rather than just stalling one check cycle.

- `product_image.png` refetched `productDetail` inline, duplicating
  `_product_steps_cached` at the cost of a second upstream call per scrubbed
  frame — and that inline call was why this one route could never read a
  filesystem manifest.

### Changed

- **XQPI's composite extent is now sourced, replacing the guess shipped in
  v0.6.1.** The composite grid carries its own CRS:
  `PRODUCTS/Composite_QPE/tmp_SRI/COMP_*.nc` is
  `PROJCS["WGS 84 / UTM zone 11N"]`, 936 × 760 cells at 250 m, naming FLOW
  alone as its input. Inverse transverse Mercator on the cell edges, derived
  independently on two sides and agreeing to sub-metre:

      W -119.5640   E -117.0000   S 33.2877   N 35.0267

  Two things identify it as the real domain rather than a plausible one: the
  grid aspect 936/760 = 1.2316 matches the PNG's 1365/1108 = 1.2319 to 0.02%,
  and the east edge falls on exactly −117.00000 because `x_max` is exactly
  500000, the zone's false easting. The provisional range-ring box was ~2.9×
  too narrow.

  Extents are now **per product**. The three QPE families deliberately get
  none: they render 1697 × 2310 against `composite_ref`'s 1365 × 1108 over the
  same underlying grid, so the renderer crops or pads and the pixel dimensions
  cannot say which. Giving them `composite_ref`'s box would place three
  products from a fourth's geometry.

  Noted beside the constant: a UTM-aligned grid is a trapezoid in lat/lon, so
  the west edge runs −119.51279 south to −119.56403 north — about 4.7 km of
  skew baked into an axis-aligned overlay. Acceptable over 234 km, and recorded
  so it is not chased as a bug.

### Notes for operators

- No migration, no new environment variable.
- **XQPI's picker will show one product, Reflectivity**, until the QPE families'
  geometry is known.
- An XQPI deployment should expect LB1 to alarm on the three QPE families
  immediately: at the time of the pre-release check they had not published
  since 23:54 while `composite_ref` was current, roughly 1 h 44 m past a
  25-minute threshold. That is the monitoring working on a real publisher gap.

## [0.6.1] — 2026-10-04

Everything v0.6.0 got wrong about running a second profile, found by standing
one up. **No behavior change for AQPI** — every fix is either inert there or
restores something that was already true.

### Fixed

- **`SENTINEL_PROFILE` never reached the container.** v0.6.0 documented it in
  `ops/.env.prod.example`, complete with a note that an unknown value refuses
  to boot, and wired it into neither compose file. Neither has an `env_file:`,
  so `--env-file` supplies `${...}` interpolation only and a variable reaches
  the process solely by being named in an `environment:` block. Setting the
  documented knob did nothing.

  Harmless for AQPI, which wants the default anyway. For XQPI it failed in the
  worst direction: the stack starts clean, passes its health checks, and
  monitors AQPI's product list against a trinity mount, so every check reports
  missing products and it reads as a broken XQPI rather than a misconfigured
  one.

  `test_compose_parity` could not have caught it — it compares the two compose
  files to each other, and the variable was missing from both. It now asserts
  against `ops/.env.prod.example` instead, which is what an operator actually
  reads: every documented knob must be named in some service's environment or
  sit in an explicit allowlist of interpolation-only variables, checked in both
  directions.

- **Two radarca checks registered on a profile with no radarca.**
  `layer2.radar.FLOW` asked radarca about a radar it has never heard of, and
  `layer2.xband.fleet` correlated a fleet that does not exist there. The live
  XQPI instance served 11 checks where the test measured 9.

  `checks/__init__` skipped the module, but `prewarm.py` imports it at module
  scope for one constant, which executes its registrations regardless — and
  loads even with `SENTINEL_PREWARM_ENABLED=0`, because the import sits outside
  the guard. "This module is not imported" is a property of the whole import
  graph, not of the gate, so it was never a guarantee.

  `registry.register()` now refuses a check whose defining module is
  radarca-only when `HAS_RADARCA` is false — one check in the one place every
  check must pass through. Declined checks are recorded in `registry.DECLINED`
  rather than vanishing, so "why is this check missing" has an answer. The
  module list is now canonical in `config.RADARCA_ONLY_MODULES`; the profile no
  longer restates it.

  The test's probe imported `backend.checks` alone — a module graph the
  application never runs in. It now imports `prewarm` and `api.app`, and
  asserts the registry *actively declined* those checks rather than merely
  never having seen them.

- **`/api/radars/meta` served AQPI's nine Bay Area radars on every profile**,
  with `folder=None` throughout and no FLOW at all. A profile that supplies its
  own radar table now replaces it wholesale rather than adding to it.

- `CheckMeta`'s documentation still called the source tag `"TRIN"`.

### Added

- **The profile is served over `/api/version`** — `profile`, `site_name`,
  `has_radarca`, `home_view`, `map_overlays`, `comp_extent`. It previously did
  not exist outside the backend, so the frontend could not adapt even in
  principle: branding, radar metadata and map extents were each hard-coded to
  AQPI independently.

- **FLOW's radar geography**, derived from its own volume headers rather than
  sourced second-hand. It is X-band: `TxFrequency` 9.3993 GHz gives λ = 3.19 cm,
  and the antenna agrees independently — `AntennaBeamwidth` 1.4° implies a
  1.59 m dish at that wavelength while `AntennaGain` 42.0 dB implies 1.65 m,
  which only reconcile at X. Range is 40,346 m, from `StartRange` −113.657 m
  plus 675 × `GateWidth` 59.941 m; both are per-radial variables in
  millimetres, not global attributes.

### Changed

- **The map opens over the radars the deployment actually has.** XQPI watches
  one radar in Pasadena, 375 km outside both AQPI extents, and the map opened
  on the Bay Area with its only radar off-screen. The home view is profile
  data, deliberately not derived from the radar list — fitting AQPI's nine
  radars plus their rings centres ~85 km north-east of the view it has always
  opened at, dragged by the three 100 km NEXRADs.

- **Regional overlays are declared per profile.** Watersheds, reservoirs and
  stream gauges are all Northern California — two static files literally named
  `*-norcal`, one feed served through radarca. Their toggles are hidden where
  the deployment has no data for them, and a persisted toggle for an absent
  overlay is cleared rather than left fetching the wrong region.

- **XQPI composites are placed on a provisional extent.** The publisher
  supplies no bounds — no worldfile, nothing in the manifests, and the rasters
  are fully transparent with no coastline to register against — so the box is
  an assumption: that a one-radar composite covers that radar's coverage,
  taken as the bounding box of FLOW's range ring.

  It may be wrong. XQPI's `qpe_15min` is byte-identical in size to AQPI's
  `rain15min`, and at AQPI's ~144 m/px that implies a 244 × 332 km regional
  domain in which FLOW would fill a third of the width. So the provisional
  status travels as data all the way to a marker on the map, because a placed
  overlay is pixel-for-pixel as convincing as a surveyed one and a QPE layer
  off by kilometres attributes rainfall to the wrong watershed.

### Notes for operators

- **No migration and no new environment variable.** `SENTINEL_PROFILE` already
  existed in `ops/.env.prod.example`; it now actually takes effect, which is
  the point of this release.
- An AQPI deployment that upgrades gets no visible change: same 63 checks, same
  map, same radar list, same home view.

## [0.6.0] — 2026-10-04

Theme: **a second deployment profile.** Dr. Chandrasekar asked for an "XQPI
Sentinel" watching the FLOW radar at JPL. One image now serves both networks,
selected by `SENTINEL_PROFILE`. Every existing deployment is unchanged — the
default is AQPI, and the AQPI registry is byte-identical before and after.

### Added

- **`SENTINEL_PROFILE` selects which radar network an instance watches.**
  Unset or `aqpi` keeps everything as it was. `xqpi` watches FLOW, published to
  trinity.

  This is not a config fork, because FLOW differs in the way that matters most:
  nothing serves it over HTTP. There is no radarca in front of it. So roughly
  two thirds of the check stack has no subject under XQPI, and a check with no
  subject does not go quiet — it fails every cycle, forever, and L0 failures
  cascade-suppress the backend checks that *are* working.

  `backend/checks/__init__.py` therefore splits into `_ALWAYS` and `_RADARCA`,
  and the radarca-only list lives in the profile and is asserted against the
  registry's own copy so the two cannot drift. Two radarca checks sit in
  modules that must stay importable regardless of profile
  (`layer0_episode` exports `attach_episode_suppression`), so their
  registrations are gated on a new `config.HAS_RADARCA` in place. Phrased as a
  capability rather than a profile-name comparison, so a third profile has to
  state its answer instead of inheriting one by omission.

  An unrecognised value raises at import rather than falling back to AQPI. An
  instance quietly watching the wrong radar network is worse than one that will
  not start.

- **Published-tree layout is profile data.** The two trees do not share one.
  K2 nests products under `realtime/product_images/`; trinity's XQPI tree puts
  them directly under `PRODUCT_IMAGES/`, and FLOW's volumes are
  date-partitioned (UTC) where AQPI's are flat under `PRODUCTS/DROPS`. Carrying
  the wrong prefix over does not raise — every LB check reports a missing
  directory, which reads as an outage rather than a misconfiguration.

### Changed

- **XQPI derives freshness from the observation time the data declares, not
  from mtime.** A root gzip sweep walks the trinity archive daily around
  07:25 UTC and rewrites files, so mtime there does not mean "when this
  observation happened". Verified on the host: the last file of `2026/09/25`
  and of `2026/09/30` both carry mtime `2026-10-03 01:25` local — the same
  instant five days apart — against an extension mix of 2,880 `.netcdf.gz` to
  4–9 `.netcdf` per day. The sweep touches the current day's directory too.

  A check reading mtime therefore reports `pass` for up to its whole threshold
  window after each sweep, whether or not FLOW is producing: a false negative
  in the one direction monitoring must never fail, recurring daily.

  `LB2` now reads the volume filename, `LB1` the manifest's per-step timestamp
  — which also sidesteps the orphans the publisher never reclaims (26 image
  files against a manifest of 14–15 on `qpe_15min`). Directory litter is
  excluded by the leading dot rather than by failing to parse a timestamp:
  gzip leaves temp files whose names embed a real, parseable observation time
  (nine of them observed for a single source volume inside a 30 ms window),
  and an NFS silly-rename carries no timestamp at all, so nothing narrower
  catches both.

  **AQPI is deliberately unchanged**, and that is the more consequential half.
  Every sampled K2 directory has newest-by-mtime equal to newest-by-filename,
  with no dotfiles and no compression pass; and the `DROPS` tree is too
  heterogeneous for a filename basis, since `ebay` holds flat `.drops` files
  while `scvw` holds a nested `2026/` directory. Switching it would have been a
  behavior change on a live deployment to fix a defect its tree does not have.

- **The timeline separates pair groups.** Consecutive pairs ran together —
  identical 1px rules down the grid with nothing saying which two rows measured
  the same target. The last row of each group now carries a 3px rule. Done as a
  border rather than a margin or a gap row because the cells are `border-box`
  with an explicit height, so the rule grows into the row and costs no vertical
  space; `pairRows` had declined a header row for that same reason, and a
  separator that reintroduced the height would have given it back.

- **Mobile pairs the backend readings instead of giving them their own cards.**
  Mobile had not tracked the previous three releases. It rendered `LB1`/`LB2`
  as separate cards, putting the two readings of one product in different
  places on the surface where scrolling costs most — the same complaint that
  produced the paired desktop rails in v0.5.4, left unfixed here. No row said
  which tree it read, so two rows could show the same target with no way to
  tell K2 from Trinity.

  A backend stage now folds into the stage it mirrors, indented under its
  primary with a left rule and the source tag. Card headers count what the card
  shows. The collapsed "problems only" filter operates on the group rather than
  the row, because per-row it could leave a backend row indented under nothing.

  The mobile uptime grid is transposed (columns are checks), so pairing there
  is column adjacency: columns order by target first — sorting by id had
  clustered every `layer1.backend.*` before every `layer1.product.*` — and
  `LB2` folds into the `L2` group. Paired column headers now carry the source
  tag; without it a paired target rendered two identical headers side by side.

- **The pairing rule moved to `$lib/pairing`,** shared by all three surfaces,
  for the same reason `$lib/timelineFill` is shared. Each had grown its own
  copy. `endsPairGroup` is exported too, so the separator predicate is shared
  and not just the ordering.

### Fixed

- `CheckMeta`'s documentation still described the source tag as `"TRIN"`; it has
  been `"TR"` since v0.5.6.

### Notes for operators

- **No migration, no new required env var.** `SENTINEL_PROFILE` is optional and
  defaults to `aqpi`; see `ops/.env.prod.example`.
- The `xqpi` profile is config- and check-complete but not yet deployed: it
  still needs the filesystem fetcher for map imagery, Southern California
  georeferencing, and its own compose bind. Nothing about it affects an AQPI
  instance.

## [0.5.6] — 2026-10-03

### Changed

- **The timeline puts a target's two readings on adjacent rows.** Rows group by
  stage, so the Products tab drew all thirteen `L1` rows and then all thirteen
  `LB1` rows — a product and its backend reading ended up about thirteen rows
  apart, and the one thing the pair exists to show, the two sources
  disagreeing, could not be seen at all.

  A backend stage no longer gets a block of its own; it is interleaved with the
  stage it mirrors. The radarca row names the target and tags itself `RC`, and
  the backend row sits beneath it, indented, carrying only its source.

  Not a group header plus two subrows: on a grid people scroll that is 39 rows
  where there were 26, thirteen of them drawing no cells. Naming the target
  once on the first row of its pair costs nothing — the tab is exactly as tall
  as before. Deployments without the mount pair nothing and are unchanged, and
  the feeds with no backend counterpart (vectors, streams, map overlays, image
  QC) stay flat rows.

### Fixed

- **Every backend row was labelled "K2", which is false for CBAND.** It reads
  Trinity — the reason `SENTINEL_SSCB_ROOT` is a separate setting pointing at a
  separate mount. `/api/checks` served nothing that distinguished a share, so
  the dashboard guessed and was wrong for one radar in six; the timeline hedged
  it as "K2 / Trinity", which is never both and never says which.

  Each backend-reading check now declares `source_label` and `source_tag`, and
  the UI uses what the check that did the reading says. The tag is exactly two
  characters by constraint, not coincidence: it sits between the status dot and
  the sparkline in the home rails, so its width decides where every trace in
  that column begins.

## [0.5.5] — 2026-10-03

### Fixed

- **The X-band fleet correlation check had never suppressed a single alarm.**
  Across its entire recorded history — 27,917 runs, 2026-08-25 to 2026-10-03 —
  `layer2.xband.fleet` appears zero times in `suppressed_by`, despite that
  being the only reason it exists.

  2026-09-17 shows why. Three radars dropped together at 16:09; their alarms
  opened at 16:12, 16:14 and 16:15 after the 5-minute hold-down. The fleet
  count sat at 3 throughout and never reached the required 4, so the check
  reported `pass` — correctly by its own rule — and three pages went out for
  what the correlation evidence calls one upstream event.

  Two changes, both measured against that history: the bar drops to **3 of 5**
  (the count is ≥4 on 0.8% of ticks and ≥3 on 1.0%, so this is a small
  widening, and it covers the 2026-09-17 case where ≥4 cannot at any dwell),
  and the verdict now **latches** — it holds until the count falls to 1 and
  for at least 10 minutes regardless. The latch matters because suppression is
  only consulted when an alarm *opens*, one hold-down after the radar started
  failing; a verdict that flaps inside that window is invisible to exactly the
  alarms it exists to suppress.

### Changed

- **The fleet row is no longer a row in the Radars rail.** It is a verdict
  *about* the rail, not a member of it: it rendered as a seventh radar with a
  UP/DOWN word, an empty sparkline (it counts radars, so it records no
  `headroom`), no image-QC dot, and a `xband-fleet` target that truncated to
  `xband-…` — and it made the rail read 5/7 when there are six radars. It now
  renders as a banner above the list, and only while it has something to say.

## [0.5.4] — 2026-10-03

### Changed

- **The home-page rails pair each target's two sources on one row.** With the
  backend tree mounted, a radar or product is one row carrying two readings —
  `K2` (the filesystem; Trinity for CBAND) and `RC` (RadarCA, the scraped
  upstream) — each tagged where it sits, under spelled-out column headers.
  Previously the two were separate rows sorted together, which rendered
  `XEBY` immediately above `XEBY · backend` and read as a duplicate.

  The tag is on the trace rather than only in a header because a header is
  gone the moment the rail scrolls, and gone again in a screenshot of three
  rows.

- **Sparklines plot `headroom` on a fixed 0..1 axis** instead of each family
  plotting a different metric autoscaled to its own window.

  Autoscaling rescaled every trace to fill its box, which erased the
  distinction the sparkline exists to draw. Measured, in a 16 px box: a
  perfectly steady series and one with *no samples at all* both rendered flat
  on the baseline, identical pixel for pixel; and a series wobbling 2% drew
  the same full-height zigzag as one swinging 6x. A radar 85% of the way to
  its silence limit looked indistinguishable from a healthy one.

  `headroom` is the fraction of a check's freshness budget still unspent,
  recorded by every plotted family against the *same* threshold its verdict
  uses, so the trace reaches the floor exactly where the row turns red. The
  radarca and backend radar checks divide by the same per-radar
  `silent_fail_s`, which is what makes the two columns comparable: a
  divergence between the traces is the two sources disagreeing, not two
  scales disagreeing. A dashed rule marks the warn boundary.

  The image count is no longer the trace. It is still recorded, and the
  sparkline's trailing `N/h` label still shows sample flow.

### Added

- **The rails adapt to which trees a deployment mounts**, read from
  `/api/checks` rather than from the rollup — the catalog answers before the
  first run lands, so the layout does not flash its one-column form on every
  first paint. No mount means one column and no tags; CBAND without
  `SENTINEL_SSCB_ROOT`, and the vector/stream feeds that have no backend
  counterpart, say *not mounted* rather than leaving a blank cell that reads
  as a failure.

- `validation_tests/js/run_sparkline_geometry.mjs` and
  `validation_tests/test_sparkline_metric.py`. The first pins the scaling
  arithmetic — including the old autoscale behaviour, so the regression has a
  name. The second walks the check registry and fails if a family the
  dashboard plots does not record the metric it asks for; that mismatch is
  silent at runtime, since the rail receives an empty array and draws an
  empty cell. It is how `LB1`/`LB2` shipped with no sparkline in v0.5.0.

- `validation_tests/test_backend_checks.py` and
  `validation_tests/test_reprocess_stage_coverage.py` — the `LB1`/`LB2` checks
  shipped in v0.5.0 with no tests, and each of the three patch releases that
  same day fixed something a test would have caught in seconds.

- The FAQ now lists the `LB` stages and says why they sit outside the
  dependency chain: radarca being down tells you nothing about whether a
  product exists, and that independence is the point.

### Fixed

- **Threshold reprocessing silently skipped any stage it had no branch for.**
  The engine dispatched on stage through an if/elif chain; anything unmatched
  fell through to `continue`, so a job could report success having evaluated
  rows and changed nothing, with no counter and no warning. Dispatch now goes
  through a registry, and stages the engine cannot handle are counted and
  named in the job result instead of being dropped.

  This was observable in production: a reprocess run on 2026-10-03 included
  `LB1` and `LB2` in its stage list and did nothing for them, reporting
  nothing.

- **The Radars rail did not scroll**, while the Products rail did — a flex
  child defaults to `min-height: auto` and will not shrink below its content.

- **Backend rows in the Radars rail rendered as duplicate radar IDs** —
  `XEBY` directly above `XEBY` — because the rail printed the raw target
  while merging two stages. (Superseded within this release by the paired
  layout above, which names each radar once.)

- **The backend checks' sparklines fetched no series at all.** The store
  enumerated stages `L1` and `L2` by hand to decide what to fetch, so `LB2`
  was absent and `LB1` was excluded twice. Those rows drew an empty cell
  while their samples sat in `metric_samples`. That was the ninth hand-rolled
  stage enumeration; v0.5.1 found eight, all in `.svelte` files, and this one
  was in a store.

- **`test_compose_parity` had been red since v0.5.0** (an unupdated mount
  literal) and `test_retention_offload` reported a skip as a failure on every
  run, because `sys.exit(str)` exits 1. Two permanently-red tests train you
  to read past the suite's output, which is how the first one went unnoticed.

- **The products rail's age readout would have gone permanently blank.** It
  came along for free while the sparkline plotted `age_s`; once the trace
  became `headroom`, nothing fetched `age_s`. The store now fetches the union
  of what is drawn and what is printed (`readoutMetric`), and the coverage
  test asserts every metric a rail prints is one something fetches.

## [0.5.3] — 2026-10-03

### Fixed

- **Threshold reprocessing silently skipped `LB1` / `LB2` rows.** The engine
  dispatches on stage and had branches for `L1`, `L2` and `L4-T1T2` only; anything
  else fell through to `continue`. A reprocess job therefore reported success having
  evaluated nothing — the same failure mode the `_reverdict_l2` docstring records
  from 2026-08-26, for the same reason.

  `_reverdict_lb1` and `_reverdict_lb2` now re-apply the current
  `backend_max_age_s` / `backend_silent_s` to a stored observation.

- **Backend checks now record `age_s` in the payload, not only in metrics.**
  `check_runs` stores `payload` but not `metrics` (those go to `metric_samples`), and
  the reprocess engine reads only `check_runs` — so the observation it needed was not
  reachable. **Rows written before 0.5.3 carry no `age_s` and cannot be reprocessed**;
  they are skipped rather than guessed at.

- **Four more places that enumerated stages by hand**, found by auditing every
  `stage ==` / stage-literal in both codebases rather than waiting to trip over them:
  the reprocess stage selector (which offered three stages the engine could handle and
  none of the new ones), the timeline's product-category subgrouping, the home page's
  Radars and Products sections, and a duplicated stage→label map in the report-export
  modal that now delegates to the canonical one.

### Changed

- **The home page shows backend and radarca rows together.** `L2 + LB2` under Radars,
  `L1 + LB1` under Products, sorted by target so the two views of the same thing are
  adjacent and `prettyCheckLabel` distinguishes them. This is the comparison the
  backend-primary rollout depends on, and the home page is where it is most useful.


## [0.5.2] — 2026-10-03

### Added

- **`backend_max_age_s` and `backend_silent_s` are editable in `/admin/thresholds`.**
  The `LB1`/`LB2` checks read these through the normal threshold machinery, and the API
  accepted them from the start — but the admin tables wrote their column sets out by
  hand, so neither key appeared and tuning meant hand-crafting a `PUT`. Both tables now
  derive their columns from a single `PRODUCT_KEYS` / `RADAR_KEYS` list, the same
  approach `ALL_STAGES` took in v0.5.1.

### Changed

- `_validate_thresholds` type-checks the two new keys (positive number for
  `backend_silent_s`, number for `backend_max_age_s`) rather than passing them through
  unvalidated.


## [0.5.1] — 2026-10-03

### Fixed

- **`LB2` radar checks crashed on every run.** `_special_trees()` called `.rstrip("/")`
  on `SETTINGS.sscb_root`, which `config._opt_path()` returns as a `Path` — and `Path`
  has no `rstrip`. Every `layer2.backend.*` run raised `AttributeError` immediately.
  Now `str()`-converted first.

- **The new `LB1` / `LB2` stages were invisible across most of the UI.** v0.5.0 added
  them to the stage *label* lookups but not to the eight separate hard-coded stage
  *enumerations*, so the checks registered and ran while being silently dropped from the
  timeline, history, mobile history, mobile uptime rollups, report export, the silence
  matcher and the admin alert-route picker.

  Two of those were worse than cosmetic: an `LB` alarm could not be silenced from the UI,
  and `LB` routes could not be configured from the admin page.

### Changed

- **One canonical stage list.** `ALL_STAGES` and `stageOptions()` are now exported from
  `frontend/src/lib/format.ts`, and every enumeration derives from them. Eight copies of
  `['L0','L1','L2','L3','L4-T1T2']` is exactly why seven of them were missed; adding a
  stage is now a one-line change in one file.

  Backend stages are ordered immediately after the radarca stage they correspond to
  (`L0, L1, LB1, L2, LB2, L3, L4-T1T2`), so the two views of the same thing are adjacent
  wherever stages are listed. The timeline's Products tab now covers `L1 + LB1` and its
  Radars tab `L2 + LB2 + L3 + L4-T1T2`, which is the side-by-side comparison the
  backend-vs-radarca rollout depends on.

- `m/history`'s stage filter now uses the shared labels, so "Products" there reads
  "Product Freshness" as it does everywhere else.


## [0.5.0] — 2026-10-03

### Added

- **Backend product and radar checks — stages `LB1` and `LB2`.** Until now every
  product and radar verdict was derived from `radarca`'s API, which conflates two
  different failures: the product not being *produced*, and the display tier not
  *showing* it. Operators repeatedly reported "Sentinel says X, but when I checked
  the products in K2…", and when the two disagree K2 is ground truth — so each
  disagreement cost Sentinel credibility.

  The new checks read the published files directly over the monitoring host's
  existing read-only NFS mounts of K2 and trinity, and are therefore unaffected by
  anything wrong with radarca. `LB1` covers the 13 products; `LB2` covers the six
  radars, including the C-band tree on trinity which does not live under the DROPS
  tree with the five X-bands.

  They are a **third family of checks, not a modification of the existing two.**
  `layer1_product.py` and `layer2_radar.py` are untouched. Because the new checks
  carry their own stages, which family pages and which is informational is decided
  entirely in `alerts.yaml` routing — reversible with `POST /api/alerts/reload`, no
  redeploy. The same reason `layer4_image` is its own stage rather than a flag on
  the radar checks.

  **Gated on `SENTINEL_BACKEND_ROOT`.** Unset, the checks are never registered, so a
  deployment that cannot reach the shares — a dev instance with no VPN — runs an
  identical image with no new checks and nothing to explain away. The bind mounts
  default to empty named volumes and are `:ro`, so Sentinel cannot modify what it
  monitors.

  Every filesystem read is wrapped in a 5-second timeout and reports `error` rather
  than `fail` on expiry. A hung NFS mount is a failure mode Sentinel has not had
  before — every prior check is HTTP with its own timeout — and without this one
  wedged mount would stall the scheduler.

### Changed

- `backend/config.py` gains `backend_root` and `sscb_root`, both `None` when unset.
- `backend/stages.py` and `frontend/src/lib/format.ts` gain `LB1` / `LB2` labels.
  Unknown stages already fall through to their raw ID, so this is cosmetic.
- `ops/docker-compose.{prod,ghcr}.yml` gain the two read-only mounts, following the
  existing `SENTINEL_BACKUP_HOST_PATH` idiom: an env-parameterised host path that
  defaults to an empty named volume.


## [0.4.13] — 2026-09-30

### Added

- **Acknowledge an alarm from the alert email.** The escalation ladder is the
  answer to "did anyone see this", and an ack is what stops it early — but
  only if acking costs less than ignoring the mail. It previously meant
  opening the dashboard and logging in, so at 02:00 nobody did and every rung
  ran to completion. `ack_tokens` had been in `schema.sql` since May with no
  code referencing it; this fills it in.

  `GET` is read-only and renders a confirmation button; the button `POST`s.
  That split is the security design, not ceremony: Outlook Safe Links,
  Gmail's proxy and DLP scanners all follow URLs in mail before a human reads
  the message, so a `GET` that acknowledged on sight would be claimed by a
  scanner seconds after delivery — silently cancelling the remaining rungs,
  which is exactly the failure the rungs exist to prevent.

  Tokens are 256 bits of urandom, expire after 7 days, and authorise nothing
  but acking their one alarm. Single-use is enforced in the `UPDATE`'s `WHERE`
  clause, so two taps race in Postgres and one wins. The page is plain HTML
  with no SPA, session or JavaScript — it is what someone opens on a phone at
  02:00.

  Because the ack goes through `store.ack_alarm` it picks up the
  `severity_at_ack` / `status_at_ack` baseline from v0.4.11, so a link-ack
  still lapses if the condition worsens. It quiets the reminders; it does not
  blind you.

### Changed

- **Alert emails now go out as one message per address** rather than one
  message addressed to everyone. Ack tokens are issued per recipient so an
  acknowledgement records *who* made it; a shared link can only ever report
  that "someone" acknowledged. Delivery failure is per-address — one bad
  mailbox is logged and does not silence the other recipients.

- `ack_tokens` gains a nullable `recipient` column via the existing
  idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` pattern. Tokens
  predating it read back as unattributed acks and still work.

### Fixed

- **Documentation described two routing fields that do not work.**
  `count_of_targets_failing` counts siblings in `ctx["open_alarms"]`, which
  `_process` always passes as an empty list, so any `>=N` with N≥1 never
  matches and the route silently never fires. `group_by` is accepted and
  stored but referenced nowhere in the engine or the sinks — the admin guide
  claimed it "collapses a flapping product to one email rather than three".
  Both are now marked non-functional. A documented feature that silently does
  nothing is worse than an absent one: it gets configured and then trusted.

- **`16-alarm-engine.md` still described the pre-v0.4.11 ack ordering**,
  stating that `_process` checks `is_acked` *before* resolving a route. That
  changed deliberately in v0.4.11 — checking first froze an acked alarm's
  severity for life — and the doc was not updated at the time. It now also
  covers suppression re-evaluation, the severity ratchet, and ack
  lapse-on-worsening.

- Sub-check naming brought in line with v0.4.11's `B_nonempty` split across
  the severity audit, `ARCHITECTURE.md` and the porting guide; `03-administration.md`
  documents the per-route hold-down added in v0.4.12, with the measured
  numbers behind the tuning advice; `SENTINEL_PUBLIC_URL` now notes that it
  gates the ack link as well as the dashboard link; and troubleshooting gains
  "an alarm is open but no email went out", walking the six silent causes,
  plus the case where a receiver holds an address nobody owns — which cost a
  colleague all 26 of one night's alerts while every one logged as `sent`.

## [0.4.12] — 2026-09-30

### Fixed

- **The alert-routing editor silently erased per-route hold-downs on save.**
  `/admin/alerts` is a form builder, not a text editor: saving discards the
  stored config and rebuilds each route from form state. `buildConfig()` wrote
  `match`, `policy`, `when`, `severity_floor`, `repeat_interval` and
  `group_by` — but not `hold_down`, which the backend honors per route and
  which the editor never rendered. The string appeared zero times in the page.
  Opening the page and clicking Save therefore reset every per-route hold-down
  to the global default, with no error and no warning; the config saved and
  looked correct.

  Measured against the 14 days to 2026-09-30, that is the difference between
  104 and 136 alert emails — and the 32 that return are precisely the flappers
  the thresholds exist to remove, including an XEBY alarm that opened and
  self-resolved inside 12 minutes, two minutes after its page went out.

  The field is now present across all five touchpoints `repeat_interval`
  already had (type, load, `buildConfig()`, new-route default, editor control),
  labelled "only after it lasts", with the global value in the placeholder so a
  blank field reads as "inherit" rather than "none".

### Added

- `validation_tests/js/test_route_config_roundtrip.mjs` — asserts that every
  non-UI field on the `Route` interface is both read by the load path and
  written by `buildConfig()`. That function lives inline in the `.svelte` and
  cannot be imported the way the other js tests import `$lib` modules, and the
  defect class is a field being *absent*, so the assertion is source-level.
  Verified against the original bug: removing the emit line fails exactly one
  assertion, and restoring it passes.

- **The GitHub Release is now cut by CI, not by hand.**
  `.github/workflows/release.yml` fires on a `vX.Y.Z` tag push and
  publishes the Release with the annotated tag's subject as the title and
  that version's `CHANGELOG.md` section as the body. Creating the Release
  had been a manual step marked "optional but recommended" that also
  required hand-editing the notes after the fact, and it lapsed after
  v0.4.3: tags and GHCR images kept publishing while the Releases page
  named v0.4.3 as latest for six days, through v0.4.11. The job fails
  loudly if the CHANGELOG has no section for the tag, rather than
  publishing an empty release, and re-running it on an existing tag
  refreshes the notes instead of erroring. Releases for v0.4.4 .. v0.4.11
  were backfilled from the CHANGELOG.

## [0.4.11] — 2026-09-15

Root-cause work on the 2026-09-13/14 upstream incident turned up seven
Sentinel defects that let an 11h 47m data outage and a 14h 06m publisher
stall pass with zero notifications and a daily report that understated both
by roughly an order of magnitude. This release fixes all seven, and widens
the one threshold whose false alarms had trained the reflex that silenced
them.

### Fixed

- **The daily report stated a condition it had not checked.** `describe()`
  hardcoded the word "stale" for every product `warn` episode regardless of
  which sub-check warned, so the 2026-09-15 briefing told ten recipients that
  `fcst_temp` was "stale 2h 59m" — for data whose newest timestamp was five
  days *ahead* of wall clock. The warns were manifest-ordering parity
  defects. The sentence now names the sub-checks that actually warned, with a
  raw-key fallback for anything unmapped: an unfamiliar sub-check prints its
  own name, which is ugly but never wrong. Radars emit no `sub_status` map
  and keep the generic word rather than acquiring a specific one that would
  be false.

- **An outage already running when the report window opened was printed at
  its clipped length, as though that were the whole thing.** `comp_now`'s
  14h 06m stall (01:26 → 15:32 UTC) rendered as "one outage, 2h 26m" because
  the window opened at 13:05; `qpe_1hr`'s 11h 47m outage rendered as "1h
  04m". Both figures were arithmetically exact and gave a false impression.
  Such episodes now print as a floor with the last known-good time — `one
  outage, ≥ 2h 26m · last healthy 01:25 UTC 09-14, before this window`. The
  clipped number is deliberately *not* replaced by the true length, because
  the report's own header says it covers 24 hours. A one-run clipped episode
  is also promoted out of the blip counters: a 14-hour outage whose tail
  lands on a single check is an outage, not a "brief interruption".

- **Cascade suppression never expired.** `suppressed_by` was computed once at
  alarm-open and thereafter only read. Four product alarms opened
  2026-09-13 21:40 attributed to `layer0.origin.episode` — correct at that
  minute. The origin episode passed; the products did not, and went on to
  serve an empty manifest from 02:22 to 14:10 the next day. The stale
  attribution held all four silent for 17h 30m. Suppression is now
  re-evaluated every tick against a per-tick status snapshot: cleared when
  the named cause recovers, re-stamped when a different ancestor is failing,
  and left exactly as it was when no snapshot is available.

- **Alarm severity was a property of the first bad sample, not of the
  condition.** `compute_severity` read `status_at_open` and nothing else, and
  warn-opened alarms were explicitly excluded from duration promotion.
  `qpe_1hr` opened on an `E_step_count` warn, went to a hard fail four hours
  later, and served no data for twelve hours — at severity `info` throughout.
  An alarm that opened on a warn could never escalate however far the thing
  fell afterwards, which is exactly the shape a progressively degrading
  upstream produces. Severity is now the worse of open-time and current
  status, duration promotion fires if either is `fail`/`error`, and the
  result is ratcheted so it rises with the condition and falls only when the
  alarm closes.

- **Acknowledgement froze everything, not just the paging.** `is_acked`
  returned before route matching and before severity computation, so acking
  an alarm pinned its severity for life and silenced it unconditionally. One
  bulk ack at 2026-09-14 04:00:38 covered five alarms including a nowcast
  that had stopped publishing ninety minutes earlier and would not resume for
  another eleven hours. The ack check now runs after severity, and consults a
  baseline: `ack_alarm` records `severity_at_ack` and `status_at_ack`, and
  the ack lapses when either rises. Acking the degraded thing no longer means
  going deaf to what it becomes.

- **An empty manifest and a malformed one shared one nameless verdict.**
  Both returned the summary `empty/malformed steps` with no sub-check list at
  all. From 2026-09-14 02:22 to 14:10 UTC the upstream answered every request
  for `comp_ref`, `qpe_1hr`, `qpe_15min` and `precip_rate_radar` with HTTP
  200 and zero steps — 2,018 runs, an 11h 47m total data outage whose
  highest-signal symptom was the least legible line in the UI. Split into
  `B_nonempty` and `B_schema`, each with a summary that says which happened.

- **Parity verdicts never reached the summary.** `_summarize()` was called
  with the sub-check dict *before* the parity verdict was merged in, while
  `_final()` got the merged one. A parity warn therefore set the overall
  status to `warn` while leaving the summary with no `warn=[...]` list, so
  nothing downstream could name the condition — the direct cause of the
  "stale" mislabel above. One dict now feeds both.

### Changed

- **`comp_now`'s freshness threshold widened from -3000s to -2400s.** -3000
  is exactly where a healthy publish cycle ends, so the threshold had no
  slack and a single late cycle failed the check. Measured over 7 days to
  2026-09-15 with the 01:26–15:32 stall excluded (n=9,200): p50 -3227, p90
  -3078, p99 -2750, p99.5 -2122. A cycle publishes at ~-3360 and drifts to
  ~-3000 over about six minutes, so one missed publish lands near -2640 and
  two near -2280; -2400 tolerates one and catches the second. Failing share
  drops from 1.99% to 0.62%, at the cost of ~10 minutes of extra detection
  latency on a genuine stall. The churn mattered: it produced six pass/fail
  flips in the 00:00–01:00 UTC hour on 2026-09-14, an hour before the real
  stall, and the alarms for that stall were bulk-acked at 04:00 and then ran
  unnoticed for eleven more hours.

- `alarm_acks` gains two nullable columns, `severity_at_ack` and
  `status_at_ack`, via the existing idempotent `ALTER TABLE ... ADD COLUMN IF
  NOT EXISTS` pattern. No backfill: `NULL` means "no baseline on record", and
  such an ack is honored rather than revoked on a guess.

- The daily report's JSON now carries `fail_subs`, `warn_subs` and
  `truncated` per subject, so API consumers can reach the same facts the
  sentence states without parsing prose.

## [0.4.10] — 2026-09-14

### Fixed

- **The clip guard was measured in the wrong unit, in the unsafe direction.**
  Gmail's ~102 KB limit applies to the **encoded** body. The HTML ships
  quoted-printable — what `EmailMessage.add_alternative` picks for this
  content — and QP costs **+11%** on this markup, since `·` and `—` are
  multi-byte and every `=` and high byte expands.

  The threshold was `95,000` measured on `len(html)`, which is ~105,450 on the
  wire. There was a live window, roughly 92,000–95,000 HTML bytes, where the
  guard reported the report was fine and Gmail truncated it anyway — precisely
  the failure the fallback was added to prevent, missed because the check and
  the limit were in different units.

  `render_html` now encodes the way the send path does and compares against a
  wire threshold, rather than applying a fudge factor: the inflation depends on
  how much non-ASCII a given report contains, so a constant would be wrong
  somewhere. The log line reports both numbers, and the tests assert on wire
  bytes including that every fixture shape ships under the clip.

  Where the fallback now fires, in wire bytes:

  ```
  4/8 bins mixed:  97,921  -> stacked
  5/8 bins mixed: 106,737  -> compact, ships 67,616
  ```

- **A tooltip trim undid a property from 0.4.6.** Dropping titles from all
  single-status bins also dropped them from grey no-verdict bins — which then
  could not be told apart from a slice where nothing ran, a different
  statement. Titles are kept wherever the color is ambiguous (multi-band bins,
  bins with excluded runs, no-verdict bins) and dropped only where the swatch
  is already its own answer.

### Changed

- **The compact fallback blends rather than steps.** It used a four-step ramp
  on the bin's availability, which could not distinguish a bin that was 40%
  `warn` from one that was 40% `fail` — both landed on the same gold. It now
  mixes the status colors in proportion. Anchors are exact (all-pass is
  precisely `#16a34a`, all-fail `#b91c1c`) and the mix is monotone: more
  failure always moves the swatch toward red.

  Two limits, recorded as properties rather than oversights. A pass/fail blend
  passes through olive near 50/50, which sits close to the warn gold — three
  hues in one swatch cannot be fully unambiguous. And the blend does not floor
  the failing share the way the stacked bars do, so a 1%-fail bin is very
  nearly green. Both are the cost of asking one color to carry a proportion,
  and both are why stacking is the default and this is the fallback.

- Markup trims: band rows drop the duplicate `height` CSS (the attribute
  already says it), and plain single-status bins drop their tooltip. Worth ~4
  KB. Noted honestly: this was expected to move the fallback trigger from 5/8
  to 6/8 mixed bins and did not — 67 KB of the worst case is per-row
  scaffolding that bin trimming does not touch.

## [0.4.9] — 2026-09-14

### Fixed

- **Outlook was showing a different chart from Gmail.** Word has no CSS
  gradients, so the gradient bins collapsed to their `bgcolor` fallback — the
  bin's **worst** status, painted solid. A bin that was 95% healthy read as
  solid red. Defensible while Outlook was the minority; as the primary
  rendering for nearly the whole readership it is the exact "worst of the bin"
  distortion the proportional encoding exists to avoid.

  Bins are now stacked background-colored table **rows**, which Word draws
  natively. One rendering for every client.

  Size was the obstacle: stacked bins cost ~4× the markup and put a 19-row
  report at 119 KB, past Gmail's ~102 KB clip. Two things clear it —
  a single-color bin skips the nested table (117 of 152 bins on a real day are
  one color; that alone took 88.9 KB → 71.0 KB), and `render_html` measures
  itself and re-renders compact above 95 KB so an all-mixed day degrades to
  solid bins rather than losing its tail. That fallback colors by the bin's
  *availability*, not its worst status, so it does not reintroduce the
  distortion.

- **Three alignment faults**, each found behind the last:

  | Fault | Effect |
  |---|---|
  | Bin variants did not share a box — the multi-color cell had no `height` or `border-radius`, the single-color one had both | rows mixing them sat at different sizes and corners |
  | The availability figure had no fixed width, and the strip is right-aligned | `0.0%` pushed the bars ~18px further right than `100.0%`; radars span the full range, so it showed there |
  | `Forecast — Cumulative Precipitation · fcst_total_precip_cum` is 59 chars | wide enough to push the right-hand cell and knock that row out of the column |

  All three bin variants now render one geometry (asserted by reading the
  template), the availability cell is a fixed 54px as both attribute and CSS,
  and products print the name alone — 35 characters at worst.

### Changed

- **Products lead with their name; radars keep their id.** `XSWR` is what
  appears in alarms, check ids and the evidence link, and the call signs are
  the vocabulary the lab speaks. `max_water_level` is not a word anyone says,
  and unlike a call sign carries nothing the name does not. The product id is
  dropped rather than trailed — it still reaches the reader through the
  evidence link, and the plain-text part still carries it where no width can
  break.

- `bar_css` and the gradient path are removed rather than left dormant.

## [0.4.8] — 2026-09-14

### Changed

- **Upstream HTTP timeout raised 35s → 50s.** radarca's latency tail has moved
  again. The 35s comment named the measurement to take — `over_ceiling` on
  `layer0.origin.latency`, "sustained above ~1% means the tail has moved" — and
  it is at **7.2%**. Over 290 uncensored probes in 24 h:

  ```
  p50 4.8s   p75 10.9s   p90 23.0s   p95 41.9s   p99 59.4s   max 120.7s
  ```

  p95 was 5.9–8.4s when 35s was chosen. Buckets above the old ceiling:
  30–40s ×4, 40–50s ×10, 50–60s ×7, 60–70s ×1, 90–100s ×1, 120–130s ×1.

  The same comment warns against raising this for *burst*-induced timeouts, and
  that warning does not apply: `over_ceiling` is **5.6% outside episodes** and
  17.9% during them, so the baseline moved rather than only the peaks.

  **The cadence picks the number, not the latency.** The shortest cadence using
  this client is 60s and read timeouts are not retried, so 50s is one timeout at
  83% of a cycle; 60s would consume the whole cycle. That covers roughly 14 of
  the 24 over-ceiling samples — the 60s, 92s and 120.7s tail is not reachable by
  any timeout this scheduler can afford.

  What it trades away: a 45-second answer is now a slow pass rather than an
  error. That is a real upstream degradation being reclassified, not fixed, and
  `layer0.origin.latency` is the only thing still reporting it — which today
  returns `pass` on samples as slow as 91.9s because its verdict keys off HTTP
  status rather than latency. That check is the natural home for a latency
  threshold; deliberately not in this release.

- **Canary ceiling raised 75s → 150s**, and not optionally.
  `test_latency_canary` asserts the canary's ceiling stays more than 2× the
  operational one, and that assertion failed on this change: measuring a tail
  from inside the ceiling that truncates it is the circularity the canary
  exists to break, and 75s is not far enough above 50s to see past it.

  75s was stale regardless. It was set as "well beyond the worst observed
  (42s)", while this check recorded **120.7s** on 2026-09-13 — a sample that
  reached us only because httpx applies a bare float timeout per-operation
  rather than to the whole request, so the 75s label never was the
  total-request bound it read as. 150s is not a prediction of the true maximum;
  it is a ceiling chosen to be uninteresting, so `timed_out` staying at 0 means
  something.

- `LIVE_TIMEOUT_S` in `validation_tests/test_layer0_website.py` moves with the
  operational ceiling, as its own comment instructs — the live probes hit the
  same endpoint, and a lower ceiling fails for reasons unrelated to the test.

## [0.4.7] — 2026-09-13

### Changed

- **Every subject gets its own row in the daily report, healthy ones
  included.** Perfectly healthy subjects collapsed into a single
  `N nominal: CBAND, …` line to keep the report short. That traded away the
  number a reader most often wants to confirm: `CBAND 100.0% nominal` reads
  differently from CBAND merely being *absent* from the trouble list, and
  absence is ambiguous — it could equally mean the check stopped running.

  Rows are still sorted worst first, so a clean subject costs two lines at the
  bottom rather than attention at the top.

  The evidence link now hangs off healthy rows too, which changes its meaning
  slightly and deliberately: for a row with something wrong the filtered view
  is the evidence for it; for a row at 100% it is the evidence that there was
  nothing. It is also where a clean row's *excluded* runs can be seen, which is
  why `error` and `skip` stay in the link's filter even though v0.4.6 stopped
  counting them — the disclosure line needs somewhere to point.

  Against production the report grows from 51.3 KB to 64.9 KB, 37 KB clear of
  Gmail's ~102 KB clip.

## [0.4.6] — 2026-09-13

### Changed

- **The daily report covers `pass`, `warn` and `fail` only.** `error` means
  Sentinel's own probe failed; `skip` means it declined to judge because a
  dependency was already down. Neither says anything about whether a radar is
  working, and both were moving the figures.

  The effect is larger than it sounds. Measured against the same 24 h window:

  | Subject | Before | After |
  |---|---|---|
  | XSCW | 85.5% | **90.2%** |
  | XSCR | 94.2% | **99.7%** |
  | XSCV | 94.4% | **99.9%** |
  | XSWR | 94.8% | **99.4%** |
  | CBAND | 94.1% | **nominal** |
  | XEBY | 0.0% | 0.0% |
  | *every product* | — | *unchanged* |

  Every radar moved and not one product did, which is the finding in a line:
  the radars' missing percent was our probe erroring against the upstream API
  and being counted as the radar being offline. Errors were class 2 in the
  episode grouping, indistinguishable from a fail — so CBAND's "offline 15m
  across 3 outages" was three API errors, and it now reads nominal, which is
  what it was. The products' losses are genuine fails and stayed put.

  Swatches lose the violet and grey bands with them.

### Fixed

- **`describe()` claimed "no checks in window" for a subject whose every run
  was excluded.** With 1,440 runs in the window that is simply false, and it is
  the plainest possible false claim in a report whose purpose is to be
  checkable. It now reads "nothing conclusive — all 1,440 checks errored or
  were skipped"; a genuinely empty window keeps the old wording. Both are
  pinned, because they are different statements that were sharing a sentence.

### Notes

Dropping two statuses from a report is the easiest possible way to make a blind
spot look healthy — the mistake this codebase has made three times already
(closing alarms on demoted skips, painting skips green, painting errors green
above them). So the exclusion is stated rather than silent:

- The excluded count appears in both renderings, with wording that matches what
  is actually excluded. In the window above: 2,413 of 31,995 runs, 7.5%.
- A slice where every run errored renders in the no-verdict grey, never green,
  and its tooltip says "N not counted" so it differs from a slice where nothing
  ran at all.
- Availability is `None`, not 0%, when nothing was conclusive — calling it 0%
  would invent an outage out of our own blindness.

One honest cost: outage durations grow 5–12 minutes on a ~2 h outage, because
the episode window now runs over the filtered series and failures either side
of an excluded run merge into one episode. The alternative splits one outage
into two on the strength of a run that told us nothing.

`/api/report/daily` renames `inconclusive` to `excluded`, and its meaning
widens from demoted skips to all errors and skips.

## [0.4.5] — 2026-09-12

### Fixed

- **The daily report laid itself out with `display:inline-block`, which Outlook
  does not implement.** Outlook renders through Word; the template used
  inline-block in four places with no `mso` conditionals to compensate, so the
  email came apart there while looking correct in Gmail.

  | Element | What Outlook did |
  |---|---|
  | Swatch strip (`<table display:inline-block>`) | Nested tables are block-level in Word — the strip dropped to its own line, away from the percentage |
  | Availability `%` (`<span>` + `padding-left`) | Padding on an inline element is dropped — no gap |
  | "N nominal" dot (`<span>` + width/height) | Both ignored on a span — the dot was absent |
  | "Open the dashboard" (`<a>` + padding) | Padding on an inline `<a>` is dropped — bare text, no button |

  All four are table cells now, the only horizontal layout primitive Word
  honors: swatches and percentage share one two-cell row, the dot is a one-cell
  table, and the button's padding sits on a `<td bgcolor>` with the `<a>`
  filling it. `display:inline-block` no longer appears in the rendered output.

  The swatches themselves still degrade deliberately: Word has no CSS
  gradients, so it shows the `bgcolor` fallback — a solid swatch in the worst
  status color, which is the grid's own "color = worst status" rule without the
  density. Every other client gets the full composition.

## [0.4.4] — 2026-09-11

Everything in this release is one mistake in five places: **inferring a status
from another one's absence.** A remainder assumed to be `pass`, a manifest
assumed clean because nothing was watching it, an evidence link assumed to show
what the claim was about.

### Fixed

- **Errors rendered as green on the bucketed timeline.** The same assumption
  the v0.3.0 skip fix corrected, surviving one band higher up: that fix added a
  grey band for skips but left the band *above* it hard-coded to the pass
  color, so a cell whose worst status was `fail` drew its fail band, its skip
  band, and then everything left over in green — **including the runs that
  errored**.

  XEBY on 2026-09-11 logged 673 `fail`, 23 `error`, 20 `skip` and **not one
  `pass`** in 24 hours, and every one of its cells drew part green. A radar
  down for weeks showed healthy for the runs where the probe itself failed.

  Cells are now composed from every status: `fail`, `error`, `warn`, `skip`,
  then only what genuinely remains. Two details the tests forced:

  - Sizing bands while walking them let the most severe eat the cell and drop
    the rest — one error among 999 fails vanished, the same "guess low and it
    disappears" failure `MIN_BAD_PX` exists to prevent, moved up a band. Bands
    are collected first and sized second, and when they do not fit, pixels come
    off the **largest** band down to its floor.
  - `pass` is the one band with **no** floor. Giving it one drew a cell that
    was 98.9% failing at 93.75% — rounding a 1.1% healthy share up to a visible
    slice. Defects keep their floors; health has to earn its pixel.

- **The daily report's evidence links appeared to disprove the report.** It
  said `qpe_15min`, `qpe_1hr`, `precip_rate_radar` and `comp_ref` were
  99.5–99.8% over 24 h; "See the checks behind this" showed unbroken green for
  all four. The report was right and the link was wrong, which is the worse way
  round — the reader's correct conclusion is that the numbers are junk.

  These products run every ~60 s, so the window holds ~1,437 runs while
  `/history` renders the newest 500, and `ORDER BY finished_at DESC` puts the
  cut at the *start* of the window. All 19 non-passing runs were in the
  truncated end, and nothing on the page said it had been truncated.

  | Product | Runs | Non-pass | Shown |
  |---|---|---|---|
  | `comp_ref` | 1,436 | 3 | **0** |
  | `precip_rate_radar` | 1,436 | 4 | **0** |
  | `qpe_15min` | 1,438 | 7 | **0** |
  | `qpe_1hr` | 1,437 | 5 | **0** |

  Fixed at all three layers: the link now carries `status=fail,error,warn,skip`
  (only non-nominal rows are ever linked, so every link hangs off a claim about
  something going wrong); `/history` was silently **dropping** the `status`
  deeplink parameter, so the filter would have been ignored even once sent; and
  `/api/history/checks` returns `X-Total-Matching` / `X-Truncated` so the page
  can say "showing the newest 500 of 1,438". That last one matters beyond the
  report — anyone filtering a wide window by hand was getting a truncated
  answer with nothing saying so.

- **Forecast parity warned on 78% of runs for something that was never a
  fault**, and the observed products were not checked for it at all. See
  [0.4.3] for the first half; this release adds
  `classify_timestamp_sequence()`, so a duplicated entry, one image under two
  timestamps, or time running backwards now warns on the observed products
  too. Its policy is deliberately the **opposite** of the forecast one: there,
  repeats are the upstream's normal structure and alarming on them is crying
  wolf; here, none has ever been seen in ~41,000 runs, so one appearing is
  news. The test asserts that asymmetry so nobody harmonizes the two
  classifiers and silently reopens the blind spot.

### Changed

- **The report names its subjects**: `XSWR · Sawyer Ridge`, `fcst_temp ·
  Forecast — Temperature`. Id first — it is what appears in alarms, check ids
  and the evidence link — and the name second, for readers who have not
  memorized five X-band call signs. Names come from `RADAR_META` and
  `check_labels`, the two tables that already hold this vocabulary, so the
  report cannot drift from the dashboard.

- **The report's swatches use the timeline's palette and encoding.** They were
  colored by availability percentage on a separate ramp while the grid colors
  by status composition, so the same hour read two ways. Two colors had drifted
  outright (`#d97706`/`#dc2626` vs `#9a6905`/`#B91C1C`) and `error` had no
  color here at all.

  Each swatch is now the same stack as a timeline cell. Rendering that as
  nested color rows cost ~100 bytes a band and took the email from 46 KB to
  **91 KB** against production — inside 11 KB of Gmail's ~102 KB clip
  threshold, and a clipped report loses its conclusion silently. It is one
  `linear-gradient` per swatch instead, with `bgcolor` carrying the most severe
  color so Outlook (which renders through Word) degrades to a solid
  worst-status swatch — the grid's own rule, minus the density. Back to
  51.7 KB.

## [0.4.3] — 2026-09-11

### Fixed

- **Forecast parity warned on 78% of runs and flapped alarms, for something
  that was never a fault.** `fcst_temp` warned on 265 of 341 runs over seven
  days and opened 25 alarms that all auto-closed, while the condition behind
  them had not changed since at least 2026-09-04.

  The upstream stitches a short-range and a long-range block into one manifest
  and re-lists the long-range block. Captured from production on 2026-09-11,
  `temperature/details_F.json` held **129 entries for 73 distinct files**:

  ```
  pos[  0.. 18]  step  0..18   ts 09-11T19:00 .. 09-12T13:00
  pos[ 19.. 73]  step 18..72   ts 09-12T14:00 .. 09-16T21:00
  pos[ 74..128]  step 18..72   ts 09-12T14:00 .. 09-16T21:00   ← verbatim replay
  ```

  The old rule — every index is the previous plus one — read each replay as
  steps served out of order. Across the week the same manifest appeared at 19,
  74, 129, 187 and 243 entries, one more replay each time, and the mismatch
  count tracked the replay count exactly: 0, 1, 2, 3, 4. It also flapped: the
  short 19-entry form is clean and passes, the long form warned, and the
  upstream alternates between them roughly every half hour.

  `classify_step_sequence()` now judges the sequence as a whole and separates
  two questions the old rule conflated:

  | Condition | Verdict |
  |---|---|
  | A step missing from the range | `warn` |
  | Time not advancing on first occurrence | `warn` |
  | Time out of order inside one block | `warn` |
  | A republished block disagreeing about the times | `warn` |
  | The same forecast time listed twice | reported, `pass` |
  | One file carrying two times where blocks join | reported, `pass` |

  The last row is the judgment call. At the seam a single file carries two
  forecast times — `step18.png` at both 13:00 and 14:00 — so one of those hours
  displays its neighbor's image. That is a real upstream defect, and it does
  not set the verdict, because the index in these filenames is a position
  within its own block rather than a global identity: the same lesson the tilt
  archive taught, where a frame index names a slot and not a frame.

  Being permissive here has an obvious failure mode, and the test found it:
  the first version accepted *any* re-listing, including a block republishing
  the same steps with different timestamps — the upstream giving two different
  answers, which is precisely what must not be swallowed. A republished block
  must now either match what came before or overlap by at most one index at
  the join.

- **Nothing is silenced into invisibility.** The summary carries `rep=` and
  `seam=`, the payload keeps `blocks` / `repeated_entries` / `steps_multi_ts` /
  `defects`, and the timeline drilldown — which rendered parity *only* when the
  verdict was not `pass`, so this would have disappeared from the UI the moment
  it stopped alarming — now states both in plain language regardless of verdict.

- `docs/ARCHITECTURE.md`'s L3B section documented the old rule and still
  claimed the verdict was `fail` on mismatch, which v0.1.2 changed to `warn`.

### Added

- `validation_tests/test_forecast_parity.py`, run against four manifests
  captured from production rather than hand-written ones — the shape that broke
  this is the shape that matters, and an invented fixture is one that agrees
  with its author. Fixtures live in `validation_tests/fixtures/` because
  `samples/` is gitignored, and a test whose inputs are not in the repo passes
  only on the machine that wrote it.

## [0.4.2] — 2026-09-09

### Fixed

- **A restart inside the send hour re-sent the daily report to every
  recipient.** The "already sent today" guard lived in memory, the tick runs
  every five minutes, and the report is now enabled with four real recipients —
  so a deploy at 07:20 would find `hour == 7`, no memory of having sent, and
  mail the professor a second copy. v0.4.0's notes claimed deploys "cannot make
  it drift or double-send"; that was true of drift and of the DST boundary, and
  false of the restart, which is the one that actually happens.

  The guard is now a row in `settings.digest_state`, claimed **before** the
  send and in a single conditional statement:

  ```sql
  INSERT INTO settings (key, value, …) VALUES ('digest_state', …)
  ON CONFLICT (key) DO UPDATE SET value = …
   WHERE settings.value->>'last_sent_date' IS DISTINCT FROM $2
  RETURNING key
  ```

  No row returned means someone already claimed the day. Claiming first means
  the failure mode is a missing report, recoverable from
  `GET /api/report/daily`, rather than a duplicate to a mailing list, which is
  not recoverable at all. A database that cannot record the claim does not
  send. Verified against Postgres, not only against a stub: first claim wins,
  same-day re-claim returns nothing, next day wins.

  A schedule change still releases the claim, so moving the hour forward sends
  today rather than skipping it — and the test for that now uses a fresh task
  per case, because asserting the flag on a task whose previous line had
  already set it passes no matter what the code does.

- **`settings.digest` was stored as a jsonb string, not a jsonb object.** The
  digest's save path passed `json.dumps(cfg)` to a `$2::jsonb` parameter on a
  pool that already installs a jsonb codec, so the value was encoded twice —
  the exact pitfall `admin.py` carries a six-line warning about. It read back
  correctly, which is why it survived review; what it broke was SQL, and it
  broke it silently. `value->>'enabled'` on that row is NULL, so the row cannot
  be inspected or queried like its four neighbours — found while verifying
  something else and getting an empty result instead of an answer.

  New saves write a real object. The loader still unwraps a string, so an
  existing deployment keeps its recipients across the upgrade instead of
  quietly reverting to the disabled defaults.

## [0.4.1] — 2026-09-09

### Fixed

- **The README granted the Apache-2.0 licence and denied it, four lines
  apart.** v0.4.0's rewrite replaced the paragraph under `## License` and left
  the two beneath it, so the section read "Use it, run it, modify it, ship it"
  and then "No license is granted to use, copy, modify, distribute, host, or
  deploy the Software … essentially closed source."

  Worth a patch release rather than a quiet fix on `main`: the README is the
  first thing a reader checks before deciding whether they may run this, and a
  contradiction there resolves to "no" whatever `LICENSE` says. It survived the
  pre-publication audit because the search was for the word "proprietary",
  which the old text never used.

  The disclaimer those paragraphs also carried is kept and now matches
  `NOTICE`: written independently, not a work product of CSU, the CHILL
  facility, Dr. Chandrasekar's lab or the AQPI program, and not endorsed by
  them. That is a statement of fact, not a licence term.

### Added

- **`SECURITY.md`**, so vulnerability reports arrive privately instead of as
  public issues. It puts the monitored upstream explicitly out of scope —
  Sentinel reads CSU's public endpoints, and demonstrating a Sentinel bug by
  probing them is not in bounds — and draws the line that matters for this
  project: a deployment's own misconfiguration is out of scope, but Sentinel
  making such a mistake easy to commit *silently* is in scope and wanted.
  Rendered into the docs site as **Reference → Security policy**.

- `docs/index.md` states the licence. The docs site is a separate front door
  and never mentioned it.

## [0.4.0] — 2026-09-09

Reporting release, and the release that opens the source.

Sentinel has always been able to answer "is anything wrong right now". It could
not answer "how did the network do yesterday" without someone driving the UI,
which meant nobody asked. The daily report answers it by radar and by product,
unprompted, every morning.

Alongside that, the project is now Apache-2.0 and the repository is public, so
the lab can deploy from GHCR without a token and adapt the code to its own
upstream.

**Upgrading:** nothing breaks and no migration is needed. The report is off
until you enable it at `/admin/digest`, and it needs two things that are easy
to have already: SMTP configured at `/admin/email`, and `SENTINEL_PUBLIC_URL`
set — without the latter the report still sends, but every evidence link is
omitted, which removes the one thing that makes a claim checkable. Send
yourself a test copy from the admin page before adding recipients.

### Added

- **Daily activity report** (`/admin/digest`, off by default). One email each
  morning covering the previous 24 hours, with a row for every radar and every
  product, and a link from each row to the checks behind it.

  Organized by **subject, not by alarm**, which is the whole point. An
  alarm-centric summary of 2026-09-08 would have opened with "nothing needs
  attention" — every open alarm was acknowledged — while XSWR sat at 58.3%
  availability after a continuous 9 h 16 m outage with no open alarm at all.
  For the same reason an acknowledgment never removes a row: an ack means a
  human has seen it, not that it stopped happening.

  Every figure is stated in the terms an operator uses:

  - **Durations, not run counts.** "273 failed checks" says nothing, and
    calling them "missed scans" would be false — a failed check is not a
    missed scan.
  - **Sustained outages and momentary blips counted separately.** Grouping
    consecutive failures naively made XSCV look like it had 19 outages on a
    day it was fine; they were 19 isolated single-run blips totalling zero
    minutes.
  - **No internal vocabulary.** `GHOST_UP` reads as "reported online but sent
    nothing", matching the glosses already in `docs/04-faq.md`.
  - **Availability counts only runs that returned a verdict**, with the share
    excluded disclosed on the report — the same distinction between "we
    checked and it's fine" and "we stopped being able to see" that this
    release series has been correcting elsewhere.

  Config (recipients, hour, timezone, whether to include products) lives in
  `settings.digest` and is editable from the admin UI, so the lab can set it
  up without a redeploy. The daily send fires on a wall-clock hour in the
  configured zone rather than a 24 h interval, so deploys cannot make it drift
  or double-send, and the DST boundary cannot move it.

  A **test copy can be sent to one address** from the admin page — deliberately
  never the configured recipient list, so an operator can see the thing before
  the recipients do. Also renders a live preview without sending.

  `GET /api/report/daily` returns the same computed result as JSON, so the
  email and any other surface can never disagree about a number, and the tests
  assert against data rather than rendered text.

- **HTML email** support in `send_transactional`, as multipart/alternative
  with the text part first. The digest template is single-column table layout
  with inline styles, a dark-mode block, a small-screen block, and bars drawn
  as background-colored table cells rather than images or Unicode blocks —
  images are blocked by default in most clients and block glyphs render
  inconsistently. Kept to ~52 KB because Gmail clips a body over ~102 KB and
  would silently truncate the tail of the report.

### Changed

- **License: proprietary → Apache-2.0, and the repository is public.** The
  previous license granted nothing, which made the deployment docs contradict
  the intent: they explain how to run Sentinel against your own radar network,
  and the license forbade it.

  Apache rather than MIT for the explicit patent grant and the trademark
  reservation — both worth having if an institution adopts the code. `NOTICE`
  carries the attribution and states plainly that Sentinel monitors, and is not
  affiliated with, the AQPI network or Colorado State University.

- **GHCR packages are public,** so `docker compose pull` works with no login.
  `docs/02-deployment.md` previously warned operators *not* to make the
  packages public and documented a PAT with `read:packages` as the required
  path; that advice is now exactly backwards and is rewritten. The token path
  is kept for anyone running a private fork, which is the only case that still
  needs it, and `ops/preflight.sh` no longer reports privacy as intentional.

### Security

- **Redacted an email address left legible in a screenshot.**
  `docs/images/admin-audit.png` had ~26 rows of the audit log blurred and the
  last row missed — a full address, readable, at the bottom edge. Caught in the
  pre-publication audit; a public repository makes every commit permanent, so
  this had to be right before the flip rather than after.

  Also replaced a real person's address used as an input placeholder in the
  digest admin page with `ops@example.edu`.

  The rest of the audit was clean: no credentials in history, no key material
  in the tree (VAPID keys are generated at runtime into the database), no
  private addresses, internal hostnames or NFS paths, and CI using only the
  auto-provided `GITHUB_TOKEN`. The scan was validated against a known control
  string first — a malformed search returns zero hits and looks identical to a
  clean result.

## [0.3.1] — 2026-09-05

### Fixed

- **Historical tilt lookups could not reach the archive they were stored in.**
  `tilt_image.png?time=<ISO>` built an exact-second archive key, but frames are
  captured on radar-display's own ~140 s cadence — so a request could only
  succeed if it named a capture instant precisely. Verified against production
  immediately after the v0.3.0 deploy: a request 40 minutes back returned 404
  while the frames plainly existed (`20:59:56`, `20:57:36`, `20:55:16`, …).

  The storage side of "the archive is deeper than the origin" was working; the
  retrieval side was not, which made the claim true and useless at once.
  Archive-only requests now resolve to the nearest captured frame within 10
  minutes, and return 404 beyond that rather than serving something
  arbitrarily stale as though it were what was asked for. The frame actually
  served is reported in `x-tilt-ts`.

  Confirmed live: 20, 30 and 45 minutes back all resolve from disk with no
  upstream request; two days back still 404s.

## [0.3.0] — 2026-09-05

Archive-completeness release, plus three surfaces that were reporting health
they had not observed.

Sentinel kept only what someone had looked at, so the archive was dense in
Reflectivity and nearly empty everywhere else, and per-elevation tilt imagery
was never stored at all. It now captures every published moment and tilt, and
the tilt archive reaches further back than the origin's own 16-minute window.

The rest of the release is a single recurring mistake in three places: a skip
is not a pass, an unfed sparkline is not a stopped upstream, and an empty
volume is not an empty archive. Each looked like health.

**Upgrading:** `SENTINEL_PREWARM_ENABLED` is off by default and should stay
off unless you have read its cost in `docs/91-env-vars.md`. If you deploy from
`docker-compose.ghcr.yml`, re-read your `SENTINEL_ARCHIVE_HOST_PATH` before
restarting — that file could not previously express it.

### Fixed

- **`docker-compose.ghcr.yml` promised parity with `prod.yml` and did not have
  it.** Its header invites an operator to switch between building locally and
  pulling images by changing the `-f` argument alone. But it hard-coded
  `sentinel_archive:/data/archive` with no host-path indirection, while
  production points that at an NFS share holding 22 GB of imagery — so the
  switch would have attached an empty named volume instead. The stack comes up
  healthy, the gallery is empty, and new captures land on the container disk
  while the real archive sits unreferenced. Nothing errors, because a fresh
  archive and a detached one are indistinguishable. It was also missing
  `/data/cold` and all three retention variables, so the offload that keeps
  the hot database bounded would have silently stopped.

  `validation_tests/test_compose_parity.py` now enforces the claim: matching
  environment keys, matching mount points, and no mount pinned where the other
  file takes a host path.

- **radar-display TLS verification is back on.** The client was created with
  `verify=False` in 2026-05 for an expired certificate. That certificate was
  renewed on 2026-07-14 and is valid to 2026-10-12 — a verified request
  returns 200 — so the workaround outlived its cause by about seven weeks
  without anyone noticing, because nothing was watching for the condition to
  clear. A disabled safety check with no expiry is indistinguishable from a
  permanent one.

  New `layer0.net.radardisplay_tls` verifies the certificate hourly, warns 14
  days before expiry and fails once it lapses, so the next renewal is seen
  coming instead of arriving as every tilt request failing at once.
  `SENTINEL_RD_VERIFY_TLS=0` restores the old behavior if it does lapse —
  an env var rather than a source edit, so the choice stays visible in the
  deployment.

- **The WebSocket `hello` frame reported `0.1.0`,** hard-coded and unrelated
  to `_version.py`. It now reports the real version, which was the third
  independent copy of that string found in two days.

- Two Svelte 5 reactivity warnings: `mapDiv` is now `$state` (a `bind:this`
  target), and `ReportExportModal`'s intentional initial-value capture is
  annotated rather than left looking accidental — an `$effect` re-syncs the
  props on open, which is the only moment new defaults should win.

- **Skips rendered as green on the bucketed timeline.** Two separate paths,
  both asserting we had checked and found nothing wrong when in fact we had
  declined to judge:

  - `cellFill` hard-coded the remainder above a defect band to the pass
    color, so a bucket that was 20% fail and 60% skip drew 20% red over 80%
    green.
  - `pass` outranks `skip` when picking a bucket's worst status, and `pass`
    was in `FLAT_STATUSES`, so any bucket containing even one pass was drawn
    as a single flat green block with its skips erased entirely. This was the
    common case.

  Measured over 24 h at 1 h grain: 141 of 1,093 buckets contained a skip; 59
  rendered as solid green and 2 more as a green remainder. A real example from
  production — `layer0.origin.latency`, 45 runs of which **33 were skipped** —
  drew as `var(--color-ok)`, indistinguishable from a fully healthy hour.

  Cells are now drawn from their composition: the defect band at the bottom,
  then skips in gray, then the share that really passed. The server sends
  `n_skip` alongside the existing per-status counts to make that possible.

  This is the same mistake the alarm engine was making by closing alarms on
  demoted skips, and it matters more here — the grid is what an operator scans
  to decide whether to look closer at all, so a blind spot that looks healthy
  is the one thing it must never draw.

  Ambiguity resolves the opposite way from `badFraction`: a server too old to
  send `n_skip` grays nothing, because gray means "no data" and inventing it
  would be its own lie. The exception is a `skip`-status cell, which can only
  mean every run skipped. Skips also yield to the bad band rather than the
  reverse, so one failure among 999 skips still draws its `MIN_BAD_PX` floor.

- **Sparklines drained away while an operator watched a healthy system.** They
  were seeded once at page load and thereafter fed only by WebSocket `run`
  events — which the backend broadcasts **only when a check's status
  changes**. On a healthy system that is almost never. Measured on production:

  | | |
  |---|---|
  | Runs in 2 h | 2,698 |
  | Of those, status changes (i.e. broadcast) | 104 — **3.85%** |
  | Live WS capture, 120 s | 2 `run` events, **4 metric samples** total |

  With ~18 sparklines on the page, most received nothing at all. Meanwhile
  `Sparkline` advances its own `now` every 5 s, so the window kept sliding
  while no samples arrived: the trace drained from the right and eventually
  emptied.

  That is worse than a blank decoration. An emptying right edge is exactly
  the shape the component uses to mean *"this upstream has stopped"* — the
  signal it was rewritten to show after the 2026-05-19 outage, where a frozen
  trace fooled the operator. The bug made healthy checks wear the appearance
  of dead ones, and it looked most convincing precisely when someone sat and
  watched, which is when they were most likely to believe it.

  Fixed by polling, not by broadcasting more: streaming every run is what
  wedged the browser tab at ~30 events/minute, which is why the
  transition-only filter exists. New `GET /api/checks/-/metrics_recent`
  returns many series in one request, with `since` so a steady-state poll
  carries a handful of points rather than the whole window, and the dashboard
  refreshes sparklines on the existing status tick and on tab re-focus.
  Samples already delivered by the WebSocket are de-duplicated by timestamp.

### Added

- **Tilt imagery is now cached and archived like everything else.** The
  per-elevation PPI stack from radar-display went straight upstream on every
  request with `cache-control: no-store` and none of the protections the
  radarca image paths have had since 2026-08-27 — no LRU, no single-flight, no
  negative cache, no concurrency cap and no archive. Scrubbing a tilt loop is
  exactly the workload those were built for. `tilt_image.png` now resolves
  through the same `_serve_source` ladder (LRU → archive → upstream) and
  reports its provenance in `x-sentinel-cache`.

  Two things about tilts made this more than a wiring change:

  - **radar-display stamps frames in Mountain time.** `Time.Value` is a naive
    string with no offset and no zone — and it is neither UTC nor the radars'
    own Pacific: the radars are in California, but radar-display is hosted at
    CSU. Verified live, frame 0 read `13:56:55` while UTC was `19:58:02`. A
    naive parse files every frame 6–7 hours out, with the error moving twice a
    year with DST.
  - **Frame indices are positions, not identities.** `..._0.png` means
    "newest" and the window shifts every ~140 s, so the same URL names a
    different image minute to minute. Archive keys are derived from capture
    time instead, which is also what makes history queryable.

  Because every served frame is archived, `tilt_image.png?time=<ISO>` reaches
  back past radar-display's own 7-frame (~16 min) window — **the archive is
  now deeper than the origin.** Frames outside the live window are served from
  the archive or 404; they are never re-requested upstream, because no URL
  still names them.

- **`SENTINEL_PREWARM_ENABLED` — continuous capture of every moment and
  tilt.** The archive was demand-driven, so it was dense in Reflectivity and
  empty elsewhere: in the 7 days to 2026-09-05 it gained 19,540 Reflectivity
  frames against 36 Velocity, 35 ZDR, 35 PhiDP and 1 RhoHV. The moments an
  operator would most want to compare against reflectivity were precisely the
  ones not kept, and the first look at any of them was also the slowest.

  A sweep walks 25 moment streams (X-band ×4, CBAND ×5) and 60 tilt streams
  (5 radars × 4 elevations × 3 moments), skipping combinations that do not
  exist upstream — X-band publishes no RhoHV, and requesting it anyway would
  404 five streams on every sweep forever.

  **It ships off.** It is the only thing in Sentinel that generates upstream
  traffic nobody asked for, and the increase is not marginal: ~4,000 →
  ~49,000 images/day, ~280 MB → ~1.1 GB/day, against two systems belonging to
  other people. It reuses the same LRU, archive and single-flight as operator
  traffic, so steady-state cost is one fetch per genuinely new frame rather
  than one per sweep. `prewarm.fetched` vs `prewarm.already_had` in
  `/api/_debug/stats` shows whether that is holding.

### Fixed

- **`/api/version` reported `0.2.0` on v0.2.1 and v0.2.2.** Nothing in CI
  compares `backend/_version.py` to the tag, and the release process never
  mentioned bumping it — the step lived only in `_version.py`'s own docstring,
  which pointed at the release doc, which pointed back. Both releases shipped
  a build that misreported itself. The step is now step 0 of the documented
  process, with the reason it is easy to miss.

## [0.2.2] — 2026-09-05

Backup-integrity release. v0.2.0 hardened `ops/backup.sh` and, in the same
commit, changed the defaults it ran with — so the new guard began refusing to
write to a destination that commit had just repointed at local disk. Nightly
backups aborted for two days on the reference deployment and nothing said so,
because the check built to announce exactly that was never wired up.

**Upgrade if you take backups.** The mount-guard defaults changed, and the
freshness check now needs a directory mounted at `/data/backups` to work at
all. Existing dumps are unaffected — nothing in this release touches, moves or
prunes them differently.

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
  when it answered fine but omitted a radar, labeling both "radar-status API
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

[Unreleased]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.4.13...HEAD
[0.4.13]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.4.12...v0.4.13
[0.4.12]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.4.11...v0.4.12
[0.3.1]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/jkmesches/AQPI-Sentinel/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jkmesches/AQPI-Sentinel/releases/tag/v0.1.0
