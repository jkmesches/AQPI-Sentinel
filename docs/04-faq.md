# FAQ for the lab team

Written for people who use Sentinel's readings rather than its code: what it
watches, where the numbers come from, and how it decides something is wrong.

If you want the short version: Sentinel independently re-derives the health of
`radarca.engr.colostate.edu` from that site's own public APIs, every couple of
minutes, and keeps the evidence.

---

## 1. What is Sentinel actually watching?

Two things, by two independent routes, and the difference between them is the
most useful signal Sentinel produces.

The **display tier** is the public radarca service. Sentinel reads exactly
what a browser visiting radarca would read — no privileged API, no feed from
the radar processors — and reasons about it.

The **backend tier** reads the published files directly, off the backend
shares (K2, and Trinity for CBAND), over a read-only mount. radarca is not
involved in any of it.

Every stage belongs to one tier or the other: `L*` stages ask radarca what it
believes, `LB*` stages read the files. Where the two disagree, **the
disagreement is the finding** — usually that the data exists and something in
the display tier is not showing it. See Q4 for the stages, Q8 for how one
upstream event is told apart from five radar faults.

Neither tier sees inside the processing itself. When Sentinel says "XSWR is
not reporting," the precise claim is about what reached a named place —
radarca's public API, or a named directory on disk — not about the radar
hardware. A deployment with no backend mount runs the display tier alone, and
the interface says so rather than implying coverage it does not have.

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

Those five are the display tier. The **backend tier** reads files rather than
APIs, on a read-only mount:

| Source | What we take from it |
|---|---|
| The product trees on K2 | Whether each product is really being produced, independent of what radarca shows |
| The DROPS dated trees | Whether each radar's scans are landing on disk right now |
| `PRODUCTS/Radar_processed/recentfiles` | Whether each radar is staged where the composite will actually read it |
| The composite's own output | Which radars contributed to each composite, as opposed to which were expected to |

A deployment without those mounts simply runs without the `LB` stages.

## 3. How often does it check, and does that load your servers?

Cadences run from 30 s (is the internet up) to 1 h (slow forecast products);
most radar and product checks run every **120 s**. Total outbound traffic is
roughly **30 requests/minute**.

We actively work to keep that low. All six radar checks used to fetch
`/api/radar-status/` independently — 4,320 calls/day where 720 suffice — and
now share one memoized fetch per cycle. If our traffic is ever a problem for
you, tell us; the cadences are configuration, not architecture.

That figure covers the display tier only. The backend-tier checks read mounted
files and make **no upstream requests at all**, so everything added to the
`LB` stages — product production, radar arrival, composite staging and
participation — cost radarca exactly nothing.

## 4. What do the stages mean?

Each check belongs to a stage, ordered from "is anything reachable" to "does
the imagery look sane":

| Stage | Name | Question it answers |
|---|---|---|
| L0 | Connectivity | Is the internet up, DNS resolving, radarca reachable? |
| L1 | Product Freshness | Is each product publishing recent, correctly-sized data, according to radarca? |
| LB1 | Backend Products | Is each product actually being produced — read from K2 directly, not via radarca? |
| L2 | Radar Scans | Is each radar actually emitting scans, according to radarca? |
| LB2 | Backend Radar Arrival | Is each radar's data landing on disk right now — read from the backend trees directly? |
| LB3 | Backend Processing | Is each radar staged where the composite reads, is it reaching the composite, and is the QPE producer still running? |
| L3 | Map Overlays | Do overlays render in a real browser? |
| L4 | Image Quality | Does the imagery itself look plausible? |

The stages are also a dependency chain. If L0 says the origin is unreachable,
downstream failures are collateral, and Sentinel marks them as such instead of
painting forty red cells for one root cause.

