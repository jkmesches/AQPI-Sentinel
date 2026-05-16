# Monitoring plan for `radarca.engr.colostate.edu`

_Companion to `radarca-public-characterization.md`. Drafted 2026-05-15._

Five layers, deepest first. The design principle is **hierarchical suppression**: if a higher layer fails, suppress alarms on the layers below it for the same target, so one outage doesn't fire 100 alerts.

```
Layer 0 — Website / edge alive
Layer 1 — Per-endpoint API health + freshness
Layer 2 — Per-radar status (declared + observed)
Layer 3 — Image-embedded timestamp ground truth
Layer 4 — Radar signal-pathology detection
```

---

## 1. Website status (Layer 0)

**Goal:** detect when the Next.js/nginx layer itself is down, *separately* from data-side issues.

**Checks (cadence: every 30–60 s):**
- `GET /public` → require:
  - HTTP 200, `Content-Type: text/html`
  - body ≥ 50 KB (shell is ~65 KB; very small ≈ wrong response)
  - body must contain literal strings `"Radar Data"`, `"Atmospheric Forecast"`, `"CoSMoS Data"`, `"National Water Model"` (these are baked into the SSR HTML; their absence ≈ wrong app served)
- `GET /` → must contain Next.js not-found markers (`"404: This page could not be found"`, `next-error-h1`). **Note:** Next.js serves the not-found page with **HTTP 200**, not 404 — assert on body markers, not status code. A 500 or hang means the Next process is wedged behind a healthy nginx.
- TLS cert expiry > 30 days (`openssl s_client` parse, weekly).
- DNS A-record resolves.
- **Origin-alive canary:** `/public` is served from Next.js full-route cache (`s-maxage=31536000`, always `x-nextjs-cache: HIT`) and will keep returning 200 even if the Next.js or Django origin dies. Cache cannot be busted from the client (tested: query-string, `Cache-Control: no-cache`, `Pragma: no-cache`, HEAD vs GET — all still `HIT`). Use any `/api/*` endpoint as the actual origin canary; those are not edge-cached. `GET /api/radar-status/` returning 200 + JSON proves the Django app is live.

**Alarm trigger:** 3 consecutive failures over 90 s.

**Suppression effect:** when this layer fails, suppress all layer 1–4 alarms (don't page about stale data if we can't even reach the site).

