# FAQ for the lab team

Written for people who use Sentinel's readings rather than its code: what it
watches, where the numbers come from, and how it decides something is wrong.

If you want the short version: Sentinel independently re-derives the health of
`radarca.engr.colostate.edu` from that site's own public APIs, every couple of
minutes, and keeps the evidence.

---

## 1. What is Sentinel actually watching?

The **public-facing radarca service**, not the radars directly.

Sentinel has no privileged access to CSU-CHILL infrastructure, no feed from
the radar processors, and no back door. It reads exactly what a browser
visiting radarca would read, and reasons about it.

That is a deliberate limit and it shapes every answer below. When Sentinel
says "XSWR is not reporting," the precise claim is *"XSWR's data is not
reaching radarca's public API,"* which could be the radar, the ingest
pipeline, the publishing step, or the API. Sentinel can usually tell you
**which** of those by correlating across radars and products — see Q7 — but
it cannot see inside the pipeline.

## 2. Where does the data come from?

Five upstream endpoints, all public:

| Endpoint | What we take from it |
|---|---|
| `/api/radar-status/` | Each radar's **declared** status (UP/DOWN) |
| `/api/xbandRadarImages/` | Per-radar, per-moment list of published scan filenames |
| `/api/productDetail?file=…` | Per-product step lists and timestamps |
| `/api/imageData?file=…` | The product/scan PNGs themselves |
| The rendered site (Playwright) | Whether overlays actually draw in a real browser |

Scan timestamps come from the **filenames** (`..._20260826-0049.png`), which
is how we can tell "the newest scan is 6 minutes old" rather than merely "some
images exist."

## 3. How often does it check, and does that load your servers?

Cadences run from 30 s (is the internet up) to 1 h (slow forecast products);
most radar and product checks run every **120 s**. Total outbound traffic is
roughly **30 requests/minute**.

We actively work to keep that low. All six radar checks used to fetch
`/api/radar-status/` independently — 4,320 calls/day where 720 suffice — and
now share one memoised fetch per cycle. If our traffic is ever a problem for
you, tell us; the cadences are configuration, not architecture.

## 4. What do the five stages mean?

Each check belongs to a stage, ordered from "is anything reachable" to "does
the imagery look sane":

| Stage | Name | Question it answers |
|---|---|---|
| L0 | Connectivity | Is the internet up, DNS resolving, radarca reachable? |
| L1 | Product Freshness | Is each product publishing recent, correctly-sized data? |
| L2 | Radar Scans | Is each radar actually emitting scans? |
| L3 | Map Overlays | Do overlays render in a real browser? |
| L4 | Image Quality | Does the imagery itself look plausible? |

The stages are also a dependency chain. If L0 says the origin is unreachable,
downstream failures are collateral, and Sentinel marks them as such instead of
painting forty red cells for one root cause.

## 5. What is the difference between fail, error, warn and skip?

This distinction matters more than it looks, and Sentinel got it wrong until
August 2026 — `error` used to be rendered in the same red as `fail`, which
made ~1,500 upstream API timeouts a day read as radar outages.

| Status | Means | Example |
|---|---|---|
| **pass** | Checked, healthy | Scans arriving on time |
| **warn** | Degraded, not broken | Declared DOWN but data still flowing |
| **fail** | **The monitored thing is broken** | Radar declared UP, no scans |
| **error** | **We could not determine its state** | Upstream API timed out |
| **skip** | Deliberately not evaluated | Suppressed by an upstream failure or a silence |

`fail` is a statement about radarca. `error` is a statement about *our
visibility*. Treat a screen full of `error` as "Sentinel is flying blind,"
not "everything is down."

## 6. How do you decide a radar is down?

Sentinel reconciles two independent facts: what radarca **declares** the
radar's status to be, and what it **observes** in the published scans.

| Declared | Scans arriving? | Verdict |
|---|---|---|
| UP | yes | `HEALTHY` |
| UP | no | **`GHOST_UP`** — claims up, silent |
| DOWN | no | `CONFIRMED_DOWN` — known outage, correctly reported |
| DOWN | yes | `STUCK_DOWN_FLAG` — the status flag is stale, data is fine |
| — | can't tell | `OBSERVED_API_ERROR` |

`GHOST_UP` is the one worth paging on: it is the failure mode the status feed
itself will not tell you about.

Note that `CONFIRMED_DOWN` is *not* alarming in the same way. If a radar is
down and radarca says it is down, everything is working as designed — the
outage is real but nobody is misinformed.

## 7. Why is one radar's threshold different from another's?

Because "how old is too old" genuinely differs per radar, and because of a
subtlety that cost us a lot of false alarms.

The check asks: *how old is the newest published scan?* That age contains two
things — the radar's scan interval, **and upstream's publication lag** between
a scan happening and appearing in the API. On this fleet the lag runs roughly
300–500 s.

Thresholds calibrated in May were derived from scan cadence alone (~120 s).
The result: XSWR scans every 120 s and delivers ~27 images per poll — a
perfectly healthy radar — but its 240 s threshold was below the publication
lag, so it reported `GHOST_UP` on **96%** of checks. Recalibrated in August
against measured age instead, with 6,406 historical verdicts corrected.

