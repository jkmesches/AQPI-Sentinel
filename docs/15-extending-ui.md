# Extending the UI

The frontend is **SvelteKit 2 + Svelte 5 (runes) + Tailwind v4 +
MapLibre GL JS**. The architectural conventions are tighter than
typical — a few patterns came out of perf debugging — so this doc
focuses on the ones that matter for new contributions.

**Companion**: [`ARCHITECTURE.md`](ARCHITECTURE.md) for the data-
flow side (REST + WS, reactive store, microtask coalescing).

---

## File layout

```
frontend/src/
├── app.css                — Tailwind v4 entry + @theme tokens
├── app.html               — root HTML shell
├── hooks.server.ts        — UA-sniff mobile redirect
├── routes/
│   ├── +layout.svelte     — desktop chrome (top bar, stage strip, footer)
│   ├── +page.svelte       — home / dashboard
│   ├── timeline/          — desktop /timeline
│   ├── history/           — desktop /history
│   ├── admin/             — /admin/* (users, alerts, groups, …)
│   ├── settings/          — /settings/* (devices, …)
│   ├── m/                 — mobile shell (parallel /m/* tree)
│   └── login/             — /login
└── lib/
    ├── api.ts             — typed REST client + types
    ├── origin.ts          — API base URL detection + fetch prefix patch
    ├── ws.ts              — WebSocket client
    ├── push.ts            — Web Push subscribe/unsubscribe
    ├── format.ts          — stageLabel, productLabel, prettyCheckLabel
    ├── diag.ts            — ?diag= bisect harness flags
    ├── platform.svelte.ts — iOS/Android detection + install-prompt store
    ├── stores/
    │   ├── state.svelte.ts — global reactive store (rollup, alarms, metrics)
    │   ├── auth.svelte.ts  — auth state
    │   └── theme.svelte.ts — light/dark toggle
    └── components/
        ├── StatusDot.svelte
        ├── Sparkline.svelte
        ├── PieStatus.svelte
        ├── MultiSelectChips.svelte
        ├── MapView.svelte
        ├── HistoryDetailModal.svelte
        ├── PushRoutingEditor.svelte
        └── mobile/
            ├── MobileNav.svelte
            ├── MobileStatusMap.svelte
            └── MobileDrillDown.svelte
```

**Mobile is a parallel tree under `/m/*`** — UA-sniff in
`hooks.server.ts` redirects phones to `/m` from the root. Mobile
pages compose their own shell and components; they don't share
chrome with the desktop layout.

---

## Reactive store

`lib/stores/state.svelte.ts` is the single source of truth for live
data:

```typescript
export const sentinel = $state<{
    rollup:  StatusRollup | null;    // GET /api/status response
    alarms:  Alarm[];                 // GET /api/alarms
    metrics: Record<string, {ts: number; value: number}[]>;  // keyed `check_id|metric`
    pulseTick: Record<string, number>;  // status-transition counters
    // ...
}>({...});
```

Which series land in `metrics` is decided in one place —
`sparklineSeries()` walks whatever stages the rollup has and asks
`format.ts` two questions per check: `sparklineMetric()` (what to
draw) and `readoutMetric()` (what to print as a number). A rail that
reads a key nobody asked for gets an empty array and renders nothing,
with no error, so add the question rather than the lookup. Asserted by
`validation_tests/js/test_sparkline_coverage.mjs`.

Two write sources:

1. **REST poll** every 5s — refreshes the whole rollup atomically.
2. **WebSocket push** for live events (alarm_open, run, etc.)
   — only fires on transitions (see the "transition-only broadcast"
   comment in `backend/api/app.py`).

Three perf-critical conventions:

### Atomic replacement, not in-place mutation

Don't mutate `sentinel.rollup.stages` in place:

```typescript
// ❌ WRONG — re-renders every subscriber on every write
sentinel.rollup.stages.L1.push(newRow);

// ✓ RIGHT — atomic replacement; subscribers re-render once
sentinel.rollup = { ...sentinel.rollup, stages: { ...stages, L1: [...L1, newRow] } };
```

Svelte 5's deep reactivity tracks individual property writes. At
~30 events/min across 17 subscribers, in-place mutation freezes
the tab in 10–15 minutes. The atomic-replacement pattern + microtask
coalescing (`pendingMerge` in `state.svelte.ts`) is what makes the
live tab survive long sessions.

### Microtask-coalesced WS bursts

Don't apply WS events one at a time:

```typescript
// ❌ WRONG
ws.on('run', (e) => { sentinel.runs = [...runs, e.run]; });

// ✓ RIGHT — coalesce in a microtask
if (!this.pendingMerge) this.pendingMerge = new Map();
this.pendingMerge.set(e.run.check_id, e.run);
if (!this.mergeScheduled) {
    this.mergeScheduled = true;
    queueMicrotask(() => this.flushMerge());
}
```

