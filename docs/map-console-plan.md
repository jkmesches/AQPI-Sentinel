# Map Console Plan

Target release: the next minor. This plan splits the Live page into a status dashboard
and a radar console, then builds the console past parity with both RadarCA and
the current map.

Written 2026-11-20, after reading `frontend/src/lib/components/MapView.svelte`
(3,201 lines), `routes/+page.svelte`, `lib/radarStatus.ts`, `lib/mapPrefs.ts`
and `docs/radarca-public-characterization.md`.

## 1. Decisions

Joseph settled these before the plan was written. The sections below build on
them rather than re-opening them.

| Decision | Choice |
|---|---|
| Routing | `/` is the console, `/dashboard` is the Dashboard |
| Scope | Legend, cursor readout, permalinks, unit toggles, compare panes |
| Structure | Decompose MapView behind a layer registry |
| Mobile | Console responsive to phone width, `/m/` keeps MobileStatusMap |
| Readout | Ramp bin only, blank when the pixel is off-ramp |
| Compare | Any pair of selections, shared timeline, locked cameras |
| Dashboard | Superset of today's rails, relaid out for full width |
| Radar pins | LB2 paints, LB3 where LB2 has no row, unknown where neither does |
| Version | the next minor |

## 2. Parity Gaps

We carry all four of RadarCA's product families, 13 products against their 15.
Four things it has that we do not:

1. A colorbar per product and unit. We paint values with no scale at all, so
   an operator can see where the rain is and not how much.
2. Unit toggles for in/mm and F/°C. `backend/config.py` pins one unit per
   product today (`in`, `dBZ`, `ft`, `F`).
3. Two synchronized viewports when you pick Individual Radar.
4. A flowlines layer, the 26 MB NHD stream network. Out of scope here, because
   our watersheds file is already heavy and nothing has asked for it.

Everything else runs ahead already. Dark and light theming, NEXRAD WMS-T,
terrain with 3D pitch, burn scars with an age window, hydrographs in the
popup, tilt elevations, five moments, simultaneous multi-radar overlay,
blank-frame skipping, atomic frame swaps, health-colored radar markers,
staleness banners sourced from L1 verdicts, and persisted prefs.

One console feature is impossible rather than unscheduled. A vertical
cross-section needs raw volume data and upstream publishes plan-view PNGs, so
it is not on any list.

## 3. Radar Status Precedence

`lib/radarStatus.ts` takes `worstOf` across L2, LB2 and LB3 today, which lets
radarca's opinion paint a pin red while the files are landing on disk
normally. The new rule prefers the filesystem.

```
if the deployment has any backend row (LB2 or LB3):
    pin = worst of the target's LB2 rows           # arrival, preferred
    pin = worst of the target's LB3 rows           # when no LB2 row exists
    pin = NO_READING                               # when neither exists
else:
    pin = worst of the target's L2 rows            # today's behavior
```

Radarca never paints a pin on a deployment that reads the filesystem. A
deployment with no backend row anywhere keeps painting from L2, because graying
out the pins on the test deployment would answer a different question. The tier is decided
by rows rather than by a stage key, so a present-but-empty LB2 falls back
instead of graying the fleet.

An `error` row reads through like any other status. A check that broke is not
the same as a check that was never registered, and `skip` would claim the
second.

The hover card lists every stage that reported the radar, each labeled by what
it observes (arrival, composite, radarca) and carrying its own status, with the
pin's own row brightened. A divergence therefore stays findable without a new
map symbol.

The LB3 fallback is there for the three NEXRADs. KBBX, KDAX and KMUX are drawn
on the AQPI deployment map and carry `layer3.composite.*` and `layer3.input.*` rows
with no LB2 row at all, because a NEXRAD reaches the composite by a path with
no arrival check. Composite input is therefore the only thing Sentinel observes
about them. Treat it as their arrival reading rather than as a second kind of
fault. For the five X-bands and CBAND, where a real LB2 row exists, arrival
still wins, and a composite fault stays off the pin.

The dashboard's collapsed radar rows take the same verdict. A pin and a row
header therefore cannot disagree about one radar.

That consistency costs something, and the cost is paid on the Products rail. A
`layer3.composite.<radar>` failure no longer colors the collapsed radar row, so
a radar arriving fine and missing from the composite reads as healthy there.
The fault belongs to the composite, and it is reported on the composite's own
row under the radar input count, named with each radar that is short and its
status. Each rail answers one question.

