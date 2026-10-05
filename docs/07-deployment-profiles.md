# Deployment profiles

One image, more than one radar network. `SENTINEL_PROFILE` selects which.

Unset or `aqpi` is the AQPI network, monitored through radarca — every
deployment before v0.6.0, unchanged. `xqpi` is the FLOW radar at JPL,
published to a trinity filesystem tree.

An unrecognised value **refuses to start**. An instance quietly watching the
wrong radar network is worse than one that will not boot.

---

## Why this is not just a config file

FLOW differs from AQPI in the way that matters most: **nothing serves it over
HTTP.** There is no radarca in front of it. Products land on a filesystem tree
and that is the whole delivery path.

So roughly two thirds of the check stack has no subject under `xqpi`. A check
with no subject does not go quiet — it fails every cycle, forever, and L0
failures cascade-suppress the backend checks that *are* working. The profile
therefore decides which checks exist at all, not merely what they point at.

| | `aqpi` | `xqpi` |
|---|---|---|
| checks registered | 64 | 9 |
| stages | L0, L1, L2, L3, L4, LB1, LB2, LB3 | L0, LB1, LB2, LB3 |
| display tier | radarca + radar-display | none |
| products | 13 | 4 |
| radars | 6 monitored (9 drawn) | FLOW |
| freshness basis (products, LB1) | file mtime | the observation time the data declares |
| freshness basis (radar arrival, LB2) | the observation time the data declares | the observation time the data declares |

---

## What a profile owns

A profile is a module under `backend/profiles/`. It is **pure data and must
not import `config`** — `config` imports it, so the dependency runs one way.

`config.py` rebinds these when a profile supplies them:

| Name | What it decides |
|---|---|
| `PRODUCTS` | the product table, wholesale |
| `RADAR_FOLDER`, `RADAR_SILENT_FAIL_S` | radars and their silence limits |
| `RADAR_META_OVERRIDE` | map geography; **replaces** the built-in table rather than adding to it |
| `SITE_NAME`, `DATA_SOURCE` | what the UI calls this deployment and its source |
| `HAS_RADARCA` | whether a display tier exists — gates whole check families |
| `PRODUCT_IMAGES_PREFIX`, `RADAR_DATED_TREE` | where published files live |
| `LB1_FRESHNESS`, `LB2_FRESHNESS` | how freshness is derived (see below) |
| `COMPOSITE_EXPECTED_RADARS` | which radars LB3 expects in the composite receipt, and the directory each appears under |
| `COMPOSITE_RECEIPT_ALT` | a second receipt this profile writes but does not monitor |
| `DROPS_TREE` | the QPE producer's output tree, or `""` for a profile with no such step |
| `HOME_VIEW`, `MAP_OVERLAYS`, `COMP_EXTENT` | the map |
| `PRODUCT_LABELS` | picker labels, when the product ids are not AQPI's |

Everything the frontend needs is served on `/api/version`, so the UI adapts
from one source rather than carrying its own copy of the answer.

---

## Adding a profile

1. Write `backend/profiles/<name>.py` as pure data.
2. Add the branch in `config.py` and the name to the error message.
3. Run `validation_tests/test_profiles.py`. It asserts the AQPI default is
   untouched, that the new profile registers only checks it can observe, and
   that every radar it monitors has map geography.

The test is the point. It loads each profile in a subprocess — `config` reads
the environment at import time, so one interpreter holds one profile — and
checks **both** directions: that nothing inapplicable survives into the new
profile, and that nothing the existing one relies on was gated away. The second
is the dangerous direction, because nothing turns red.

---

## How a check is excluded

Two mechanisms, and only one of them is a guarantee.

`backend/checks/__init__.py` skips importing the modules in
`config.RADARCA_ONLY_MODULES` when `HAS_RADARCA` is false. That is an
optimisation.

`registry.register()` **refuses** a check whose defining module is in that list
when `HAS_RADARCA` is false. That is the guarantee, and it is enforced at the
one place every check passes through.

Both exist because the import skip is not sufficient. "This module is not
imported" is a property of the whole import graph, not of the gate: `prewarm.py`
imports `layer2_radar` at module scope for one constant, which executed its
registrations regardless and put two permanently-failing checks into a live
XQPI instance with the gate apparently in place.