**Disambiguation:**
- `/public` OK + `/api/radar-status/` 5xx → "API down only" (alarm but don't suppress)
- both down → "site down" (suppress downstream)

**What this does NOT detect:** any data correctness issue. A perfectly healthy frontend can serve a broken data plane.

---

## 2. Per-endpoint / per-product health (Layer 1)

**Goal:** for every endpoint enumerated in the characterization doc, track {availability, schema validity, freshness, value sanity}.

**Config-driven (one row per product slot):**

```yaml
# example
comp_ref:
  details_path: composite_ref_max/details.json
  image_dir:    composite_ref_max/images/
  cadence_s:    120          # 2 min scan cadence
  expected_steps: 31         # ≈ last hour
  max_freshness_s: 360       # alarm if newest step > 6 min old
  min_png_bytes:  50000      # below = blank/transparent
qpe_15min:
  details_path: rain15min/details_in.json
  ...
forecast_total_precip:
  details_path: total_precip/details_in.json
  cadence_s:    3600         # 1 hour
  expected_steps: 72         # ±2
  max_freshness_s: 7200      # forecast can lag 2 h
  ...
```

**Per poll, per product:**

| Check | How | Alarm |
|---|---|---|
| A. API up | `GET /api/productDetail?file=<details_path>` 200, JSON | "Product X API failing" |
| B. Schema | `steps[]` non-empty; each has `imageName,timestamp` | "Product X malformed" |
| C. Freshness | `now − max(steps.timestamp)` < `max_freshness_s` | "Product X stale (N min old)" |
| D. Cadence | median Δtimestamp ≈ `cadence_s` ±10% | "Product X cadence drift" |
| E. Step count | within ±2 of `expected_steps` | "Product X truncated/expanded" |
| F. Image exists | `HEAD /api/imageData?file=<latest>` 200 image/png | "Product X latest image 404" |
| G. Image size | bytes ≥ `min_png_bytes` | "Product X image undersized" |
| H. Image not frozen | SHA-256 over the full PNG; if same hash for ≥ 3 consecutive *new* steps, broken | "Product X frames frozen" |

**Vector/static layers (weekly):** HEAD `/geojson/flowlines.geojson`, `/geojson/watersheds.geojson`, `/data/stream_data.csv`. Alarm if `Last-Modified` > 90 days old (currently 7-Jan-2026 — acceptable, but watch for regression vs progression).

**Stream APIs (every 5 min):** pick 5 canary COMIDs from each status class (B and R), call `/api/get_stream_data/<comid>/<YYYYMMDD_HH>` and `/api/get_observed_stream_data/<comid>/<YYYYMMDD>`. **Note:** the API's own error message says `"Invalid timestamp format. Use YYYYMMDD_HHMM"` but the validator actually accepts only `YYYYMMDD_HH` (2-digit hour, no minutes — `_HHMM` returns 400). The error message is misleading. The JS call-site uses `_HH` and works. Worth flagging to the CSU team.

**Cadence:** floor at `cadence_s / 2`. So 1 min for radar products, 15–30 min for forecasts/water, 5 min for stream canaries.

**Day-1 expected catches** (from current state): atmospheric forecasts (`tp`, `total_precip_actual`, `pr`, `temp`) trip C+F+G simultaneously — staleness + 404 images. That's the existing production failure already detectable.

---

## 3. Per-radar status (Layer 2)

**Goal:** monitor each of {XSCV, XSCW, XSCR, XSWR, XEBY/EBAY, CBAND} independently. The dashboard's status API can lie or lag, so cross-check against actual data flow.

**Two sources per radar (cadence: every 2 min):**

| Source | Endpoint | Tells you |
|---|---|---|
| **A. Declared** | `GET /api/radar-status/` | What the dashboard's status chip says |
| **B. Observed** | `GET /api/xbandRadarImages/?radarFolder=<R>&productPrefix=CorrReflectivity` (1-h window) | Whether scans are actually arriving |

**Reconciliation matrix:**

| Declared | Observed (CorrReflectivity in last hour) | Verdict |
|---|---|---|
| UP | non-empty, fresh (<10 min) | healthy |
| UP | empty for ≥10 min | **silent failure** ("ghost UP") — status API lying. Alarm separately, don't trust the chip. |
| DOWN | empty | confirmed outage |
| DOWN | non-empty/fresh | stale status API — alarm "status flag stuck DOWN" |

**Moment-level granularity (every 5–10 min):** for each radar, probe all productPrefixes — `CorrReflectivity`, `CorrDifferentialReflectivity`, `RhoHV`, `FilteredPhiDP`, `Velocity`. If `CorrReflectivity` flows but the others don't → **partial radar failure** (e.g., dual-pol channel dead but transmitter fine).

**C-band notes:** the JS bundle doesn't expose the C-band's productPrefix vocabulary explicitly. Discovery probes to try: `Reflectivity`, `CorrReflectivity`, `dBZ_Composite`. Re-scan the JS bundle weekly for new constants in case the schema evolves.

**Mosaic-membership check:** the composite product's `info` field lists 7 radars (`KBBX, KDAX, KMUX + XSCW, XEBY, XSCV, XSCR`) — note **XSWR is not in this list**, suggesting it's a newer add or in a different mosaic. Verify with Chandra's team. When `comp_ref` is healthy but a member radar isn't, that radar is "dropped from mosaic" → alarm.

**Suppression:** if Layer 0 down → suppress. If `/api/radar-status/` itself unreachable → only fire the source-A alarm; keep source-B per-radar checks running.

---

## 4. Image-embedded timestamp check (Layer 3)

**Goal:** the pipeline draws a timestamp (typically bottom-left) onto every PNG. That's authoritative ground truth — the API's `productDetail` metadata can lie (e.g., the pipeline rebuilds `details.json` while serving stale images, or symlinks new names to old content).

**Setup (one-time per product):**

1. Fetch a fresh sample image for each product.
2. Locate the timestamp text by inspection. Record bbox + format:
   ```yaml
   comp_ref:
     ts_bbox: [10, 1050, 280, 35]   # x,y,w,h in pixels (843×1108)
     ts_format: "%Y-%m-%d %H:%M UTC"
     ts_location: bottom-left
   forecast_temp:
     ts_bbox: [...]
     ts_format: "valid %Y-%m-%d %H:%M / init %Y-%m-%d %H:%M"
     # two timestamps: valid time + model init time
   ```
3. Forecasts may have two stamps (valid time + initialization time). Capture both — the gap between them is the forecast lead, which directly reveals model staleness independently of any API.

**Per poll (cheap first, then OCR):**

- **(a) Bbox-hash frozen-detector — no OCR needed.** Crop the bbox, hash it. If the hash is identical for ≥ 5 consecutive scans, the timestamp text isn't changing → frozen at source even if filenames increment. This alone catches "renaming-without-regenerating" failure modes.

- **(b) OCR comparison — when (a) is fine but you want ground-truth times.**
  - Crop bbox → upscale 2–3× → binarize (Otsu) → tesseract `--psm 7` (single line). PaddleOCR if tesseract is unreliable on this font.
  - Parse with `ts_format` → `t_embed`.
  - Compare to API: `Δ = t_embed − steps[-1].timestamp`. |Δ| > 1 min → "label mismatch" (alarm: pipeline misindexing).
  - Compare to wall clock: same threshold as Layer 1 freshness.
  - On OCR parse failure for 3+ consecutive polls → alarm "image format / overlay changed".

**Implementation order:** ship (a) immediately; calibrate and add (b) one product at a time.

---

## 5. Radar signal-pathology detection (Layer 4)

The hard part — distinguishing the malfunction artifact you described ("marked artifacting unrelated to weather or common scatter") from real weather + benign clutter. Plan in capability tiers; deploy progressively, each tier shipping standalone value.

### Tier 0 — Build the labeled corpus (start day 1, cost ~0)

Archive every reflectivity scan per radar to disk. At ~130 KB × 6 radars × 720 scans/day ≈ **560 MB/day → ~200 GB/yr** — comfortable on the home cluster.

Maintain a labeled subset:
- `healthy_clear`, `healthy_weather`
- `benign_clutter_ground`, `benign_clutter_sea`, `benign_external_cband`, `benign_bio_bloom`
- `broken_pathology_*` — **seed this by asking Chandra's team / yourself for 5–10 archived examples of the specific artifact**. Without exemplars this tier stalls.

Every other tier benefits from this corpus; Tier 5 requires it.

### Tier 1 — Coarse statistical anomaly detection (no domain knowledge)

Per scan, per radar, compute:
- `coverage` — % pixels above the no-return background
- `mean_dBZ`, `std_dBZ` over valid pixels (use `colormap_ref.png` as the inverse lookup)
- `histogram` over the 16 dBZ color bins
- `spatial_autocorrelation` (Moran's I over a 5×5 window) — real weather is spatially correlated; broken-radar noise often isn't

Maintain a **7-day rolling baseline** per radar per metric (separately for day/night to control for diurnal effects). Alarm when any metric breaches ±3σ of its baseline.

Catches: dramatic noise floods, sudden coverage collapse, saturation events.

### Tier 2 — Heuristic pathology signatures

Specific detectors for known failure modes (numpy + OpenCV, ~ms each):
- **Range-ring saturation** — reproject image to polar (use `radarCoordinates[X].{lat,lon,range}` as origin), plot mean dBZ vs range. Peak at fixed range bands = transmitter/receiver ringing fault.
- **Frozen frame** — pHash a non-overlay region; identical pHash on N consecutive new scans = transmitter off (but pipeline still publishing).
- **All-extreme** — fraction of pixels at min or max bin > 30% = saturation.
- **Speckle / uncorrelated noise** — high local variance with low neighbor correlation.
- **Impossible sectorial pattern** — split the disc into 16 azimuth wedges; if one wedge's stats diverge dramatically without a known geographic cause (mountain, coast at that azimuth), flag.

### Tier 3 — Cross-radar consistency

X-bands + NEXRADs overlap. For each pair (A, B) with overlapping coverage, compute mean(|dBZ_A − dBZ_B|) over the overlap. Sudden divergence (one radar shows storms where the others show clear) → suspicion on the outlier. Pair this with the HRRR forecast for a "is precipitation plausible right now" sanity layer.

Implementation needs an overlap map — derive it once from `radarCoordinates` + ranges.

### Tier 4 — Dual-pol moment consistency

For each X-band scan, fetch Z, Zdr, ρhv, ΦDP for the same time and align spatially:
- Real meteorological scatter: ρhv > 0.95, Zdr in ~[−2, +6] dB, Z↔Zdr coupled.
- Non-met (clutter/interference): ρhv low (< 0.7).
- Radar fault signatures (vs benign clutter): e.g., Zdr pinned to one value across the whole disc → one polarization channel dead; ρhv pattern uncorrelated with any physical scatter → electronics fault.

Rules get domain-heavy here. Collaborate with Chandra's group on thresholds.

### Tier 5 — Learned classifier (depends on Tier 0)

Once Tier 0 has a labeled set (target: ≥ ~100 exemplars per class):
- Embed each scan with a pretrained image encoder (DINOv2 ViT-S/B, or CLIP) — no need to train from scratch.
- Train a small classifier (logistic regression or shallow MLP) on the embeddings.
- At inference: embed → predict probabilities → threshold → alarm.

This is the only realistic path to detecting the specific artifact you described without exhaustively enumerating its appearance. Anomaly score over time becomes its own monitored signal.

### Deploy order

1. Tier 0 archive — immediate, just disk + a cron pull.
2. Tier 1 coarse anomaly — ~1 day's work, catches dramatic failures.
3. Tier 2 heuristics — moderate, hand-tuned per failure mode.
4. Tier 3 cross-radar — needs overlap map.
5. Tier 4 dual-pol — pair with Chandra's team for thresholds.
6. Tier 5 classifier — after corpus reaches threshold size.

---

## Global architecture & decisions

- **Pollers** (Python or Go) — one process per layer, separate cadences. Independent failure domains.
- **State store** — SQLite for metrics/status history (or Prometheus + Grafana if you want the dashboard story).
- **Image archive** — per-radar PNG store, SHA-256 + bbox-hash indexed.
- **Alarm router** — implements the hierarchical suppression. Without it, a single edge outage fires hundreds of alerts.
- **Optional web UI** — a status grid (site / per-endpoint / per-radar / artifact-score-per-radar) over the SQLite. Cheap to build, very valuable for triage.

**Decisions needed before building:**
1. **Polling cadence floor** — be respectful: never poll a product faster than its native cadence (2 min for radar, 1 h for forecasts).
2. **Where in the cluster** to host this and how to expose alerts (email, Slack, custom).
3. **Storage budget** for the image archive (~200 GB/yr at full retention; can downsample old data).
4. **Coordinate with the CSU team** before deploying — especially before Tier 5 training and any high-rate polling. They may also be able to supply ground-truth labels and known-bad exemplars.
5. **Seed examples** of the specific "broken-radar" artifact from Chandra / yourself — Tier 5 can't start without 5–10 to begin labeling against.