Which product that row belongs to is a profile's own answer, since nothing
links a composite run log to a product id. `config.COMPOSITE_PRODUCT` declares
it (`comp_ref` on AQPI, `composite_ref` on XQPI), import refuses a value absent
from that profile's `PRODUCTS`, and `/api/version` carries it to the rail. A
backend that predates the field names no product and the rail shows no input
row, because attaching a composite's fault to a guessed product is worse than
leaving the reading in the radar rail alone.

## 4. Module Layout

MapView holds map lifecycle, eight layer families, playback, prefs, the gauge
chart generator and the whole control panel in one file. The project's own
testing convention wants logic in `$lib/*.ts` where a `.mjs` runner can reach
it, which is the second reason to decompose.

```
lib/map/
  core.svelte.ts        map lifecycle, style swap, resize, death detection
  playback.svelte.ts    step list, play loop, frame barrier, activity skipping
  panes.svelte.ts       compare pane state, camera lock
  permalink.ts          URL codec, precedence against mapPrefs
  units.ts              per-product unit vocabulary, labels, conversion
  readout.ts            offscreen sampling, pixel to ramp bin
  layers/
    registry.ts         the LayerModule interface and the registry
    composite.ts  nexrad.ts  radars.ts  tilt.ts
    watersheds.ts reservoirs.ts streamGauges.ts burnScars.ts terrain.ts
  ramps/
    data/*.json         color to value bins, transcribed and dated
    invert.ts           nearest-ramp lookup with a tolerance
```

Each layer declares one shape. Adding a layer then becomes a data change.

```ts
interface LayerModule {
	id: string;
	prefsKey?: keyof MapPrefs;
	ensure(m: maplibregl.Map, ctx: LayerCtx): void;
	refresh(m: maplibregl.Map, ctx: LayerCtx): void;
	remove(m: maplibregl.Map): void;
	/** Frame-barrier participant. Loads, then returns the paint to apply. */
	prepare?(ctx: FrameCtx): Promise<FrameApply | null>;
}
```

The barrier that v0.12.0 added stays as it is. Three participants written by
hand become whatever the registry reports. A new layer therefore joins atomic
frame swaps without wiring of its own.

## 5. Ramps and the Legend

The legend and the readout both need the mapping from color to value, and the
value half is the hard part. Sampling RadarCA's colorbar PNGs gives the colors
exactly. The numbers beside them are rendered text. They get transcribed by hand once,
checked in with the source URL and the date, and reviewed.

A ramp that is wrong produces a legend that lies and a readout that lies
confidently. Each one is therefore measured before it ships. The check samples
the distinct colors in a set of real frames for that product and reports what
fraction maps to a ramp entry within tolerance. Full coverage means the ramp
is right. Anything short of that means the ramp is wrong or incomplete, and
the readout stays off for that product until it is fixed.

The legend renders as themed SVG rather than a proxied PNG. It therefore reads
in both themes, scales on a phone, and can carry the selected unit.

### Measurement Results

Shipped on 2026-11-21. The colorbars are on the mount rather than on
radarca's site, written beside the frames by the code that paints them, so
they are the painting code's own tables rather than a transcription.
`validation_tests/visual/extract_ramp.py` reads them and
`ramp_coverage.py` measures each one, with the results checked in as
`frontend/src/lib/map/ramps/data/coverage.json`. The registry answers only
for pairs that file records, so a ramp cannot reach the screen without its
evidence.

AQPI's X-band reflectivity bar carries the same colors as FLOW's over a
different range, -20 to 80 dBZ against -30 to 80. The same pixel is 4.86 dBZ
on one deployment and -2.40 on the other, so the ramps are keyed per radar
rather than per moment.

The C-band (radar id `CBAND`, folder `sscb`) shares the X-bands'
reflectivity and velocity ramps and paints ZDR and PhiDP from some other
colormap, with none of its 253 colors matching across a million opaque
pixels. It publishes no colorbar, so those two have no ramp and the legend
stays off for them. Measuring per radar rather than per band is what caught
this.