A burst of 30 WS events in one tick lands as one reactive update,
not 30.

### Pause on tab hidden

```typescript
if (typeof document !== 'undefined' && document.hidden) {
    // drop the event; visibilitychange will trigger a full refresh
    return;
}
```

Backgrounded tabs don't accumulate state; they re-read on focus.
This is what keeps long-running tabs from leaking.

---

## Tailwind v4 conventions

The theme follows Colorado State University's colors. `app.css` sets the dark
theme's tokens in its `@theme` block, and the light theme overrides them under
`[data-theme="light"]`:

```css
@theme {
    --color-canvas:  #0a130d;    /* near-black with CSU Green's hue */
    --color-default: #e3e8e1;
    --color-accent:  #C8C372;    /* CSU Gold */
    --color-ok:      #39a758;    /* CSU Green lifted to read on the dark canvas */
    /* ... */
}
[data-theme="light"] {
    --color-canvas:  #f6f6f2;
    --color-accent:  #1E4D2B;    /* CSU Green */
    /* ... */
}
```

Use the tokens, not literal colors:

```svelte
<div class="bg-[var(--color-surface)] text-[var(--color-bright)] border-[var(--color-border)]">
```

A literal color breaks the light/dark toggle, and the `ui check` palette rule
reports it. A few rules keep the theme readable:

- Every text and status token is at least 4.5:1 on the canvas, surface and
  surface-hi of its theme, and the comment beside a token states its measured
  ratio. `validation_tests/test_theme_palette.py` checks both, so run it after
  a token edit.
- The header band (`.band`) is CSU Green in both themes and sets its own text
  and status colors from the `--color-band-*` tokens, since the page's status
  colors do not read on green. A block that redefines a token takes it from
  another token, never from a literal.
- MapLibre paints cannot read CSS custom properties, so the map takes its
  colors from `$lib/palette.ts`, which mirrors the tokens it needs. The palette
  test fails if the two drift apart.
- No text is set below 12 px. Text is Poppins, and numbers, times and IDs use
  the `num` class (a monospaced face with tabular figures).

**Status colors are mapped through `statusText()` / `statusBorder()`
in `format.ts`** — use them rather than re-coloring per call site.

---

## Mobile-specific patterns

### Touch target ≥ 44px

iOS HIG floor. Every tappable element gets `min-height: 44px` or
equivalent padding:

```svelte
<button class="min-h-[44px] ...">
```

### Disable tap highlight

iOS Safari paints a translucent gray on every tap. Suppress it
on custom buttons:

```svelte
<button style="-webkit-tap-highlight-color: transparent;">
```

### Bottom-nav clearance

The `MobileNav` floats at the bottom. Every scrollable mobile page
adds a bottom spacer:

```svelte
<div style="height: calc(72px + env(safe-area-inset-bottom, 0px));"></div>
```

### Safe-area insets

iPhone's home indicator + notch:

```svelte
<header style="padding-top: env(safe-area-inset-top);">
<nav style="padding-bottom: env(safe-area-inset-bottom);">
```

### MobileDrillDown for tap-to-expand

The `MobileDrillDown` component is a slide-up full-screen sheet
with safe-area padding + a back arrow. Use it for any detail view
that doesn't justify its own route:

```svelte
<MobileDrillDown bind:open={detailOpen}
    title={r.target} subtitle={r.check_id}
    stage={r.stage} status={r.status}>
    <!-- snippet body here -->
</MobileDrillDown>
```

**Don't use `<a href>` for nav inside a drilldown** — same-route
param-only nav silently no-ops while the drilldown stays open. Use:

```svelte
<button onclick={() => { detailOpen = false; goto(url); }}>
```

---

## Map integration

`MapView.svelte` (desktop) and `MobileStatusMap.svelte` (mobile)
wrap MapLibre. Three patterns to know:

### WebGL context-loss guard

iOS Safari drops the WebGL context under memory pressure. After
context loss, `map.style` is undefined and any `getLayer()` call
throws. Both components implement `mapDead` state + bail-early
guards (`mapAlive()`):

```typescript
function mapAlive(): boolean {
    return !!(map && !mapDead && map.style);
}

// In every mutation function:
if (!mapAlive()) return;
```

Plus a user-visible "Reload map" affordance.

### Style-ready vs isStyleLoaded

`isStyleLoaded()` flickers between true and false every time we
call `setData()` on a source. Track style-ready ourselves with a
one-shot `styleReady` flag that flips true on `map.on('load', ...)`.

### Image cache for radar overlays