The `LB` stages sit deliberately outside that chain. They read the published
files straight off the backend shares, so radarca being down tells you nothing
about whether a product exists — and that independence is the point. Where an
`LB` stage and its radarca counterpart disagree, the `LB` answer is the one
describing reality, and the disagreement itself is the useful signal: the data
is being produced but something in the display tier is not showing it.

The home page puts that comparison on one row. Each radar and product carries
two readings, tagged **K2** (the backend tree — Trinity for CBAND) and **RC**
(radarca), plotted on the same fixed scale against the same threshold. When
one trace falls and the other does not, that is the disagreement, visible
without reading anything. A deployment with no backend mount shows a single
column and no tags.

<!-- The answers below are duplicated in prose in frontend/src/lib/faq.ts,
     rendered at /faq in a running Sentinel. The WORDING is deliberately
     allowed to differ (see validation_tests/test_faq_parity.py), but a
     factual correction has to be applied to BOTH copies -- that test
     compares only the questions and cannot catch a half-applied one.
     On 2026-10-12 a retracted claim about XSCW had to be removed from
     both, and the second copy was nearly missed. -->

`LB3` watches the gap between the other two. A radar can be arriving on disk
(`LB2` green) and a product can be publishing on time (`LB1` green) while that
product is quietly computed from fewer radars than it claims. On 2026-10-05 a
radar aged out of the AQPI composite and nothing anywhere said so, because
nothing was looking between arrival and publication.

That gap turned out to have a gap of its own. The composite does not read the
tree `LB2` watches; it reads a staging directory that something else copies
into. For eighteen days after a K2 reboot in September 2026, the three NEXRAD
staging directories held **zero files** while the composite went on publishing
— it ran on six radars instead of nine, and the only place that was visible was
a directory nothing in Sentinel looked at. `layer3.input.<radar>` watches it
now, for all nine composite inputs including the three NEXRAD sites, which
have no other coverage anywhere in Sentinel.

`LB3` also watches the QPE producer, and that one is deliberately non-paging:
nothing in the live product chain reads the tree it writes, so a stall there
is worth seeing and not worth waking anyone for.

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
| **skip** | Deliberately not evaluated | Suppressed by an upstream failure, or nothing to assess |

`fail` is a statement about radarca. `error` is a statement about *our
visibility*. Treat a screen full of `error` as "Sentinel is flying blind,"
not "everything is down."

`skip` comes in two flavours that look identical on the timeline. Most are
**cascade skips**: when one upstream thing breaks, Sentinel stops reporting
its own opinion about everything downstream, so a single origin fault doesn't
paint thirty cells red. That is a refusal to guess, *not* a clean bill of
health — a skip never resolves an open alarm, and the previous verdict
stands until a real check result replaces it. The other flavour is an
**intrinsic skip**, where the check ran and genuinely had nothing to assess;
that one does count as "nothing wrong". The drilldown tells you which.

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

Two rules were learned expensively, and they are why some sub-checks show no
verdict at all rather than a green one:

- **A band is fitted per class, never per radar.** A limit derived from one
  radar's own history goes blind exactly where that history is already bad:
  fit it to XSCW's 18-hour stalls and 18 hours becomes "normal for XSCW".
  The per-radar freshness numbers above are the exception that proves it —
  they are measured against publication lag, which genuinely is per radar.
- **No band is better than a guessed one.** Where there is not yet enough
  measurement to set a limit, the sub-check is omitted rather than set to
  pass. "We did not judge this" and "we judged it healthy" are different
  claims, and a series that records the second for a period it never
  assessed is worse than one with an honest gap.

## 8. Five radars went red at once. Is that five outages?

Almost certainly one.

Radars at Santa Cruz, Sonoma, East Bay, Coyote Valley and Sawyer Ridge do not
fail in the same second. Over the 14 days to 2026-08-25, **80.2%** of all
`GHOST_UP` readings happened while four or five radars were silent
simultaneously; only **2.8%** were isolated to one radar. The clearest case:
four sites — XSWR, XSCR, XSCV and XSCW — entered `GHOST_UP` at
`2026-08-13 12:29:52` and left it at `2026-08-16 19:29:54`. Sub-second
alignment, 79.0 hours apart.