`KBBX`, `KDAX` and `KMUX` publish a reflectivity bar that the console never
paints. Its NEXRAD overlay draws Iowa State's `nexrad-n0q-wmst` layer, which
uses the NWS N0Q ramp. That bar is measured and deliberately left out.

RhoHV has no ramp on either deployment, so slice 6's readout will stay off
for it.

## 6. Cursor Readout

Hovering the overlay reports the bin the pixel falls in, such as `35-40 dBZ`,
and reports nothing when no ramp color sits within tolerance. Anti-aliasing at
feature edges and PNG quantization both produce colors between bins, and the
nearest match to one of those is a number that looks authoritative and is
wrong. Blanking is the honest answer there.

Implementation draws the current frame to an offscreen canvas once per frame
swap, then samples on pointer move. Sampling cost stays off the hot path
because the canvas is only redrawn when the frame changes.

Shipped on 2026-11-21. Two of its properties constrain slice 9's second
pane.

The inverse runs in Web Mercator, since that is how MapLibre interpolates
between an image source's corners. A latitude-linear inverse is exact at both
edges of the overlay and worst in the middle, where it is off by 1.17 km over
a Bay Area box, so it reads correct wherever a reviewer checks it first.

The sampler holds a frame only while exactly one radar is on, because the
readout names one scale. Two panes each need their own sampler and their own
`activeRamp`, and a pane showing two radars shows no readout.

## 7. Units

`PRODUCTS` gains a unit vocabulary per product, naming the details file and
the image subdirectory for each unit. `product_steps` and `product_image.png`
take an optional unit, validated against that product's own vocabulary rather
than a global list.

The L1 checks keep reading the canonical unit. Thresholds like
`max_freshness_s` were characterized against measured cadence in one unit
subtree, and a display toggle must not move them.

### Stale Unit Subtrees

Shipped on 2026-11-21. Six products carry a second unit, and reading the
mount changed what the slice had to do.

Three of the six stopped writing their millimeter subtree on 2026-09-30
while still writing inches every two minutes. Those stale manifests parse,
list a full 31 steps and look healthy, so only their timestamps give them
away. Switching units there serves seven-week-old rainfall that is
pixel-for-pixel as convincing as the current frames.

The stale-composite banner does not catch it, which follows from the rule
above. That banner reads the backend's L1 verdict for the product, and the
L1 checks read the canonical unit, so they stay green while a stale
alternate unit fills the screen. So the backend reports what the mount held
for each unit, the unit button carries a warning sign before the unit is
chosen, and a separate banner sits over the map after it is chosen.

Temperature has no unit control. Its "Deg" subtree held frames
byte-identical to its "F" ones, so a toggle would move nothing while
implying it had.

Slice 9's second pane inherits this. Each pane needs its own unit, and a
pane on a stale unit needs its own banner, because a single centered
warning cannot say which half of the screen it is about.

## 8. Permalinks

The URL carries product, frame timestamp, active radars, moment, overlays,
pane layout and camera. URL parameters win over stored prefs when present, and
prefs fill in the rest. Updates go through `replaceState`, debounced on a
settled change. Scrubbing therefore does not fill the back button.

Playback position encodes as a timestamp and never as a step index. Indices
shift as the window rolls, which is the same class of bug as the step list
holding a product's frames after the product changed.

Shipped on 2026-11-21. Two details are worth carrying into slice 9.

The link has its own overlay vocabulary rather than `ALL_OVERLAYS`. That
constant names only the four overlays a profile supplies data for and
spells two of them with underscores, so validating against it lost terrain,
3D terrain and NEXRAD. A second pane adds its own toggles to that
vocabulary rather than reusing the first pane's.

The write merges into the existing query rather than replacing it, because
`?diag=probe` turns on the structural probe the visual baseline reads. Any
parameter the console does not write survives.

## 9. Compare Panes

Each pane owns its product, radars, moment and overlays. Both panes read the
same step list at the same index, and their cameras are locked together, so
the only difference on screen is the thing being compared.

The frame barrier covers both panes together. A pane that paints while the
other is still loading would make the comparison itself wrong, which is worse
than a slow frame. Image fetches share the backend LRU, because it keys on the
logical source string rather than on which pane asked.

### Locking By Time

