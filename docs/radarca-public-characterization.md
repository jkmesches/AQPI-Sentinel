# `radarca.engr.colostate.edu/public` — full characterization

_Scan performed 2026-05-15 ~23:58 UTC._

## 1. Stack & hosting

| Layer | Detail |
|---|---|
| Edge | **nginx** (TLS, gzip/`Vary: Accept-Encoding`) |
| App | **Next.js 14 App Router** (`X-Powered-By: Next.js`, build id `N508H0j8gMuenLti7CDQM`) |
| Backend (API) | **Django** (the `/api/*` 404 page is the Django default; `Allow: GET, HEAD, OPTIONS`, DRF-style JSON errors) |
| Cache | `x-nextjs-cache: HIT` on the HTML shell; `Cache-Control: s-maxage=31536000, stale-while-revalidate`. APIs are `cache-control: no-store`. |
| Routes that exist | `/public` (200, the dashboard), `/login`, `/ui/public/stream-graph?comid=…`. Root `/`, `/robots.txt`, `/sitemap.xml` all **404**. |
| Security headers | `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` (on `/api`), `Referrer-Policy: same-origin`, `Cross-Origin-Opener-Policy: same-origin`. No CORS header set → API is **same-origin-only**. `<meta robots="noindex">` on every page. |

## 2. Response to curl / Playwright / scrapers

- `curl https://radarca.engr.colostate.edu/public` returns **the full HTML shell** (≈ 65 KB, label text included — "Radar Data", "Total Precip…", "Atmospheric Forecast", "CoSMoS Data", radar IDs etc. are present in the SSR output and grep-able).
- **But the map, all imagery, status chips, time slider, opacity slider and panel state are JS-driven** — the page is `ClientPageRoot` (`"use client"`). No SSR data fetch happens.
- **Plain curl gets you exactly zero data values**: no current image, no radar-status chip, no timestamps. Every dynamic value comes from `fetch()` calls *after* hydration.
- For a tool like Playwright/Puppeteer: wait for the API calls below to complete; the canonical "ready" signal is the radar-status fetch (always fires on mount).

## 3. UI structure (left panel / right panel / map)

Header (sticky, `bg-theme_green` CSU green): CSU stacked logo, hamburger (mobile), "Home", two UTC clock chips (rendered with `opacity:0` until JS swaps in the time), "Log in" → `/login`.

Body is a left **accordion panel** + an OpenLayers **map** that fills the rest of the viewport. The accordion contains four sections:

| Section | Items in UI (id → label) |
|---|---|
| **Radar Data** | `qpe_15min` "Total Precip, 15 minute QPE" · `qpe_1hr` "Total Precip, 1 hour QPE" · `radar_pr` "Precip Rate" · `comp_ref` "Reflectivity" · `comp_now` "Reflectivity Nowcast" · `individual_radar` "Individual Radars" |
| **Atmospheric Forecast** | `tp` "Hourly Accumulation" · `total_precip_actual` "Total Precipitation" · `pr` "Precip Rate" · `temp` "Temperature" |
| **CoSMoS Data** | `water_level` · `water_depth` · `max_water_level` · `max_water_depth` |
| **National Water Model** | `stream_reach` |

Plus shared controls:
- **Image Opacity** slider (0–100) — MUI Slider.
- **Time/animation controls** (visible after a product is picked): Play / Stop / Step First / Backward / Forward / Last / "Small steps"; current date/time chip in UTC.
- **Unit toggle Chips** per product: `in`/`mm` (precip), `F`/`DEG` (temp, `°C`), `ft` (water), `dBZ` (reflectivity).
- **Colorbar PNG** swaps based on product + unit (see §5).
- **Radar Status** popover with one chip per radar (id+state). Default fallback list rendered before fetch: `XSCV, XSCW, XSCR, XEBY, XSWR`.

When the user picks **Individual Radar**, the screen splits into **two synchronized OL viewports** ("left X-band images" / "right X-band images") — each fetches from `/api/xbandRadarImages` independently.

Clicking a **Stream Reach** point triggers two warm-up API calls and then `router.push('/ui/public/stream-graph?comid=…')` — a separate, also-client-rendered page.

## 4. Map