Current values (seconds), re-tuned monthly:

| XEBY | XSCV | XSCW | XSCR | XSWR | CBAND |
|---|---|---|---|---|---|
| 300 | 660 | 720 | 780 | 660 | 1080 |

**Raising a threshold cannot hide an outage.** A radar publishing *no* images
is marked not-fresh regardless of any threshold. Thresholds only govern the
"images present but not advancing" case — a frozen feed — where the cost is
detection latency, not detection.

## 8. Five radars went red at once. Is that five outages?

Almost certainly one.

Radars at Santa Cruz, Sonoma, East Bay, Santa Clara and Sierra do not fail in
the same second. Measured over 14 days, **80%** of all `GHOST_UP` readings
happened while four or five radars were silent simultaneously; only **3%**
were isolated to one radar. The clearest case: four sites entered `GHOST_UP`
at `2026-08-13 12:29:52` and left it at `2026-08-16 19:29:54` — sub-second
alignment, 79 hours apart.

Sentinel now runs a fleet-correlation check that recognises this shape and
raises **one** systemic alarm instead of five, while still showing each
radar's individual verdict. An isolated single-radar failure is unaffected and
still pages normally.

Worth saying plainly: that 79-hour episode was a **real** data outage. The
correlation logic makes the count accurate, not the problem smaller.

## 9. Can a past verdict change? Is the history trustworthy?

Yes, verdicts can be re-derived — and the history is trustworthy *because*
of how that works.

When a threshold is corrected, Sentinel can re-evaluate historical runs under
the new value, so the record reflects our best current understanding rather
than a setting we have since learned was wrong. Three rules make that safe:

- **Raw observations are never modified.** The scan count and newest-scan
  timestamp are what we measured; only the *interpretation* is recomputed.
- **The original verdict is preserved** in the row, including the threshold
  that produced it. You can always see what we said at the time and why.
- **Runs where no data existed are never reclassified.** A genuine stoppage
  cannot be tuned away.

The August recalibration is a worked example: 231,430 rows re-evaluated,
6,406 corrected (all `GHOST_UP` → `HEALTHY`), zero rows deleted, and the
79-hour outage still recorded in full across all five radars.

## 10. Who gets alerted, and how do I stop being paged for something I know about?

Alarms route by **severity tier**:

- **Action required** — broken (`fail`/`error`). Auto-escalates after 30 min.
- **Attention required** — degraded (`warn`). Visible, doesn't page.
- **Informational** — healthy or out of scope.

Recipients, escalation steps and on-call schedules live under `/admin/alerts`
and `/admin/groups`, including recurring quiet hours and per-device routing on
mobile.

For planned work, use a **silence** (`/admin/silences`) rather than muting a
check permanently — silenced alarms still appear on the dashboard and in the
record, they just stop paging. If you are seeing noise you believe is wrong,
that is worth reporting rather than silencing: two of the largest sources of
false alarms found so far were a stale threshold and a colour that made
"couldn't measure" look identical to "broken."

---

## 11. What happens to an image between radarca publishing it and a verdict appearing?

Seven steps, every cycle, for each X-band radar and each mosaic product.

| Step | What happens |
|---|---|
| 1. Resolve | Ask radarca which scan is *latest* — `xbandRadarImages` for a radar, `productDetail` for a mosaic product. |
| 2. Fetch | Pull the PNG bytes. This is the only upstream request the image checks make. |
| 3. Archive | Hash the bytes with SHA-256 and store them content-addressed. Identical bytes are stored once. |
| 4. Tier 1 | Measure the frame — coverage, intensity, structure, perceptual hash. No judgement yet. |
| 5. Tier 2 | Run the pathology detectors against those pixels. |
| 6. Compare | Check the perceptual hash against the previous run to catch a stuck feed. |
| 7. Verdict | Take the worst sub-verdict. Anything not on the OK list becomes a `warn`. |

The split between steps 4 and 5 is deliberate and load-bearing. **Tier 1
records what the frame is** and is threshold-free. **Tier 2 decides whether
that is a problem**, and every tunable number lives there.

The payoff is that when a threshold turns out to be wrong — which has happened
more than once — the judgement can be re-run over stored measurements without
re-fetching a single image from radarca. That is how 13,575 rows were corrected
in August without adding any upstream load.

!!! note
    Image checks emit `warn`, never `fail`. A strange-looking frame is a reason
    for a human to go and look, not a claim that the product is broken.
    Sentinel cannot distinguish an artifact from genuinely unusual weather, and
    it should not pretend otherwise.

---

## 12. What tests do you actually run on the image itself?

### Tier 1 — measurement

A pixel counts as **active** where its alpha channel exceeds 10. These are
recorded for every frame, whether or not anything is wrong:

| Measurement | What it captures |
|---|---|
| Coverage % | Share of the canvas carrying data. Drives most of the suppression logic in §13. |
| Mean / std intensity | Brightest channel per active pixel — overall level and spread. |
| Top-3 intensity bins | Where the intensity histogram piles up, in 16 buckets. |
| Horizontal autocorrelation | Whether neighbouring pixels agree. Structured weather correlates; noise does not. |
| Perceptual hash | 16×16 pHash. Two frames with the same hash are visually identical. |
| SHA-256 + byte size | Exact identity of the file, for the archive and for dedup. |