Shipped on 2026-11-21, with one departure from the paragraph above. The panes
are locked by the time they show and not by a shared step index, because
their products can have different cadences and index 12 is then a different
minute in each list. Slice 8 settled that a position is a time, and each pane
resolves the shared time to its own nearest step through the same
`stepForTime`. A pane with no step inside the window stays where it is.

Each pane is a MapView with a pane number. The primary pane owns the stored
prefs, the address bar and the time strip, since a second writer would fight
it for each of them. The strip sits on the page rather than inside the pane:
inside, it took height from that pane alone and left the two maps covering
different ground at one zoom.

The barrier is still per pane. Two panes each hold their own, so a pane can
still paint while the other is loading. Closing that needs the page to gather
both panes' thunks, which is the one piece of this section not yet built.

## 10. Console Controls

Keyboard control for play, step, jump to live and product switching. A time
axis that shows its gaps instead of a bare step slider, which the per-step
activity fractions already make possible. Playback speed. Per-layer opacity in
place of one global slider. The measure tool, export, place search and the
responsive pass move to section 11, since none of them shares anything with
these four.

The keyboard cannot be shared with MapLibre. Its own handler pans with the
arrows whenever the canvas has focus, which it has as soon as anyone clicks
the map to read a value, so a bound arrow would pan and step at once. The
console takes the bare arrows for time, disables that handler, and gives the
shifted arrows and the zoom keys back, so a keyboard user loses nothing.

A focused control owns the keys it handles and no others. Treating any focus
as owning the whole keyboard is the obvious rule, and it is wrong, because
clicking play and then pressing an arrow is ordinary, and a button has never
handled an arrow. Text entry is the exception that does own everything. The
deployed page found this, not the unit tests: every check after the play
button took focus failed.

The listener belongs to the page rather than the pane, for the same reason
the time strip does. Two panes each holding one would both answer a press.
Time keys go to the published strip; the keys that belong to one pane go to
whichever pane the pointer is over.

The time axis places each frame at its own time, and a bar covers its own
interval only up to the normal cadence. Drawn from one frame to the next, a
three-hour hole becomes a three-hour bar at the last frame's activity, which
says the weather held rather than that nothing was published. Whatever is
left over is drawn as a gap, and the strip says how long it was. The cadence
is the median interval, since a mean would be dragged toward the gaps it
exists to find. Anything the times cannot support (a null, a string that is
not a date, an unsorted list) falls back to even spacing and says so, because
a strip that cannot be drawn is worse than one drawn the old way.

Playback speed is four fixed steps rather than a slider. The dwell is a floor
and not a rate: the loop waits for the frame to load and then waits out
whatever of the dwell is left, so above about 2x on six radars the load sets
the interval, and finer control would be a fiction.

Per-layer opacity keeps the values that shipped as its defaults, 0.7 for the
composite, 0.8 for the radars and 0.6 for NEXRAD, so three constants became
three settings without repainting any console. The single slider older builds
saved upgrades onto the radars alone, which is the only layer it ever moved,
and only where no per-layer value exists yet.

## 11. Dashboard

The Site, Radars, Products and Alarms rails all stay, with nothing dropped.
The layout changes because the center six columns the map used to hold are now
free. A small locator map keeps the answer to where a failing radar is, which
is the one thing the split would otherwise take away.

## 12. Profile Geography

Everything in this section shipped on 2026-11-20, across v0.12.1 through
v0.14.1, and it is kept as the record of what was wrong rather than as work
remaining. The composite picker and the NEXRAD extent went out in v0.12.1,
the Stadia domain was registered the same afternoon, and the moment
vocabulary landed in v0.13.0 alongside serving imagery off the published
tree. Only the `stream_gauges` profile gap below is still open, and it stays
latent while `map_overlays` hides the toggle.

Measured against both CSU deployments on 2026-11-20, each running v0.12.0,
read directly over `the access helper` and then in a real browser under `the access helper
--split`. Every layer of the XQPI deployment map is dead, and each one has its own
cause.

