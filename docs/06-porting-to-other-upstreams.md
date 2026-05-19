# Porting Sentinel to other upstreams

Sentinel is structurally generic HTTP monitoring — the alarm engine,
admin surface, mobile shell, and dispatch pipeline don't know
anything about radar. The radar opinions live in two places:
`backend/config.py` (the product catalog + radar inventory) and
`backend/checks/` (the layer-by-layer probe classes). This doc walks
through what you'd change to point Sentinel at a non-radarca system.

**Who this is for**: developers willing to read the existing check
modules and adapt them. If you just want a monitoring tool out of
the box, Sentinel is over-fit to radarca for that — pick something
more generic (Uptime Kuma, Statping, etc.).

**Companion docs**:

- [`13-extending-checks.md`](#) (roadmap) — how to add a new check
  class from scratch.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the full stack walkthrough.

---

## What's portable, what isn't

| Subsystem | Reusable as-is | Radar-specific |
|---|---|---|
| Scheduler + cadence runner | ✓ | — |
| Alarm engine (routes, policies, escalation, schedules) | ✓ | — |
| Suppression + cascade demote | ✓ (uses generic `depends_on`) | — |
| Admin UI (users, groups, alerts, thresholds, silences, audit) | ✓ | — |
| Mobile shell + push routing | ✓ | — |
| Threshold registry | ✓ | The default knob names are radar-flavored (`silent_fail_s`, `max_freshness_s`). Replace per check. |
| Email + Web Push dispatch | ✓ | — |
| L0 connectivity checks (origin, public page, TLS, root, Sentinel internet, Sentinel DNS) | mostly | The URL paths probed live in `config.py`; swap them. |
| L1 product checks | conceptually | Hard-coded to the radarca product JSON shape. Rewrite the probe + parser. |
| L2 radar reconciliation | radar-specific | Reuse the pattern (declared vs observed) if your upstream has the same shape; otherwise drop. |
| L3 overlay parity (Playwright) | upstream-specific | The DOM/JS surface it probes is radarca's. Adapt or drop. |
| L4 image quality | radar imagery only | Coverage / autocorrelation / extent are tuned to weather radar PNGs. Drop unless you have similar imagery. |

The dependency graph wires it all together; as long as the new
checks declare `depends_on` correctly, every cross-cutting feature
(cascade demote, suppression, mobile drilldowns) works without
changes.

---

## Step 1: inventory your upstream

Before touching code, write down:

- The **products** (or whatever your upstream's primary data
  artifacts are called). For radarca it's `comp_ref`, `qpe_15min`,
  etc. — twelve named feeds.
- The **per-product manifest** URL pattern. For radarca it's
  `/api/productDetail?file={product}/details.json`.
- The **per-product image** URL pattern. For radarca it's
  `/api/imageData?file={path}`.
- The **expected refresh cadence** of each product (how often
  fresh data lands).
- Any **status feed** the upstream provides (radarca exposes
  `/api/radar-status/`). Optional — without one, L2 reduces to
  pure observation.

For each, write a one-line URL example. That's what you'll wire
into `config.py`.

---

## Step 2: edit `backend/config.py`

The two most important constants:

```python
SETTINGS.base = "https://your-upstream.example.com"   # via env var

PRODUCTS = {
    "your_product_id": {
        "details":            "your_product/details.json",
        "image_subdir":       "your_product/",
        "max_freshness_s":    120,        # < 0 if FUTURE-dated (forecast)
        "min_png_bytes":      5_000,
        "expected_steps":     30,         # how many steps in a healthy manifest
        "cadence_s":          60,         # scheduler interval for this check
    },
    # ...
}
```

The `image_path()` helper builds the upstream image URL from the
`image_subdir` + scan filename. If your upstream uses a different
naming convention, also edit `image_path()`.

If your upstream has a status feed, also update
`backend/checks/layer2_radar.py` to parse it.

---

## Step 3: edit `backend/checks/`

Decide which check classes apply to your upstream:

### L0 — always relevant

`backend/checks/layer0_website.py` probes the origin's public
surface. The four checks (origin alive, public page, root 404, TLS)
need the URL paths edited:

- `OriginAliveCheck` — probes `{SETTINGS.base}/api/radar-status/`.
  Change to whatever your upstream's "I'm alive" endpoint is.
- `WebsitePublicCheck` — probes `{SETTINGS.base}/public` and looks
  for SSR markers. Change the path + the marker strings.
- `RootNotFoundCheck` — looks for Next.js not-found markers at
  `{SETTINGS.base}/`. If your upstream returns 404 instead of
  200-with-markers, change the assertion.
- `TLSCertCheck` — extracts hostname from `SETTINGS.base`. No
  change needed unless you want a different days-before-expiry
  threshold.

`backend/checks/layer0_network.py` (Sentinel Internet + Sentinel
DNS) is upstream-agnostic. Leave it alone.

### L1 — adapt per-product

`backend/checks/layer1_product.py` is the bulk of the logic. It:

1. Fetches the per-product manifest (`/api/productDetail`).
2. Parses the steps list, finds the latest step's timestamp.
3. Computes age, compares to `max_freshness_s`.
4. Fetches the latest image, validates size + MIME type.
5. Records sub-check verdicts (`A_api`, `B_schema`, `C_freshness`,
   etc.).

Rewrite the URL builders + JSON parser to match your upstream's
shape. The sub-check structure (A/B/C/D/...) is a reusable
convention — keep it, just replace the assertions.

### L2 — radar-specific, may not apply

`backend/checks/layer2_radar.py` reconciles a "declared UP/DOWN"
status feed against observed imagery cadence. If your upstream
doesn't have a parallel status feed, drop this whole layer or
reduce it to a freshness check on the radar's primary product.

### L3 — Playwright-driven page test

`backend/checks/layer3_overlay.py` opens the upstream's overlay
page in Chromium and asserts the JS pipeline rendered. Almost
certainly upstream-specific; either rewrite the assertions or drop
the check.

### L4 — image quality

`backend/checks/layer4_image.py` does coverage / autocorrelation /
content-extent on PNG snapshots. Reusable for any visual-data
upstream where coverage matters; the thresholds in `config.py` will
need re-calibration for your imagery's characteristics.

---

## Step 4: rebuild the dependency tree

Each check class declares `depends_on: list[str]`. The cascade-demote
logic walks this. For a clean tree:

- Root: `layer0.net.internet`, `layer0.net.dns` (no deps).
- L0 origin/website/root/tls depend on the network checks.
- L1 products depend on `layer0.origin.alive`.
- L2 radars depend on `layer0.origin.alive`.
- L3 overlays depend on `layer0.website.public` + the relevant L1
  products.
- L4 image checks depend on their L2/L1 parents.

The shape should match yours: bottom-up, transitively reaching
whichever L0 check represents "the foundation."

---

## Step 5: update the descriptor map

`backend/stages.py:stage_descriptor()` maps `L0`/`L1`/... to the
user-facing names ("Connectivity", "Product Freshness", etc.). If
your stages don't fit the radar-flavored names, rename:

```python
# backend/stages.py
_DESCRIPTORS = {
    "L0":      "Connectivity",        # OK for any upstream
    "L1":      "Data Freshness",      # was "Product Freshness"
    # ... etc.
}
```

Mirror the change in `frontend/src/lib/format.ts:stageLabel()`.

---

## Step 6: update product / target labels

`frontend/src/lib/format.ts` has multiple lookup tables that
translate machine ids to user-facing names:

- `PRODUCT_LABELS` — per-product display names.
- `L0_TARGET_LABELS` — L0 check targets.
- `VECTOR_TARGET_LABELS`, `STREAM_TARGET_LABELS` — radar-specific
  artifact types.
- `productCategory()` — groups products into categories on the
  home page.

Edit these to match your upstream's data taxonomy. The
`prettyCheckLabel()` function dispatches off `check_id.startswith()`
— if you keep the `layerN.kind.id` naming convention, no structural
change needed.

`backend/check_labels.py` mirrors the same mappings for the backend
side (used by cascade-demote summaries, alarm messages). Keep both
files in sync.

---

## Step 7: re-baseline thresholds

The default thresholds in `config.py` are tuned to radarca's
characteristics. For a different upstream:

1. Run Sentinel against your upstream with permissive defaults
   (e.g. `max_freshness_s = 3600`, `silent_fail_s = 1800`).
2. Watch a week of operation; note the distribution of observed
   ages, scan counts, image sizes.
3. Tighten thresholds to the 95th-percentile of healthy operation +
   a small margin.
4. The [retroactive
   reprocess](MAINTENANCE.md#reprocess-historical-data) job can
   re-evaluate the week-of-operation data under the new thresholds
   — useful for sanity-checking before going live.

---

## What you don't need to change

- The alarm engine, routing rules, recipient model, escalation
  policies.
- The mobile shell.
- The admin surface (the threshold editor adapts automatically to
  whatever knobs your checks read).
- The dispatch pipeline (email, Web Push, webhooks).
- The audit log.
- The auth subsystem.
- The CI workflows.
- The database schema (it's generic over `check_id` and stage).

If you're tempted to fork these, that's a sign you've broken the
generic/specific split somewhere. Most of the time, the right
answer is to refactor the radar-specific bits into a smaller
shape rather than fork the cross-cutting subsystems.

---

## Where to go from here

- **[`13-extending-checks.md`](#)** — the structural guide to
  adding a check class from scratch. Read once you know your
  upstream's data shapes.
- **[`16-alarm-engine.md`](#)** — under-the-hood mechanics of the
  alarm pipeline. Read if you want to add a custom sink (e.g.
  Slack, PagerDuty) on top of your ported deployment.