Declined checks are recorded in `registry.DECLINED` rather than vanishing, so
*why is this check missing* has an answer.

!!! warning "A check in neither list is never imported"
    `_ALWAYS` in `backend/checks/__init__.py` holds modules that read the
    filesystem or the host; `config.RADARCA_ONLY_MODULES` holds those that talk
    to the display tier. A module in neither is silently absent —
    `test_profiles.py` fails on that rather than letting its checks go quiet.

---

## Freshness: where mtime is a sound basis, and where it is not

On K2, mtime means what it says. Measured across all 31 frames of three
products: every frame is written 2.25–2.48 min after its own declared time,
with 0.04–0.18 min of spread. A fixed publish latency.

On trinity it does not, for two separate reasons:

- a root **gzip sweep** rewrites the archive daily around 07:25 UTC, touching
  the current day's directory too;
- the publisher **re-touches its newest frame** each cycle. Observed during a
  two-hour stall: newest frame mtime 1 minute old, declared timestamp 121
  minutes old.

The second is the subtle one. It leaves newest-by-mtime and newest-by-filename
pointing at the **same file**, so comparing those two finds nothing while mtime
reads minutes and the data is hours stale.

!!! danger "Comparing newest-by-mtime to newest-by-filename is not a test for masking"
    It was used as one, on both deployments, and it would have passed on a
    product that was two hours stale. The test that discriminates is the
    per-frame offset between mtime and declared time: re-touching puts the
    newest frame's offset near zero and scatters the rest.

So `xqpi` reads the observation time the data declares — the manifest's
per-step timestamp for products, the filename for raw volumes. That also
sidesteps the orphans these trees accumulate: the published directory keeps
frames the rolling window never reclaims (26 files against a manifest of 14–15),
so **the manifest is the product and the directory is a cache with litter in
it.** The same applies when measuring these trees: iterate the manifest, do not
glob the directory.

### `aqpi` LB2 also reads the declared time — for a different reason

As of 2026-10-05 `aqpi` sets `LB2_FRESHNESS = "filename"` too, so the table
above splits. The reason is **not** the one above. On AQPI's raw arrival trees
mtime is sound: declared and mtime ages agreed within about half a minute when
measured. The problem is what else is in those directories. Each carries
126–149 **in-flight dotfiles** at any moment, and a newest-by-mtime read lands
on one of those rather than on a completed volume — so the basis changed to
one that cannot see a partial write, because the pattern is anchored at the
start of the name and a dotfile cannot match it.

The two profiles therefore arrive at the same basis from opposite directions:
`xqpi` because mtime lies, `aqpi` because mtime is honest about the wrong file.

### The pattern is per radar, not per profile

`RAW_VOLUME_TS_RE` is a dict keyed by radar id, and every radar in
`RADAR_FOLDER` must have an entry — `config.py` raises at import if one is
missing, because a gap would otherwise surface once per cycle as a broken
mount rather than as the config error it is.

It has to be per radar because AQPI's two producers name their files
differently and nothing reconciles them:

```
aqpi.scvw-20261005-162541_317_2_2_PPI.netcdf   the five X-bands
AQPI.SSCB_20261005_162356.nc                   CBAND
```

Lowercase against uppercase, hyphen against underscore. A single pattern
fitted to the X-bands matched **0 of 295** CBAND files, and an unmatched
directory makes LB2 report "no data directory for the current UTC day"
against a directory full of current data.

---

## LB3: the gap between arrival and publication

LB2 says a radar's data is landing. LB1 says a product is publishing on time.
Both can be green while the product is quietly computed from fewer radars than
it claims, and until 2026-10-05 nothing in Sentinel looked in between.

Two checks live there, and they are deliberately routed differently.

### Composite participation (one per radar, paging)

Two sources, and the better one answers a stronger question.

**Primary — the composite's own per-run log** (`COMPOSITE_RUN_LOG_DIR`, in
practice `PRODUCTS/Composite_QPE/log_SRI`). One file per run, ~720/day,
retained about 7 days, with a block per radar the composite actually processed:

```
************************** RADAR: XEBY **************************
filename: .../recentfiles//XEBY/XEBY_volume_20261005-170754_drops.nc
GetNetCDFdim: #radials = 2719  #gates (rangebins) = 675  #sweeps = 4
radar height = 608.7m
radar lon, lat = -122.062,37.8156
startDateTimeScan  = 2026-10-05T17:07:54Z
endDateTimeScan    = 2026-10-05T17:12:00Z
secondsStarttoEnd = -246
```

`GetNetCDFdim` with real dimensions means the composite **opened the file and
read its header**, so a block is evidence of *ingestion*. That lets the check
say "in composite" rather than "offered to" it, and it exposes a third state
the receipt could never see: a block present with **no** dimensions means the
composite tried this file and failed on it — worse than absence, and reported
separately as `NAMED BUT NOT READ`.

These files are written once per run, 120 s apart, so **reading the
second-newest is always a complete file.** That retires the torn-read problem
rather than defending against it. The cost is up to ~240 s of reporting lag
against a 900 s band, accepted deliberately: a late correct answer beats a
prompt one computed from half a file.

!!! warning "`secondsStarttoEnd` is used as an age, and the label says otherwise"
    Read literally the field is start-minus-**end-of-scan**, i.e. a scan
    duration. The evidence that it is an age is that one radar's distribution
    reaches 64,828 s — an 18-hour span is impossible for a 4-sweep X-band
    volume, so `endDateTimeScan` must be the composite's own reference time
    rather than anything read out of the file. That inference is sound but it
    *is* an inference, so the check derives an age independently from the run
    time minus the recorded scan start, carries both in the payload, and
    **flags a disagreement** instead of silently banding whichever it
    computed first.

**Fallback — the input receipt** (`COMPOSITE_RECEIPT`). Used only when the
run-log directory is absent or unreadable, and tagged in
`payload.participation_source` rather than substituted silently, because the
two sources support different claims. It records **intent, not outcome**: the
driver writes it and reads it back, so it says what `ls -1rt | tail -1`
selected. Its file shape has three hazards, each of which would break a
plausible parser — the radar is spelled three ways inside the filename while
the directory is uniform (so identity comes from the directory),
`recentfiles//XSCR` carries a real double slash, and the timestamp separator
differs by convention. And it is truncate-then-append with no rename:

```sh
echo "Radar files for composite:" > $filelist          # truncates
ls -1rt .../$radar/*_drops.nc | tail -1 >> $filelist   # appends, per radar
```

A read landing in that window sees a short file, and the bias is **not
random**: the appends run X-band, then NEXRAD, then SSCB, so a torn read
systematically under-reports CBAND. Completeness cannot be judged from the
content — a complete file and a nearly-complete one differ by exactly the line
you would look for — so a settle window covers *arrived mid-write* (where the
mtime is momentarily stable between appends, and a bracket is blind) and a
stat bracket covers *the write started during our read*. The 5-minute alarm
hold-down is the third layer.

### One uniform band, and why per-radar was wrong

`COMPOSITE_CONTRIB_WARN_S = 600`, `COMPOSITE_CONTRIB_FAIL_S = 900`. Not per
radar — that was the first design and measurement killed it. Over 2 complete
days, 1,440 runs, 8,298 contributions:

| radar | present | p50 | p90 | p99 | max | fires at the derived band |
|---|---|---|---|---|---|---|
| XSCR | 100.0% | 202s | 260s | 263s | 403s | **0.00% — can never fire** |
| SSCB | 100.0% | 320s | 408s | 464s | 1424s | 0.07% |
| XSWR | 97.8% | 171s | 4880s | 76376s | 83696s | 16.62% |
| XSCV | 97.0% | 111s | 204s | 71153s | 71153s | 4.94% |
| XEBY | 96.2% | 249s | 321s | 2829s | 3983s | 4.11% |
| XSCW | 85.2% | 206s | 64779s | 64828s | 64828s | 25.26% |