Sentinel now runs a fleet-correlation check that recognizes this shape and
raises **one** systemic alarm instead of five, while still showing each
radar's individual verdict. It fails when at least four of the five X-band
radars are unhealthy at once; `CBAND` is deliberately excluded, being a
different band at a different site. An isolated single-radar failure is
unaffected and still pages normally.

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
79-hour outage still recorded in full across all four sites it covered.

## 10. Who gets alerted, and how do I stop being paged for something I know about?

Alarms route by **severity tier**:

- **Action required** — broken (`fail`/`error`). Auto-escalates after 30 min.
- **Attention required** — degraded (`warn`). Visible, doesn't page.
- **Informational** — healthy or out of scope.

Recipients, escalation steps and on-call schedules live under `/admin/alerts`
and `/admin/groups`, including recurring quiet hours and per-device routing on
mobile.

One switch sits above all of that: alarms can be enabled or disabled **per
observation tier**. A site whose backend mounts are new and still being tuned
can record every `LB` reading while paging on none of them. The gate is on
dispatch only — a gated alarm still opens, still carries its severity, still
appears on the timeline and in the daily report, and records where the message
*would* have gone, so "what did Sentinel not tell me, and when" stays
answerable. It never gates evaluation: a tier that stopped recording would
leave a hole in the history that nothing could see later.

**To stop being paged for something you already know about, acknowledge it.**
An ack stops every channel for that alarm — email, console, webhook, and any
repeat — and it silences it for the whole team, not just you, because whoever
acks is taking ownership of it. Un-acking resumes paging.

For planned work, use a **silence** (`/admin/silences`) rather than muting a
check permanently — silenced alarms still appear on the dashboard and in the
record, they just stop paging. Prefer a silence over an ack when you know the
window in advance, because a silence covers the check regardless of how the
underlying alarm comes and goes.

If you are seeing noise you believe is wrong, that is worth reporting rather
than silencing: the three largest sources of false alarms found so far were a
stale threshold, a color that made "couldn't measure" look identical to
"broken", and — until v0.2.1 — a bug that let a permanently-down radar
re-announce itself every few hours because an upstream blip had been mistaken
for a recovery.

---

## 11. What happens to an image between radarca publishing it and a verdict appearing?

Seven steps, every cycle, for each X-band radar and each mosaic product.

| Step | What happens |
|---|---|
| 1. Resolve | Ask radarca which scan is *latest* — `xbandRadarImages` for a radar, `productDetail` for a mosaic product. |
| 2. Fetch | Pull the PNG bytes. This is the only upstream request the image checks make. |
| 3. Archive | Hash the bytes with SHA-256 and store them content-addressed. Identical bytes are stored once. |
| 4. Tier 1 | Measure the frame — coverage, intensity, structure, perceptual hash. No judgment yet. |
| 5. Tier 2 | Run the pathology detectors against those pixels. |
| 6. Compare | Check the perceptual hash against the previous run to catch a stuck feed. |
| 7. Verdict | Take the worst sub-verdict. Anything not on the OK list becomes a `warn`. |

The split between steps 4 and 5 is deliberate and load-bearing. **Tier 1
records what the frame is** and is threshold-free. **Tier 2 decides whether
that is a problem**, and every tunable number lives there.

The payoff is that when a threshold turns out to be wrong — which has happened
more than once — the judgment can be re-run over stored measurements without
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
| Horizontal autocorrelation | Whether neighboring pixels agree. Structured weather correlates; noise does not. |
| Perceptual hash | 16×16 pHash. Two frames with the same hash are visually identical. |
| SHA-256 + byte size | Exact identity of the file, for the archive and for dedup. |

### Tier 2 — judgment

Four detectors, each with a tunable threshold:

| Detector | Trips when | What it catches |
|---|---|---|
| **Saturation** | One quantized color holds >40% of active pixels | A frame collapsed to a single value — a stuck color map or an encoder fault. |
| **Speckle** | >35% of active pixels have no active 4-neighbor | Noise dressed as data: isolated pixels with no structure. |
| **Range ring** | Peak ring deviation >0.80× the median across 50 polar bins | Concentric artifacts on an X-band disc — a calibration or clutter-filter signature. X-band only: mosaics have no radar-centerd geometry. |
| **Frozen frame** | pHash identical to the previous run | A feed that is still publishing but no longer changing. |

The saturation threshold is **per product, not global**. Forecast fields such
as water depth encode a scalar with a thresholded color ramp and legitimately
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
FROZEN count from **13,837 to 263** (August 2026); it stood at 265 in October
2026, the difference being two genuine ones since. These are counters over a
growing archive — treat them as the shape of the problem, not as today's
totals.

The lesson generalizes: a detector that cannot tell "nothing changed" from
"nothing new arrived" will confidently report a fault that does not exist.

!!! note
    A suppressed verdict is still recorded in full. Nothing is deleted — you can
    always see which detector declined and why.

---

## 14. Do you keep the images? Can I see what Sentinel actually saw?

Yes. Every frame a Layer 4 check evaluates is stored — roughly **298,000
captures and 22 GB** when this page was last revised, and growing with every
cycle. Clicking a cell in the timeline shows the frame
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

## 15. What does the daily report tell me that the dashboard does not?

The dashboard answers "what is true now". The daily report answers "what
happened over a window, and how much of it did we actually see" — which are
different questions, and the second one is the one that gets forgotten.

Each radar and product gets a row carrying availability, how long it was
offline or degraded, how many distinct outages there were, how many were
short blips, and **how many of its failures were silent** — broken while the
status feed said otherwise. Above them sits the share of the window that was
*conclusive*: minutes where Sentinel could actually form an opinion, as
opposed to minutes it spent blind. A report that says 93% conclusive is
telling you the remaining 7% is unknown, not healthy.

Because every subject is watched by two tiers (Q1), each row has to be
summarized from one of them, and that is configurable per subject under
`/admin/digest`:

| Source | Summarizes from |
|---|---|
| `frontend` | the radarca-derived `L*` checks |
| `backend` | the file-reading `LB*` checks |
| `combined` | both, binned to the minute — a minute counts as bad if either tier says so |

The default follows the deployment: `frontend` where radarca is configured,
`backend` where it is not. Subjects can also be hidden from the report
entirely, for a radar that is known to be offline for a season and would
otherwise dominate every morning's email.

---

## What Sentinel does not do

Worth being explicit, so the readings aren't over-read:

- It does not assess **scientific quality** of radar products. L4 checks catch
  gross image anomalies — saturated, frozen, empty, ring artifacts — not
  calibration error or subtle bias.
- It does not see **inside the processing**. The backend tier reads the files
  the processing writes and the display tier reads what radarca publishes;
  neither can see the code in between, so "the composite is wrong" is not a
  question Sentinel can answer — only "the composite was built without XSCR".
- It watches the three **NEXRAD** sites only where they enter the AQPI
  pipeline — staged for the composite, and contributing to it. There is no
  scan-level, image-level or declared-status check for them anywhere in
  Sentinel, because the public endpoints that would support one are CSU's
  X-band ones.
- It cannot distinguish "the radar stopped" from "the pipeline stopped
  publishing it" for a single radar. Correlation across radars (Q8) is what
  separates those, and it needs more than one radar to work.
- Its history is only as good as its thresholds, which is why they are
  re-tuned monthly and why past verdicts can be corrected (Q9). Where a
  threshold has not been measured yet, the sub-check is left out rather than
  guessed (Q7) — an absent verdict is a real state, not a display bug.
