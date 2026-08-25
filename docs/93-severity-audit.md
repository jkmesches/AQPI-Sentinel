# Severity audit (v0.1.2 reshape)

What every check emits, what it means, and which routing tier it
lands in. Companion to [`92-glossary.md`](92-glossary.md) — the
glossary defines the vocabulary; this doc records the policy.

## The three tiers

| Tier | Meaning | Status values |
|---|---|---|
| **Action required** | Broken. Auto-pages after 30 minutes if still open. | `fail`, `error` |

> **`fail` vs `error`.** Both sit in the action-required tier, but they are no
> longer rendered identically. `fail` means the monitored thing is broken;
> `error` means the check could not determine its state (upstream API timeout,
> probe crash). Until 2026-08-25 both were painted the same red and summed into
> one "F" counter, which made ~1,500 upstream API timeouts/day read as radar
> outages. `error` now has its own colour and its own counter.
| **Attention required** | Degraded but not broken. Surfaces on the dashboard, doesn't page by default. | `warn` |
| **Useful to watch** | Healthy or out-of-scope. | `pass`, `skip` |

`severity_floor` on a push-routing config selects the tier:

- `info` (or unset) → All tiers notify.
- `warn` → Action-required only (skips warnings).
- `critical` → Long outages only (action-required tier still open
  past 30 min).

## Per-check classification

### L0 — Connectivity

| Check | Trip condition | Status | Tier |
|---|---|---|---|
| `layer0.net.internet` | Both HTTP probes fail | `fail` | Action |
| `layer0.net.dns` | All DNS probes fail | `fail` | Action |
| `layer0.origin.alive` | `/api/radar-status/` not 200/JSON | `fail` | Action |
| `layer0.website.public` | `/public` missing SSR markers | `fail` | Action |
| `layer0.website.root_notfound` | Not-found markers absent | `warn` | Attention |
| `layer0.tls.cert` (handshake) | TLS probe raises | `fail` | Action |
| `layer0.tls.cert` (expiry) | Days remaining < threshold | `warn` | Attention |

The TLS distinction matters: a failing handshake means the site is
genuinely unreachable over HTTPS *right now*. A nearing-expiry cert
means the site will be unreachable in N days but works today. First
is broken; second deserves attention but isn't yet a fire.

### L1 — Product Freshness

Per-product sub-check verdicts, folded with `worst_of()`:

| Sub-check | Trip condition | Verdict | Tier |
|---|---|---|---|
| `A_api_up` | Manifest unreachable | `fail` | Action |
| `B_schema` | Manifest not JSON / malformed | `fail` | Action |
| `C_freshness` | Latest image age exceeds threshold | `fail` | Action |
| `D_cadence` | Median inter-step Δ off cadence | `warn` | Attention |
| `E_step_count` | Step count outside tolerance | `warn` | Attention |
| `F_image_exists` | Latest image 404 | `fail` | Action |
| `G_image_size` | PNG smaller than threshold | `warn` | Attention |
| `H_image_hash` | (always pass — records sha256) | `pass` | — |
| `parity` | Filename↔manifest disagreement / non-contiguous step indices | `warn` | Attention |

Parity was reclassified from `fail` → `warn` in v0.1.2. Upstream
HRRR pipeline glitches (duplicate or out-of-order step files) are
data-quality issues, not service outages — the product is still
serving; the manifest just has a hiccup.

`H_image_hash` deserves a note: it's currently a "frozen image"
detector that's wired to always pass (the freshness check already
catches the operational concern). Kept in the sub-check list as
scaffolding for a future stale-image heuristic.

### L1 — Streams & vectors

| Check | Trip condition | Status | Tier |
|---|---|---|---|
| `layer1.stream.canary` | Live stream feed unreachable | `fail` | Action |
| `layer1.vector.flowlines` | Vector overlay 404 | `fail` | Action |
| `layer1.vector.watersheds` | Vector overlay 404 | `fail` | Action |
| `layer1.vector.stream_csv` | CSV feed 404 | `fail` | Action |