The composite never gets selected, and no product overlay loads at all. The
picker renders blank, the scrubber reads "pick a composite to scrub", and the
browser run caught four 404s for `product_steps?product_id=comp_ref` in twenty
seconds. `comp_ref` is AQPI's id, and a stale vocabulary and a bad ordering
combine to keep it selected.
`prefsVocabulary()` builds its composite vocabulary from MapView's own
`COMP_EXTENT` table rather than from the profile's `compExtent`, so `comp_ref`
validates as legal on a deployment that cannot place it. Then `onMount` runs
`if (!placeable(composite)) composite = firstPlaceable()` and follows it with
`applyPrefs(restored)`, which assigns `composite` straight from the restored
blob and overwrites the correction. Fixing the vocabulary alone would not help,
because `pick()` returns `DEFAULT_PREFS.composite` without checking that
default against the vocabulary, and that default is `comp_ref`. The correction
has to move below `applyPrefs`, which is where the overlay drops already sit
for the same reason, under a comment that explains the ordering trap.

The XQPI deployment basemap is blank, and nothing in Sentinel causes it.
`the XQPI deployment.engr.colostate.edu` is not an authorized domain at Stadia, so
vector tiles come back 401 while the style JSON and the glyph ranges both come
back 200. Nine tile requests across zoom 7, 9 and 10 were refused nine times,
and the same nine requests sent as `the AQPI deployment.engr.colostate.edu` all
returned 200. The map therefore loads, applies the style, and draws radar
overlays, markers and range rings over an empty background. No roads, no
coastline and no labels appear. The fix is registering the domain at
client.stadiamaps.com under Properties, Authorized Domains.

Two pieces of AQPI geography sit hardcoded in the map, and both break XQPI.

NEXRAD is pinned to `EXTENT_LARGE`, the Northern California box, at three
places in MapView: the WMS `BBOX`, and the image source coordinates in both
`refreshNexrad` and `prepareNexrad`. XQPI's domain runs -119.564 to -117.0 and
33.288 to 35.027. The two boxes do not touch. `EXTENT_LARGE` has its south
edge at 36.5N against XQPI's north edge at 35.027N. Turning NEXRAD on at
the XQPI deployment therefore fetches a mosaic over Sacramento and paints it 250 km
off-screen. The toggle reads as doing nothing at all.

On AQPI, `EXTENT_LARGE` is already the `comp_ref` extent. Deriving the NEXRAD
box from the profile's own composite extents reproduces AQPI's behavior by
construction and fixes XQPI at the same time, and `/api/version` carries
`comp_extent` already.

The request size does not need the same treatment, which corrects an earlier
reading of this code. MapLibre places the returned PNG by its four corner
coordinates. `WIDTH: 1024, HEIGHT: 1100` therefore sets the resolution the
mosaic is rendered at rather than its position. A box of a different aspect
loses detail along one axis rather than landing in the wrong place. The pixel
size is thus a quality question to measure on its own, outside this fix.

The moment vocabulary is the same class of problem with four copies of the
key. MapView's `MOMENTS` const, `config.MOMENT_TO_PREFIX_DEFAULT`,
`prewarm.ALL_MOMENTS` and `layer2_radar.EXPECTED_ABSENT_MOMENTS` all spell
AQPI's five moments. `profiles/xqpi.py` declares its own four at line 181,
spelling `DifferentialReflectivity` and `DifferentialPhase` without spaces,
and nothing in `config.py` reads that tuple.

The live effect is that FLOW's per-radar overlay never paints anything.
`radar_steps?radar=FLOW&moment=Reflectivity` on the XQPI deployment resolves the prefix
to `CorrReflectivity`, which is AQPI's, and answers `n: 0` with an empty step
list. `xband_scan.png` answers 404 `{"detail":"no scans in window"}` for all
five moments. FLOW is still selectable in the radar list with its moment tabs
live. The controls respond and the overlay stays empty. The vocabulary should
reach the frontend over `/api/version`, the way product labels already do.

The moment problem is larger than a vocabulary mismatch. XQPI has no xband
origin at all: `xband_scan.png` reaches radarca's Django app, `fs_products`
exposes only `read_steps` and `read_image` for product manifests and frames,
and neither has a moment listing. FLOW's moment imagery does exist on the
tree, at 697x693 and 3,920 to 5,971 bytes a frame per the profile's own
measurements, with no route to it. Fixing the spelling would therefore leave
the tabs just as empty. Either the controls are hidden on a profile with no
xband origin, the way the tilt picker already keys off `hasRadarca`, or a
filesystem-backed moment route gets built, which is a feature rather than a
patch.