### Tier 2 — judgement

Four detectors, each with a tunable threshold:

| Detector | Trips when | What it catches |
|---|---|---|
| **Saturation** | One quantised colour holds >40% of active pixels | A frame collapsed to a single value — a stuck colour map or an encoder fault. |
| **Speckle** | >35% of active pixels have no active 4-neighbour | Noise dressed as data: isolated pixels with no structure. |
| **Range ring** | Peak ring deviation >0.80× the median across 50 polar bins | Concentric artifacts on an X-band disc — a calibration or clutter-filter signature. X-band only: mosaics have no radar-centred geometry. |
| **Frozen frame** | pHash identical to the previous run | A feed that is still publishing but no longer changing. |

The saturation threshold is **per product, not global**. Forecast fields such
as water depth encode a scalar with a thresholded colour ramp and legitimately
sit above 40% in normal operation; radar reflectivity does not. A single global
number would either miss real saturation on radar or cry wolf on every forecast
frame.

### And separately, at Layer 1

Product checks test the image more cheaply as part of the freshness check:
that it exists and is really a PNG, that it is not implausibly small for that
product, and that its bytes hash to a recorded value. That is a delivery check,
not a content check — Layer 1 asks *did we get a file*, Layer 4 asks *is the
picture sane*.

---

## 13. Why do image checks so often say "quiet" or "low coverage" instead of pass or fail?

Because the honest answer is frequently *"that detector cannot say anything
useful about this frame"*, and saying so beats guessing. Each of these means
the test ran and declined to draw a conclusion:

| Verdict | Means |
|---|---|
| `OK_SAME_FRAME` | Upstream has not published anything new since the last check, so there is nothing to compare against. |
| `QUIET_LOW_COV` | Coverage is below 5%. A radar watching a clear sky produces near-identical frames; that is calm weather, not a stuck feed. |
| `QUIET_SLOW` | This product updates more slowly than we check it, so repeats are expected. |
| `OK_LOW_COV` | Too few pixels for a saturation reading to mean anything. |
| `TOO_SPARSE` / `EMPTY` | Not enough active pixels for the detector to run at all. |

These exist because the first version did not have them, and it was badly
wrong in a way that is worth describing plainly.

The frozen-frame detector compared each run against the previous one without
first asking whether upstream had published anything in between. Our check
cadence is faster than the publication cadence, so much of the time we were
re-sampling **the same published image** and reporting it as frozen against
itself — the detector was measuring our own polling rate, not the feed.

The scale of it is visible in the record: **15,326** captures are marked
`OK_SAME_FRAME`, meaning upstream had published nothing new since the previous
check. Every one of those would previously have been a candidate for a false
FROZEN. Reprocessing the history with the source comparison in place took the
FROZEN count from **13,837 to 263** (August 2026); it stands at 265 today, the
difference being two genuine ones since.

The lesson generalises: a detector that cannot tell "nothing changed" from
"nothing new arrived" will confidently report a fault that does not exist.

!!! note
    A suppressed verdict is still recorded in full. Nothing is deleted — you can
    always see which detector declined and why.

---

## 14. Do you keep the images? Can I see what Sentinel actually saw?

Yes. Every frame a Layer 4 check evaluates is stored — currently about
**298,000 captures, 22 GB**. Clicking a cell in the timeline shows the frame
that produced *that* verdict, not a fresh fetch of whatever happens to be
current now. Evidence for a past call does not drift.

Storage is **content-addressed**: each file is named by the SHA-256 of its own
bytes, under `archive/<first two hex chars>/<full hash>.png`. Three
consequences follow:

| Property | Why it matters |
|---|---|
| Self-verifying | Re-hash a file and compare it to its own name. Silent corruption cannot hide, which is what let an 18 GB migration onto network storage be verified rather than trusted. |
| Automatic dedup | A forecast product idle for an hour, or a radar sitting on a steady clutter pattern, stores one copy however many times we fetch it. |
| Stable reference | A verdict points at exact bytes, so the evidence behind it is immutable. |

Two indexes sit over the files: one from hash to metadata (size, dimensions,
first and last seen), and one from the upstream `source` string to the hash.
The second is what lets the map replay a time window from our own copies
instead of asking radarca again — which is why scrubbing the map costs upstream
nothing.

---

## What Sentinel does not do

Worth being explicit, so the readings aren't over-read:

- It does not assess **scientific quality** of radar products. L4 checks catch
  gross image anomalies — saturated, frozen, empty, ring artifacts — not
  calibration error or subtle bias.
- It does not see **inside** radarca. Every conclusion is inferred from public
  outputs.
- It cannot distinguish "the radar stopped" from "the pipeline stopped
  publishing it" for a single radar. Correlation across radars (Q8) is what
  separates those, and it needs more than one radar to work.
- Its history is only as good as its thresholds, which is why they are
  re-tuned monthly and why past verdicts can be corrected (Q9).