- **Library: OpenLayers** (chunks contain OL projection/CRS strings).
- **Base layer**: `https://tile.openstreetmap.org/{z}/{x}/{y}.png` with the standard `© OpenStreetMap` attribution.
- **Center**: `[-13610058.98, 4561655.27]` (EPSG:3857) ≈ **37.96°N, 122.26°W** — San Francisco Bay Area.
- **Zoom**: 7.5.
- **Projections in use**: `EPSG:3857` (display), `EPSG:4326` (data overlays — `imageExtent` set on `ol/source/Image`), `EPSG:32610` (UTM Zone 10N, California), plus a proj4 string `+proj=utm +zone=10 +datum=WGS84 +units=m +no_defs`.
- **siteLayer (top z-index 1e3)** is drawn from a `radarCoordinates` registry baked into the bundle:

```text
XSCV  37.3989,-121.8334  range 40 km   (X-band, San Jose / Coyote Valley)
XSCW  38.5216,-122.8022  range 40 km   (X-band, Sonoma)
XSCR  36.9847,-121.9786  range 40 km   (X-band, Santa Cruz)
XEBY  37.8156,-122.0620  range 40 km   (X-band, East Bay)
KBBX  39.4956,-121.6316  range 100 km  (NEXRAD)
KDAX  38.5010,-121.6770  range 100 km  (NEXRAD)
KMUX  37.1553,-121.8980  range 100 km  (NEXRAD)
```

(Note: `EBAY` from the status API ↔ `XEBY` in the coords/UI fallback. The composite reflectivity description mentions a 5th X-band, `XSCV`, plus C-Band — the C-band appears in radar-status as a separate row but not in the coord table.)

- **Vector overlays**:
  - `/geojson/flowlines.geojson` — **26 MB** (NHD stream network).
  - `/geojson/watersheds.geojson` — **19 MB**.
  - `/data/stream_data.csv` — 468 USGS sites (header `COMID,LatSite,LonSite,Status`; Status = B (416) or R (52)).
- **Vector zIndex**: `siteLayer` at 1000; data raster at 10.

## 5. Data flow / every endpoint

All endpoints are **same-origin** (no CORS). Django paths require a trailing slash (return 301 if omitted — `/api/imageData?file=` and `/api/productDetail?file=` are the two exceptions; they accept query without slash). Below "Triggered by" is the UI action that fires the request.

| # | Endpoint | Method | Params | Returns | Triggered by |
|---|---|---|---|---|---|
| 1 | `/api/radar-status/` | GET | — | `[{radar, status: "UP"\|"DOWN"}]` | mount of `/public` |
| 2 | `/api/productDetail?file=<rel_path>` | GET | `file` = relative path of the `details*.json` for the selected product | `{product, time, steps:[{imageName, timestamp, day, date, time}]}` | selecting any product |
| 3 | `/api/imageData?file=<rel_path>` | GET | `file` = `<product.images>/<step.imageName>` | PNG (no-store, ETag header) | each step in the time series + opacity swap; **cache-busted with `&t=Date.now()`** |
| 4 | `/api/xbandRadarImages/?radarFolder=<R>&productPrefix=<P>` | GET | `radarFolder` ∈ {XSCV, XSCW, XSCR, XSWR, XEBY/EBAY, CBAND}, `productPrefix` ∈ {CorrReflectivity, CorrDifferentialReflectivity, FilteredPhiDP, PhiDP, RhoHV, Velocity, …} | `{images:[], meta:[], timezone, plotsRoot:"/XBand/PRODUCTS/Chivo/plots", windowStartUtc, windowEndUtc}` — **1-hour rolling window** | "Individual Radar" mode, two parallel fetches (left & right viewport) |
| 5 | `/api/get_stream_data/<comid>/<YYYYMMDD_HH>` | GET | path: COMID + UTC hour | `{headers, values}` (currently echoes only `COMID`) | warm-up before stream-graph nav |
| 6 | `/api/get_observed_stream_data/<comid>/<YYYYMMDD>` | GET | path: COMID + UTC date | same shape | warm-up before stream-graph nav |
| 7 | `/data/stream_data.csv` | GET | — | CSV (16.7 KB) | mount |
| 8 | `/geojson/flowlines.geojson` | GET | — | 26 MB | mount (heavy!) |
| 9 | `/geojson/watersheds.geojson` | GET | — | 19 MB | mount (heavy!) |
| 10 | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` | GET | — | tiles | map render |

Static colorbars (small PNGs, served by nginx):

```
/images/colormap_accum_in.png      /images/colormap_accum_mm.png
/images/colorbar_prate_in.png      /images/colorbar_prate_mm.png
/images/colormap_ref.png
/images/colormap_forecast_accum_in.png  /images/colormap_forecast_accum_mm.png
/images/colorbar_forecast_prate_in.png  /images/colorbar_forecast_prate_mm.png
/images/colormap_temp_f.png        /images/colormap_temp_deg.png
/images/colormap_level_ft.png      /images/colormap_depth_ft.png
/xband_radar_data/colourbar/{reflectivity,differential_reflectivity,differential_phase,radial_velocity}.png
/xband_radar_data/colourbar/rhohv.png  ← referenced but 404 (broken link)
```

Error semantics:
- `imageData` → 400 `{"error":"Missing file"}` (no param) · 404 `{"error":"File not found"}` (unknown file).
- `productDetail` → 200 even when content is stale.
- `get_stream_data` → 400 `{"error":"Invalid timestamp format. Use YYYYMMDD_HHMM"}` (despite docs/usage suggesting `_HH`, the validator wants `_HHMM`).
- Bad COMID (empty) → Django's HTML 404 page bleeds through.

## 6. The product registry (verbatim from the bundle)

This is the single source of truth for the visual products. Each entry says how to ask the API for it.

```text
RADAR DATA
  qpe_15min            images=rain15min/images/                details_{in,mm}=rain15min/details_{in,mm}.json            colorbar=colormap_accum_{in,mm}.png
  qpe_1hr              images=rain60min/images/                details_{in,mm}=rain60min/details_{in,mm}.json            colorbar=colormap_accum_{in,mm}.png
  precip_rate (radar)  images=rainrate/images/                 details_{in,mm}=rainrate/details_{in,mm}.json             colorbar=colorbar_prate_{in,mm}.png
  comp_ref             images=composite_ref_max/images/        details=composite_ref_max/details.json                    colorbar=colormap_ref.png (dBZ)
  comp_now             images=composite_nowcast/images/        details=composite_nowcast/details.json                    colorbar=colormap_ref.png (dBZ)
  individual_radar     → /api/xbandRadarImages/?radarFolder=<R>&productPrefix=<P>