A band of *that radar's silence limit + a pipeline budget* is **blind on
XSCR** — max 403 s against a 1,080 s limit, on the one radar that is never
late — while firing on a quarter of XSCW's runs. The derivation was
principled; its input was not. `RADAR_SILENT_FAIL_S` was fitted to radarca
*reporting* cadence, so building a composite-ingestion band on it is a
borrowed figure one level removed from what it describes.

A percentile band is worse: 2× p90 hands XSCW a **36-hour** limit, because its
p90 *is* the pathology. A percentile band fails exactly when the tail is the
fault.

**The per-radar variation is the signal.** A band tuned to each radar's own
history would silence precisely the radars that misbehave — it would encode
XSCW's 18-hour staleness as normal for XSCW. Under one uniform band XSCR and
SSCB stay quiet because they are never stale, and XSWR/XSCW/XSCV are loud
because they are. 900 s sits in the thinnest part of the pooled distribution
(`[600,900)` holds 0.39%) and clear of the healthy mode.

!!! note "The ~8% fire rate measures granite, not this threshold"
    The composite applies **no staleness guard** to its radar inputs
    (`ls -1rt | tail -1`, any age), which is how an 18-hour-old volume gets
    blended into a composite reporting itself current. Add the guard and the
    tail disappears and these alarms stop. The rate is the detector reading
    the defect it was built to detect — not a number to tune away.

### Why NEXRAD is not in the expected set

The driver's own arrays intend nine inputs:

```sh
radarX=("XEBY" "XSCR" "XSCV" "XSCW" "XSWR")
radarS=("KBBX" "KDAX" "KMUX")
radarC=("SSCB")
```

The three NEXRAD directories under `web-files/NEXRAD_L2` **do not exist at
all**, and their `recentfiles` trees hold zero files. That predates the 13 days
the rotated log covers, so it is a standing condition rather than an incident.

Deriving `expected` from those arrays would make the check fire immediately and
permanently from its first cycle — which is how a check becomes one people
learn to scroll past, the exact failure we were repairing in LB2. So
`COMPOSITE_EXPECTED_RADARS` is an explicit list of the radars a profile
monitors for arrival **and** the composite is configured to include. Whether
S-band input is still intended is a real question for the AQPI team, raised
separately rather than answered by a monitoring threshold.

`xqpi` excludes `KSOX` and `KVTX` for a different reason: they *are* in its
receipt, as 2026-09-17 volumes being consumed 18 days later by a composite that
applies no staleness guard to its radar inputs. Sentinel does not monitor
either radar's arrival, so it has no band to judge them by and no business
opening an alarm it cannot characterise. The stale volumes are a real finding;
they belong in a report to the composite's owner, not in a check that fires
forever.

### The QPE producer (one check, informational, non-paging)

`DROPS` holds `Gen_X-band_QPE.py`'s output, and nothing in the live product
chain reads it. LB2 used to point there by mistake, which is how one dead
script read as five radar outages for twelve hours. The lesson is not "stop
watching DROPS" — it is **watch it at the right severity**.

So the check caps its own status at `warn` (which maps to `info` and is never
auto-promoted, where a `fail` is promoted to critical after 30 min), *and*
`alerts.yaml` routes its check id to a non-escalating policy. Two independent
statements of one intent, deliberately: whichever a future editor removes, the
other still holds. The summary states the real duration in words, so the row is
never gentler than the fact.

One check for the whole producer rather than one per folder — the folders
freeze together because it is one process, so per-folder rows would be five
restatements of one fault.

---

## Figures carry their provenance

`backend/profiles/xqpi.py` tags every constant:

- `[M]` measured here, against the live tree or the files' own headers
- `[V]` taken from the survey **and** independently reproduced here
- `[Q]` taken from the survey, not independently checked

This exists because a borrowed number and a measured one are indistinguishable
once both are constants in a file. A `max 12 min` quoted from a survey sat
thirty lines from a 28-minute gap measured directly, in the same file, and the
two were never read against each other.

Tagging also made the set enumerable, which surfaced that the untested figures
were **exactly the four thresholds that gate alarms** — the interesting values
had been checked and the load-bearing ones had not. Characterise a threshold
before the check that consumes it exists: afterwards it costs a reprocess of
every stored verdict, and an explanation of why last week's alarms were wrong.