One more endpoint ignores the profile. `/api/upstream/stream_gauges` on
the XQPI deployment returns 468 Northern California sites, the first of them at 38.96N
119.85W, roughly 300 km from Pasadena. Nothing renders them today because
`map_overlays` is `burn_scars` alone there and the toggle stays hidden. This
gap is latent rather than live. The endpoint should answer per profile regardless.

Two smaller pieces of AQPI vocabulary show on the XQPI page. The map panel's
header is hardcoded to `count="6 radars · 3 NEXRAD"` in `routes/+page.svelte`
on a deployment with one radar, and the moment tabs offer AQPI's five where
FLOW publishes four under different names. The Products rail also renders
Atmospheric Forecast, CoSMoS Data and National Water Model as empty groups
marked "not exposed upstream", which is AQPI's taxonomy on a page that has
none of those families.

The parts that do work on the XQPI deployment, checked the same way: the backend itself
(`product_image.png` for `composite_ref` step 30 returns an 87 KB PNG), the
home view at Pasadena zoom 9.5, the product labels, the status rails, and the
picker's `placeable` filter. The three QPE products each publish 31 steps
while having no entry in `comp_extent`, and the picker correctly leaves all
three out rather than placing them with another product's geometry.

## 13. Slices

One commit per slice, in this order.

1. Route split, shipped. `/` is the console and `/dashboard` the dashboard,
   with its locator map. Joseph reversed the landing page mid-slice, so the
   console is what opens. No change to map behavior.
2. Radar status precedence, shipped. Arrival paints, composite input covers the
   radars arrival cannot see, radarca paints only where no backend row exists,
   and a composite shortfall is reported on the Products rail. 58 cases in
   `validation_tests/js/test_radar_status.mjs`.
3. MapView decomposition, shipped 2026-11-21 across four commits.
   Behavior-preserving: 28 of 28 captured states identical at each
   step. MapView went from 3,399 lines to 2,929.
4. Profile geography, shipped. NEXRAD extent on 2026-11-20 and the moment
   vocabulary on 2026-11-21. The vocabulary half was also a live fix: the
   console carried AQPI's five moment labels as a constant, and three of
   the five returned 400 on the XQPI deployment.
5. Ramps and legend, shipped 2026-11-21, including the coverage
   measurement and what it turned up about the two reflectivity ramps.
6. Cursor readout, shipped 2026-11-21.
7. Units, backend and frontend together, shipped 2026-11-21, with the
   stale-subtree warning the measurement forced.
8. Permalinks, shipped 2026-11-21.
9. Compare panes, shipped 2026-11-21, except the barrier across both.
10. Keyboard, time axis, playback speed, per-layer opacity, shipped
    2026-11-21. The blank-frame stall found in slice 3 was fixed ahead of
    the rest, since it sits in an already-extracted pure module and needed
    none of the pane restructuring: the skip scan wrapped back onto the
    frame it started from and returned it, so playback parked whenever
    every frame was blank and the list was no shorter than the skip bound
    plus one. Seven tilt frames on a clear day are that shape.
11. Measure tool, export, place search, responsive pass.

Slice 3 is the risky one, and it comes before the features. They land in the
new structure rather than being ported into it afterward.

## 14. Verification

Each slice runs `svelte-check` and `code-check` on what it touched. The pure
modules get `.mjs` runners in the existing style, which esbuild-bundles the
real module and asserts against it.

Slice 3 needs more than that, because a behavior-preserving refactor is where
regressions hide. `validation_tests/visual` is the baseline, built before any
code moved. `?diag=probe` publishes `window.__sentinelProbe()`, which reports
the camera, every layer in paint order, the sources this component adds, the
paint and layout of each layer on them, and the component's own control
state. `capture.py` walks 28 prefs states through that probe and `compare.py`
diffs two captures.

Structure rather than pixels, for two reasons. The imagery under the overlays
is live, so a pixel diff an hour apart is mostly noise. And a screenshot
cannot see a layer inserted at the wrong `beforeId`, a paint property
dropped, or a source that stopped being added, which are the faults a
decomposition produces. Screenshots are written beside the JSON for a human.

The harness was measured against itself before being trusted, and that
measurement changed its design three times.

