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