### L2 — Radar Scans

Verdicts from radar reconciliation:

| Verdict | Trip condition | Status | Tier |
|---|---|---|---|
| `HEALTHY` | Declared up + scans arriving | `pass` | — |
| `CONFIRMED_DOWN` | Declared down + no scans | `fail` | Action |
| `GHOST_UP` | Declared up + no scans within threshold | `fail` | Action |
| `STUCK_DOWN_FLAG` | Declared down + scans still arriving | `warn` | Attention |
| `OBSERVED_API_ERROR` | xband images endpoint failed | `error` | Action |
| `DECLARED_API_ERROR` | radar-status endpoint failed | `error` | Action |

`STUCK_DOWN_FLAG` is a flag-stuck-true upstream bug — the radar is
operating but the status feed says otherwise. Worth noticing,
doesn't need a page.

#### Fleet correlation

| Check | Trip condition | Status | Tier |
|---|---|---|---|
| `layer2.xband.fleet` | ≥4 of 5 X-band radars simultaneously unhealthy | `fail` | Action |

The five X-band radars sit at separate sites and do not fail in lockstep.
Measured over 14 days to 2026-08-25, **80.2%** of all `GHOST_UP` runs occurred
while 4–5 radars were ghosting at once, and only **2.8%** were isolated to a
single radar; one episode had four sites entering and leaving `GHOST_UP`
within the same second, 79 hours apart. Correlation that tight is one upstream
event, not five radar outages.

The five X-band radar checks list `layer2.xband.fleet` in `depends_on`, so when
it trips their alarms are still recorded but marked `suppressed_by` — one
actionable page describing the true scope instead of five saying the same
thing. `layer2.radar.CBAND` deliberately does **not** depend on it: different
band, different site, and it stayed healthy through the real episodes.

An isolated single-radar failure does not trip this check and pages exactly as
before.

### L3 — Map Overlays

| Verdict | Trip condition | Status | Tier |
|---|---|---|---|
| `MATCH` | Overlay timestamp == manifest timestamp | `pass` | — |
| `MISMATCH` | Overlay UI shows different timestamp than API | `warn` | Attention |
| `NO_API` | API field for comparison wasn't present | `warn` | Attention |
| Page-load error | Playwright couldn't render | `error` | Action |

### L4 — Image Quality

Sub-check verdicts from tier-1 + tier-2 image QC:

| Sub-check | Trip condition | Verdict | Tier |
|---|---|---|---|
| `extreme` | Saturated or near-empty | `warn` | Attention |
| `speckle` | Autocorrelation below threshold | `warn` | Attention |
| `range_ring` | Range-ring artifact detected | `warn` | Attention |
| `frozen` | No content change across window | `warn` | Attention |

L4 is by design an "attention" tier — image quality issues are
diagnostic, not outage-grade. A QPE image with bad speckle is still
served to users; it just needs investigation.

## Cascade demote

Cascade-demoted rows (downstream of an unhealthy upstream) get
`status=skip` regardless of what the check would otherwise have
returned. The dashboard shows them as gray with a `↑` badge. They
don't open alarms; the upstream's own alarm is the single
point-of-blame.

## Migration notes (v0.1.1 → v0.1.2)

- Existing alarm rows keep their `alarms.severity` value — the
  reshape applies to **newly-opened** alarms only.
- Existing routing rules with `status` matchers (`fail` / `error`)
  continue to work unchanged; `status` is still the raw five-value
  check signal.
- `severity_floor: warn` on a push device, which previously meant
  "all alarms" (because everything opened at warn), now means
  "broken only." If you want the old behavior, switch to
  `severity_floor: info`.
- Two checks changed verdict-level: `layer0.website.root_notfound`
  (fail → warn) and L1 parity (fail → warn). They'll stop
  contributing to fail-status alarms and start producing
  warn-status ones.

If your routes weren't using `severity_floor` heavily before, the
reshape is a no-op for your workflow. If you were, see the
`/admin/alerts` inline cheat-sheet for the new mapping.