The shared `_IMAGE_CACHE` LRU in
`backend/api/routes/upstream.py` keeps recent PNG fetches warm.
Frontend pre-fetches via `prefetchNexradFrames()` — flicker-free
scrubbing on the map's playback strip depends on this.

---

## Adding a new page

Drop a `+page.svelte` into `routes/`:

```svelte
<!-- frontend/src/routes/my-page/+page.svelte -->
<script lang="ts">
    import { sentinel } from '$lib/stores/state.svelte';
</script>

<div class="p-4">
    <h1 class="text-[20px] text-[var(--color-bright)]">My page</h1>
    <pre>{JSON.stringify(sentinel.rollup, null, 2)}</pre>
</div>
```

That's it. SvelteKit picks it up at the next build. The desktop
chrome (`+layout.svelte`) wraps it automatically.

For a mobile parallel page, drop it under `routes/m/my-page/+page.svelte`.
The UA-sniff doesn't redirect *to* deep mobile routes — users land
on `/m` and navigate; you may want to add a tab to
`MobileNav.svelte` if the page should be top-level navigable.

---

## Adding an admin page

Admin routes are gated server-side (the backend's `require_admin`
dep). On the frontend, conditionally render based on `auth.user.role`:

```svelte
{#if auth.user?.role === 'admin'}
    <a href="/admin/my-feature">My feature</a>
{/if}
```

The route file itself doesn't need special markup — the
authenticated fetch calls inside will 403 if a non-admin somehow
navigates there.

---

## ?diag= harness

Bisect perf issues by toggling subsystems off:

| Flag | Disables |
|---|---|
| `no-ws`    | WebSocket subscription |
| `no-poll`  | 5s polling refresh |
| `no-map`   | MapView |
| `no-spark` | Sparkline SVGs |
| `no-tick`  | 1s clock tick |
| `no-pulse` | Status-dot transition animation |

The flags are read by `lib/diag.ts` and each subsystem checks `diag.X`
before rendering / subscribing. Add a new flag by extending the
const map in `diag.ts`, following the existing entries.

One flag turns something on rather than off:

| Flag | Publishes |
|---|---|
| `probe` | `window.__sentinelProbe()`, a snapshot of the map's structure |

---

## Visual baseline

`validation_tests/visual` holds the map console to its current behavior
across a refactor. `capture.py` drives 28 `mapPrefs` states through
`?diag=probe` and writes one JSON record per state; `compare.py` diffs two
captures and exits non-zero on a structural change.

```bash
python validation_tests/visual/capture.py /tmp/before --shots
# ... change the code, deploy ...
python validation_tests/visual/capture.py /tmp/after
python validation_tests/visual/compare.py /tmp/before /tmp/after
```

The snapshot is structure rather than pixels: the camera, every layer in
paint order, the sources MapView adds, and the paint and layout of each layer
on them. The imagery underneath is live, so a pixel diff is mostly noise, and
a screenshot cannot see a layer added at the wrong `beforeId`.

State comes from `localStorage` rather than from clicking, so the matrix
survives a markup rewrite.

Two things the harness handles that a first attempt will not. Several fields
move on their own (a fresh `blob:` id per load, `Date.now()` inside the
burn-scar paint expressions, the tilt overlay's corners, which come from the
scan's own `center` and `range_km`), so each state is captured twice, and the
differing keys are measured instead of guessed. Layer order between the
vector overlays also depends on which fetch returns first, so a state whose
own two loads disagree is recorded as unstable and an order change on it is
reported as a warning.

Capture against a deployment that has the layers you care about. Taking a
baseline on `the test deployment` and comparing it against the AQPI deployment
compares two profiles, not two versions of the code.

---

## What to NOT do

- **Don't add `credentials: 'include'` to fetch calls.** Combined
  with `allow_origins=['*']` it's forbidden by browsers. Use
  Bearer tokens (already wired globally via `installFetchPrefix()`).
- **Don't mutate `sentinel.rollup.stages` in place.** Atomic
  replacement only — see the "Reactive store" section above.
- **Don't broadcast WS events on every state change in the
  backend.** Only on transitions. Look at `_maybe_broadcast` in
  `backend/api/app.py`.
- **Don't read `config.PRODUCTS[…]` directly from a check** — use
  the threshold registry (`backend/thresholds.py`).
- **Don't use literal colors.** Always reference `--color-*`
  tokens so light/dark works.
- **Don't bake hostnames into the frontend image** unless you have
  to. The runtime `origin.ts` heuristic handles most deploys.

---

## Where to go from here

- **[`13-extending-checks.md`](13-extending-checks.md)** — adding
  a backend check (whose data your UI will display).
- **[`14-extending-api.md`](14-extending-api.md)** — adding the
  REST endpoint your new UI page reads from.
- **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — the bigger picture
  your frontend lives in.