ATMOSPHERIC FORECAST  (HRRR 0-18h, NBM 19-120h; 3km, hourly except precip_rate=15min)
  tp                   images=total_precip/images/             details_{in,mm}=total_precip/details_{in,mm}.json         colorbar=colormap_forecast_accum_{in,mm}.png
  total_precip_actual  images=total_precip_cumulative/images/  details_{in,mm}=total_precip_cumulative/details_{in,mm}.json
  pr (forecast)        images=precip_rate/images/              details_{in,mm}=precip_rate/details_{in,mm}.json          colorbar=colorbar_forecast_prate_{in,mm}.png
  temp                 images=temperature/images/              details_{F,DEG}=temperature/details_{F,Deg}.json          colorbar=colormap_temp_{f,deg}.png

COSMOS  (1-hour cadence, sub-tidal/coastal)
  water_level          images=water_level/images/              details=water_level/details.json                          colorbar=colormap_level_ft.png
  water_depth          images=water_depth/images/              details=water_depth/details.json                          colorbar=colormap_depth_ft.png
  max_water_level      images=max_water_level/images/          details=max_water_level/details.json
  max_water_depth      images=max_water_depth/images/          details=max_water_depth/details.json
```

There's also a `userProductsDescription` map suggesting an authenticated/admin product set with model selectors (`HRRR_TP, HRRR_PR, HRRR15_PR, GFS_TP, BLEND_TP, QPE_15m, QPE_1hr, QPE_PR`) — not exposed on `/public`.

## 7. Live state captured during this scan (2026-05-15 ~23:58 UTC)

**Radar status** (from `/api/radar-status/`):

```json
[{"radar":"XSCV","status":"DOWN"},
 {"radar":"XSCW","status":"UP"},
 {"radar":"XSCR","status":"UP"},
 {"radar":"XSWR","status":"UP"},
 {"radar":"EBAY","status":"DOWN"},
 {"radar":"CBand","status":"UP"}]