| Found by running it twice | Fix |
|---|---|
| The forecast composite's layer landed on some runs and not others at a flat 9 s wait | Poll until the set of source and layer ids stops changing |
| A fresh `blob:` id for the NEXRAD mosaic on every load | Scrub the object-URL id |
| `Date.now()` baked into the burn-scar paint and filter expressions | Scrub integers in the epoch-millisecond range |
| `watershed-line` landing either side of `burn-scar-line` | Capture each state twice and record the order as unstable, which `compare.py` reports as a warning |
| The tilt overlay's corners moving about 60 m between scans | Name `sources.tilt-overlay.coordinates` as upstream's, since `/api/upstream/tilt_steps` reports the scan's own `center` and `range_km` |

Two consecutive captures of unchanged code agree on all 28 states
structurally, which is the baseline's noise floor. Anything the
decomposition moves shows against it.

The layer-order race is fixed. It survived readiness gating and appeared on
two of the four comparisons run after it, as an order-only warning on
`26-overlays-all` or `06-composite-fcst`. Every vector overlay was added
with `beforeId: 'radar-halo'`, which put each immediately under the radar
markers and left their order among themselves to whichever fetch returned
first. `$lib/map/layers/stack.ts` declares the order and each overlay is
inserted before the first layer above it that is already on the map, so a
layer toggled on late still lands in its declared place. Two captures of
that build agree on all 28 states with no order warnings.

One state in 56 loads has failed to show what it was asked for, the
forecast composite not appearing inside the 30 s cap. The same state then
loaded in 0.5 s on 12 of 12 reloads, and the upstream answered in under
120 ms, so it is neither the network nor a settled fault in the component.
The capture retries a state once and keeps the first error on its record, so
a run finishes while the rate stays visible. The cause is open.

The LB2 change cannot be seen working on the test deployment, whose catalog is L0, L1,
L2, L3 and L4-T1T2 with no backend tier at all. It gets unit tests here plus a
direct check against the AQPI deployment, which carries LB2 for all five X-bands and
CBAND, and the XQPI deployment, whose catalog is L0, LB1, LB2 and LB3 with no L2 at all.
Those two cover both sides of the precedence rule.

## 15. Risks and Open Items

The decomposition regressing the frame barrier or prefs restore. Both have
cost real debugging time before. The slice 3 baseline exists to catch a
repeat.

Ramp values transcribed wrong, which the coverage check is designed to catch
before the readout ships.

CBAND is safe, measured rather than assumed. `/api/checks` on the AQPI deployment lists
`layer2.backend.CBAND` in LB2 with source tag `TR`, currently passing, so
`SENTINEL_SSCB_ROOT` is set and CBAND keeps its color under the new rule.

The three NEXRADs drove the LB3 fallback in section 3. KBBX, KDAX and KMUX are
drawn on the AQPI deployment map with 100 km range rings, and their only readings are
LB3 (`layer3.composite.*` and `layer3.input.*`, all six passing right now).
None of the three has an L2 or an LB2 row. An LB2-only rule with no fallback
would therefore have grayed out a third of the markers on the production map.

Composite-participation faults losing their place in the radar rail, which
slice 2 has to answer rather than inherit.

Compare panes doubling image fetches. The backend heap plateaus around 1.5 GiB
on the test deployment today and `ops/docker-compose.prod.yml` sets no memory limit on
any service, so the limit should land before panes do.

`layer4.xband.FLOW` has no `L4_PROFILES` entry and warned on its first run,
at 0.089% coverage with speckle and frozen both tripping. FLOW's frames are
697x693 where AQPI's X-bands are larger, and the profile already warns
against reusing thresholds across that gap. One sample cannot separate a
calibration gap from a real condition, so it is being watched rather than
tuned. Calibrating it honestly means sampling across quiet and active
weather.

the XQPI deployment's newest composite frame is unreadable on arrival, intermittently.
Three consecutive frames failed the PNG signature check on 2026-11-20 while
frames a few minutes older served 98-104 KB. The producer writes the file
before its bytes land. L4 image QC now runs there and is what would report
it; before v0.14.0 nothing on that deployment opened a frame at all.

Anything wrong with the map on the XQPI deployment beyond the three items in section 12.
All three were reproduced directly over `the access helper`, which reaches both CSU
deployments from this machine, and the browser-level symptoms have not been
checked yet. A browser test needs `the access helper --split`, because the plain
namespace routes only CSU networks and the basemap tiles come from a public
CDN that would fail there for the wrong reason.