```

2 / 6 radars **DOWN**: XSCV, EBAY.

**Per-product latest-step age (negative = into the future, i.e. forecast/nowcast):**

```text
rain15min            last=2026-05-15T23:56   +4 min        ✓ healthy (2-min cadence)
rain60min            last=2026-05-15T23:56   +4 min        ✓
rainrate             last=2026-05-15T23:56   +4 min        ✓
composite_ref_max    last=2026-05-15T23:56   +4 min        ✓
composite_nowcast    last=2026-05-16T00:52   -52 min       ✓ (correct nowcast lead)
total_precip         last=2026-05-11T00:00   +10 DAYS      ✗ STALE pipeline
total_precip_cum     last=2026-05-11T00:00   +10 DAYS      ✗
precip_rate (fcst)   last=2026-05-06T14:00   +9 DAYS       ✗
temperature_F        last=2026-05-11T00:00   +10 DAYS      ✗
water_level          last=2026-05-16T15:00   -15 h forecast ✓
water_depth          last=2026-05-16T15:00   -15 h forecast ✓
max_water_level      n=1                                   (single max-of-window)
max_water_depth      n=1                                   (single max-of-window)
```

And the forecast images are not only stale by metadata — `imageData` returns **404** for `total_precip/images/C_hrrr_accum_step0.png` etc. So the forecast pipeline is producing neither updated `details.json` nor surviving images.

## 8. Recommended health-monitoring strategy

You don't need to render the page to monitor everything visible on it. The full state is reachable from these endpoints, in order from cheap to expensive:

1. **Cheap continuous heartbeat (every 1–2 min)**
   - `GET /api/radar-status/` → alert on any radar flipping `UP → DOWN` (5 X-band: XSCV, XSCW, XSCR, XSWR, EBAY/XEBY; 1 C-band).
   - For each product in §6, `GET /api/productDetail?file=<details>` and check:
     - HTTP 200 & valid JSON
     - `steps` non-empty
     - `now - max(timestamp)` against the expected cadence: 2-min (radar QPE/rate/comp_ref), 30-min lead (comp_now), 60-min (forecast/water), forecast-horizon (max_water_*).
     - Number of steps drifting low (e.g. forecast should be 72–75; saw exactly that, but on a stale data file). If `n` shrinks, pipeline truncating.

2. **Image-existence spot-check (every 5–10 min, per product)**
   - Take the latest `imageName` from `productDetail` and `HEAD /api/imageData?file=<images><imageName>`. Anything ≠ 200 = broken (the forecasts currently fail this).
   - For one or two products, also `GET` the PNG and verify `Content-Type: image/png` and size > a sane floor (e.g. composite_ref_max images come in ~130 KB).

3. **Visual / perceptual check (optional, lower-frequency)**
   - Decode the PNG and look at it for sanity (non-blank, % of non-transparent pixels, dBZ histogram in expected range). Useful for catching the failure mode where the file exists but is all-zeros / all-white.
   - Compare the bbox of non-transparent pixels with each radar's `range` circle from `radarCoordinates` to spot "radar dropped from mosaic" situations even when `radar-status` reports UP.

4. **X-band per-radar (every 2 min)**
   - `GET /api/xbandRadarImages/?radarFolder=<R>&productPrefix=CorrReflectivity` for each of {XSCV, XSCW, XSCR, XSWR, XEBY, CBAND}.
   - Alert when `images:[]` while `radar-status` says `UP` (current state for XSCW even though it's UP — worth a closer look, but the API window is the last 60 min so a brief outage matches).
   - The endpoint reveals the *real* on-disk path (`plotsRoot: /XBand/PRODUCTS/Chivo/plots`), which is useful if you ever get access to the upstream filesystem.

5. **Vector layers (rarely; large)**
   - `HEAD /geojson/flowlines.geojson` and `/geojson/watersheds.geojson` — alert on `Last-Modified` regression. (Currently both: `Wed, 07 Jan 2026`.) Same for `/data/stream_data.csv`.

6. **Colorbars (one-time)**
   - HEAD all 14 colorbar PNGs once on deploy. `/xband_radar_data/colourbar/rhohv.png` is **404** in code today — that's a pre-existing broken link in the bundle worth filing.

## 9. Reference artifacts saved locally during this scan

- `/tmp/radarca_public.html` — full landing HTML (65 KB).
- `/tmp/rsc_decoded.txt` — decoded `__next_f` Flight payload.
- `/tmp/radarca_chunks/*.js` — 11 downloaded JS chunks (the `page-b235e445a494c5d8.js` is the most useful — it has the entire product registry and radar coord table).
- `/tmp/rs.json` (radar-status), `/tmp/pd*.json` (sample productDetail responses), `/tmp/img.png` (a real composite_ref PNG, 843×1108 RGBA).
