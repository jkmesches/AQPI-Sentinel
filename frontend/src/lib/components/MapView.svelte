<script lang="ts">
	import { onMount, onDestroy, untrack } from 'svelte';
	import { getSite, ALL_OVERLAYS, type HomeView, type Overlay,
	         type Extent, type Product } from '$lib/site';
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import { sentinel } from '$lib/stores/state.svelte';
	import { theme } from '$lib/stores/theme.svelte';
	import TimeControls from '$lib/components/TimeControls.svelte';
	import { url as apiUrl } from '$lib/origin';
	import { productLabel } from '$lib/format';
	import {
		radarStatusByTarget, radarStatusOf, radarVerdicts, radarStageReadings
	} from '$lib/radarStatus';
	import { isAlerting } from '$lib/grouping';
	import { diag } from '$lib/diag';
	import { nexradImageUrl, unionExtent } from '$lib/map/nexrad';
	import {
		burnScarAge, burnScarFilter, newestBurnScar,
		BURN_SCAR_SOURCE, BURN_SCAR_FILL, BURN_SCAR_LINE
	} from '$lib/map/burnScars';
	import { gaugeChartSvg, gaugeSummaryHtml } from '$lib/map/gaugeChart';
	import { selectionBounds, toggleId, uniqueIds } from '$lib/map/selection';
	import { beforeIdFor } from '$lib/map/layers/stack';
	import {
		nextInterestingStep, remainingDwell, stepTo, FRAME_DWELL_MS
	} from '$lib/map/playback';
	import {
		compositeImagePath, extentCorners, scanImagePath, tiltImagePath,
		type ImageCorners
	} from '$lib/map/frames';
	import {
		applyLayers, prepareFrame,
		type FrameApply, type LayerModule
	} from '$lib/map/layers/registry';
	import { reservoirsLayer, watershedsLayer } from '$lib/map/layers/geography';
	import {
		ensureTerrainSource, removeTerrainSourceIfUnused, terrainLayer
	} from '$lib/map/layers/terrain';
	import { burnScarsLayer } from '$lib/map/layers/burnScars';
	import { streamGaugesLayer } from '$lib/map/layers/streamGauges';
	import {
		radarExtent, radarPointsGeo, radarRangesGeo, rangeSpanDeg
	} from '$lib/radarGeo';
	import { buildSharedTimeline } from '$lib/radarTimeline';
	import { loadPrefs, savePrefs, clearPrefs, DEFAULT_PREFS,
	         BURN_SCAR_YEARS_MIN, BURN_SCAR_YEARS_MAX } from '$lib/mapPrefs';
	import { parseStepTs, gaugeSeries } from '$lib/gaugeSeries';
	import type { GaugePoint, GaugeFetch } from '$lib/gaugeSeries';

	interface RadarMeta {
		id: string;
		lat: number;
		lon: number;
		range_m: number;
		kind: string;
		name: string;
		folder: string | null;
		elevations: number[] | null;
	}

	let mapDiv: HTMLDivElement;
	let map: maplibregl.Map | undefined;
	let resizeObs: ResizeObserver | undefined;
	let radars = $state<RadarMeta[]>([]);
	// One-shot — flips to true once the style has loaded for the first time
	// and stays true. Don't use isStyleLoaded(): it flickers to false every
	// time we call setData on a source, which would race our own mutators.
	let styleReady = false;
	// Tracks WebGL context loss — same problem the mobile map handles. On
	// desktop it's rare (typically only the GPU driver crashing) but the
	// same `this.style is undefined` traceback can fire, so we guard the
	// same way.
	let mapDead = $state(false);
	function mapAlive(): boolean {
		return !!(map && !mapDead && (map as any).style);
	}

	// -------------------------------------------------------------------------
	// === Load-bearing perf: image-decode semaphore. ===
	//
	// A Firefox trace caught >900s of CPU in `MOZ_Z_inflate_fast` +
	// `PremultiplyChunk_SSE2` (gzip-decompress + premultiply) during
	// heavy map interaction. Cause: scrubbing/playing/moment-switching
	// kicks off N radar overlays in parallel — each PNG fetch+decode
	// lands on the main thread and stacks up until the tab locks.
	//
	// This semaphore caps concurrent in-flight decodes at
	// MAX_INFLIGHT_DECODES. Pre-decodes with Image().decode() (respects
	// `decoding="async"`) BEFORE calling MapLibre's updateImage(url), so
	// MapLibre's subsequent fetch is a cache hit and the actual layer
	// swap is essentially free. Token-based supersession lets a newer
	// scrub abandon an old preload mid-queue.
	//
	// If you change anything here, profile with 5+ radars active and
	// play running before/after. Removing the throttle reintroduces
	// scrub-related freezes within ~30 seconds of interaction.
	const MAX_INFLIGHT_DECODES = 4;
	let _decodesInFlight = 0;
	const _decodeQueue: (() => void)[] = [];
	function _pumpDecodes() {
		while (_decodesInFlight < MAX_INFLIGHT_DECODES && _decodeQueue.length) {
			const fn = _decodeQueue.shift()!;
			_decodesInFlight++;
			fn();
		}
	}
	async function preloadImage(url: string, isCurrent: () => boolean): Promise<boolean> {
		// Wait for a free slot.
		await new Promise<void>((resolve) => {
			_decodeQueue.push(resolve);
			_pumpDecodes();
		});
		try {
			if (!isCurrent()) return false;
			const img = new Image();
			// === Load-bearing: crossOrigin BEFORE src ===
			//
			// This preload and MapLibre's updateImage() request the SAME url.
			// Without crossOrigin the preload is a no-CORS request and caches
			// an OPAQUE entry; MapLibre then fetches with CORS, reads that
			// entry back, and the browser refuses it — net::ERR_FAILED on
			// every frame. That is exactly what broke the map on 2026-08-27,
			// and it forced these responses to be no-store, which in turn
			// meant both requests hit the network.
			//
			// Requesting with CORS here makes the cached entry reusable by
			// MapLibre, so the pair collapses to one fetch and the responses
			// can be cacheable again. Must be assigned before .src or the
			// browser has already started a no-CORS load.
			img.crossOrigin = 'anonymous';
			img.decoding = 'async';
			img.src = url;
			await img.decode();
			return isCurrent();
		} catch {
			return false;
		} finally {
			_decodesInFlight--;
			_pumpDecodes();
		}
	}

	// Debounce helper for sync functions that get hammered by scrubber drags
	// or rapid button presses.
	function debounce<T extends unknown[]>(fn: (...a: T) => void, ms: number) {
		let h: ReturnType<typeof setTimeout> | undefined;
		const wrapped = (...args: T) => {
			if (h) clearTimeout(h);
			h = setTimeout(() => {
				h = undefined;
				fn(...args);
			}, ms);
		};
		wrapped.flush = () => { if (h) { clearTimeout(h); h = undefined; fn(...([] as unknown as T)); } };
		return wrapped;
	}

	// Composite products available on the map. IDs mirror backend/config.py
	// PRODUCTS table; "none" is the explicit off state. Radar composites
	// approved 2026-05-18; Atmospheric Forecast composites added on user
	// request 2026-05-18.
	type Composite =
		| 'none'
		| 'qpe_15min'
		| 'qpe_1hr'
		| 'precip_rate_radar'
		| 'comp_ref'
		| 'comp_now'
		| 'fcst_total_precip'
		| 'fcst_total_precip_cum'
		| 'fcst_precip_rate'
		| 'fcst_temp'
		| 'water_depth'
		| 'water_level'
		| 'max_water_depth'
		| 'max_water_level';
	// Default to Reflectivity composite — most useful at-a-glance view.
	let composite = $state<Composite>('comp_ref');
	let nexradEnabled = $state(false);
	let overlayOpacity = $state(0.8);

	// Layers panel show/hide — persisted per browser.
	let panelOpen = $state(true);
	const PANEL_KEY = 'sentinel-layers-open';

	// Geographic reference layers — watersheds (HUC-8 outlines) + major
	// flood-relevant reservoirs. Both off by default; persisted across
	// reloads under the same scheme as the panel-open state.
	let watershedsOn = $state(false);
	let reservoirsOn = $state(false);
	let terrainOn = $state(false);
	let terrain3DOn = $state(false);
	let streamGaugesOn = $state(false);
	let burnScarsOn = $state(false);
	// Oldest scar drawn, in years since ignition. Bounded by the constants
	// mapPrefs clamps to, so the slider and the restored value agree.
	let burnScarYears = $state(DEFAULT_PREFS.burnScarYears);
	const WATERSHEDS_KEY = 'sentinel-map-watersheds';
	const RESERVOIRS_KEY = 'sentinel-map-reservoirs';
	const TERRAIN_KEY = 'sentinel-map-terrain';
	const TERRAIN_3D_KEY = 'sentinel-map-terrain-3d';
	const STREAM_GAUGES_KEY = 'sentinel-map-stream-gauges';

	// Lazy-loaded GeoJSON data, fetched the first time a layer is enabled.
	// === Load-bearing: image overlays go BENEATH the radar markers ===
	//
	// The marker layers are built in one pass at style load (range-fill,
	// range-stroke, radar-halo, radar-point, radar-label) so they stack in that
	// order. Image overlays are added and removed dynamically afterwards, which
	// puts them at the TOP of the stack unless a beforeId says otherwise — and
	// an opaque raster over the dots makes the radars unreadable and unclickable,
	// which is the whole interaction on this map.
	//
	// `range-fill` is the lowest marker layer, so inserting before it keeps every
	// marker and range ring above the imagery. The existence check matters: a
	// beforeId that is not on the map makes MapLibre throw, and overlays can be
	// requested before the marker layers exist (theme swap, early scrub). In that
	// window top-of-stack is correct anyway, because the markers are added above.
	const MARKER_BASE_LAYER = 'range-fill';

	function addOverlayBelowMarkers(m: maplibregl.Map, layer: maplibregl.AddLayerObject) {
		m.addLayer(layer, m.getLayer(MARKER_BASE_LAYER) ? MARKER_BASE_LAYER : undefined);
	}

	// Where one vector overlay goes, from the order declared in
	// $lib/map/layers/stack. These all used `beforeId: 'radar-halo'`, which
	// puts each immediately under the markers and leaves their order among
	// themselves to whichever fetch returned first. The visual baseline
	// caught watershed-line landing either side of burn-scar-line across
	// runs of identical code.
	const vectorBefore = (id: string) =>
		beforeIdFor(id, (x) => !!map?.getLayer(x));

	// The overlays that the registry owns.
	//
	// Watersheds and reservoirs shared one `refreshGeography`, and the
	// watershed branch's mid-fetch bail returned out of the whole function,
	// so a theme swap at the wrong moment left the reservoirs unbuilt until
	// something else triggered a pass. A module each ends that.
	const OVERLAY_MODULES: LayerModule[] = [
		watershedsLayer,
		reservoirsLayer,
		terrainLayer(),
		streamGaugesLayer(),
		burnScarsLayer({
			// Read through a getter and a thunk, so a module built once still
			// sees the current profile and slider value on every pass.
			get profile() { return siteProfile; },
			years: () => burnScarYears,
			onLoaded: (through) => { burnScarThrough = through; }
		})
	];

	/** Whether each overlay should be on, by module id. */
	const overlayEnabled = (id: string) =>
		({
			watersheds: watershedsOn, reservoirs: reservoirsOn, terrain: terrainOn,
			streamGauges: streamGaugesOn, burnScars: burnScarsOn
		})[id] ?? false;

	// What failed in the last pass, by module id.
	//
	// The registry returns errors rather than throwing them, and an error
	// with nowhere to go is a swallowed one. The probe publishes this, so a
	// layer that stopped building shows up in validation_tests/visual
	// instead of as a map quietly missing an overlay.
	let overlayErrors = $state<Record<string, string>>({});

	// Invalidates an in-flight pass. A theme swap replaces the style, and a
	// fetch that started before it must not attach to the new one.
	let overlayPass = 0;

	async function applyOverlays(t: 'light' | 'dark'): Promise<void> {
		if (!map || !styleReady) return;
		const m = map;
		const pass = ++overlayPass;
		const alive = () => pass === overlayPass && map === m && styleReady;
		const outcomes = await applyLayers(OVERLAY_MODULES, (mod) => ({
			map: m,
			theme: t,
			enabled: overlayEnabled(mod.id),
			before: vectorBefore,
			alive
		}));
		if (!alive()) return;
		const errors: Record<string, string> = {};
		for (const o of outcomes) {
			if (o.error) errors[o.id] = o.error;
		}
		overlayErrors = errors;
	}

	// Newest ignition in the loaded file, e.g. "Dec 2025". Empty until the
	// data arrives, and the panel hides the line rather than showing a date
	// it does not have.
	let burnScarThrough = $state('');

	/**
	 * Fetch this deployment's burn-scar perimeters.
	 *
	 * The asset is per profile, unlike the watershed and reservoir files in
	 * $lib/map/layers/geography: the NorCal and SoCal perimeter sets are
	 * different data clipped to different boxes. The profile slug is
	 * validated in $lib/site before it reaches this path. Returns an empty
	 * collection on a missing file, so a deployment whose file has not been
	 * generated draws nothing rather than throwing.
	 */
	// Per-radar moment chooser — applies to every active overlay.
	type Moment = 'Reflectivity' | 'Velocity' | 'Differential Reflectivity' | 'PhiDP' | 'RhoHV';
	let currentMoment = $state<Moment>('Reflectivity');
	const MOMENTS: { key: Moment; short: string; full: string }[] = [
		{ key: 'Reflectivity',              short: 'Z',    full: 'Reflectivity' },
		{ key: 'Velocity',                  short: 'V',    full: 'Velocity' },
		{ key: 'Differential Reflectivity', short: 'Zdr',  full: 'Differential Refl' },
		{ key: 'PhiDP',                     short: 'ΦDP',  full: 'Differential Phase' },
		{ key: 'RhoHV',                     short: 'ρhv',  full: 'Correlation Coef' }
	];

	// ---- per-elevation tilt (single-radar inspector, Phase 1) -------------
	// radar-display only carries 3 of our 5 moments. Map the shared ones;
	// Zdr + ΦDP have no tilt source (null → tab disabled while tilt is on).
	const MOMENT_TO_TILT: Record<Moment, string | null> = {
		'Reflectivity': 'reflectivity',
		'Velocity': 'velocity',
		'RhoHV': 'copolarcorrelation',
		'Differential Reflectivity': null,
		'PhiDP': null
	};
	// 0 = off (radarca single-tilt overlay). 1..4 = radar-display elevation
	// folder. Only takes effect when exactly one X-band radar is active.
	let tiltEl = $state(0);
	// Reactive view of the tilt eligibility, consumed by the panel UI.
	const tiltRadarMeta = $derived.by(() => singleTiltRadar());
	const tiltEngaged = $derived(tiltEl >= 1 && !!tiltRadarMeta);

	// time controls — owned by MapView, drive composite overlay frame
	interface Step { i: number; ts: string | null; imageName: string; day: string; date: string; time: string }
	let steps = $state<Step[]>([]);
	let stepIdx = $state(-1);
	/**
	 * Which composite `steps` describes, or '' when it came from somewhere
	 * else (radars, NEXRAD, tilt).
	 *
	 * `composite` changes the instant the picker is used, while the step list
	 * only catches up when loadComposite's fetch returns. Anything reading
	 * both has to know whether they agree yet.
	 */
	let stepsFor = $state('');
	let playing = $state(false);
	let activity = $state<number[]>([]);     // non-empty pixel fraction per step
	// === Stale-composite guard ===
	//
	// The CoSMoS hydro products (water_depth / water_level / max_water_*)
	// froze upstream at a 2026-07-07 forecast run and never resumed. The
	// water_* images have since 404'd, which is self-evident on screen — but
	// max_water_* still serve a perfectly good PNG that is over 50 days old,
	// and the map would happily draw it as if it were current inundation.
	// That is the dangerous case: stale data that looks live.
	//
	// Whether a product counts as stale is L1's question, and `unhealthyProducts`
	// below already carries its answer. The banner used to decide for itself,
	// with one threshold of 3 hours for every product, while the backend holds a
	// per-product `max_freshness_s` spanning 360 s for the composites to 90,000 s
	// for max_water_*. A 250-to-1 range behind a single number is wrong in both
	// directions: max_water_depth raised the banner at 3 h against a 25 h
	// expectation, with L1 reporting pass beside it, and a composite whose feed
	// had died stayed unmarked for three hours against a 6-minute expectation.
	// The second is the failure this guard exists to catch.
	//
	// Taking L1's verdict rather than a copy of its thresholds also keeps the
	// banner and the picker's "not updating" mark from contradicting each other,
	// since both now read the same row. Nothing is drawn when L1 has no row for
	// the product yet, which is the quiet answer rather than a guessed one.
	//
	// The age below stays, as the text the banner shows once it is raised.
	// Forecast products legitimately carry FUTURE steps, so a negative age is
	// normal and is reported as such rather than suppressed.

	// parseStepTs reads the API's unmarked timestamps as UTC rather than as
	// the viewer's local time. See $lib/gaugeSeries for why that matters.
	// The scrubber labels are unaffected: `day`, `date` and `time` come
	// formatted from the backend and are never re-parsed here.
	const compositeAgeH = $derived.by(() => {
		if (composite === 'none' || steps.length === 0) return null;
		// The list can still describe the product that was selected a moment
		// ago, because `composite` changes on click while loadComposite is
		// still fetching. Reporting that one's age under this one's name is
		// how a fresh product got a "not current" banner that a reload
		// cleared, and the slower the upstream the longer it showed.
		if (stepsFor !== composite) return null;
		const ts = steps
			.map((s) => (s.ts ? parseStepTs(s.ts) : NaN))
			.filter((n) => !Number.isNaN(n));
		if (ts.length === 0) return null;
		return (Date.now() - Math.max(...ts)) / 3_600_000;
	});

	function humanAge(h: number): string {
		if (h >= 48) return `${Math.floor(h / 24)} days`;
		if (h >= 2) return `${Math.floor(h)} hours`;
		return `${Math.max(1, Math.round(h * 60))} min`;
	}

	// Products whose L1 check is currently unhealthy — used to mark options in
	// the composite picker so a stale one is visible BEFORE it is selected.
	const unhealthyProducts = $derived.by(() => {
		const out: Record<string, string> = {};
		for (const r of sentinel.rollup?.stages?.L1 ?? []) {
			if (r.status === 'fail' || r.status === 'error') out[r.target] = r.status;
		}
		return out;
	});

	/** Declared after unhealthyProducts, which it reads. See the guard above. */
	const compositeStale = $derived(
		composite !== 'none' && !!unhealthyProducts[composite]
	);

	const stepLabel = $derived(
		stepIdx >= 0 && stepIdx < steps.length
			? `${(steps[stepIdx].day || '').toUpperCase()} ${steps[stepIdx].date || ''}  ${steps[stepIdx].time || ''}`
			: ''
	);

	// Radars currently overlaid on the map. Includes X-bands and CBAND
	// (clickable kinds). NEXRAD radars have no per-radar scan endpoint.
	// Array (not Set) because Svelte 5 $state reliably re-triggers on array
	// re-assignment but is finicky with Set replacement.
	let activeRadars = $state<string[]>([]);

	// The map's home view. Shared by first load and by Reset, so the two
	// cannot drift apart.
	//
	// These are AQPI's tuned values and remain the fallback. A profile that
	// watches a different network supplies its own over /api/version, resolved
	// in onMount BEFORE the map is constructed — XQPI watches one radar in
	// Pasadena, 375 km outside both AQPI extents, so opening here would put
	// its only radar off-screen. See $lib/site.
	const FALLBACK_HOME: HomeView = { center: [-122.6, 37.95], zoom: 7.2 };
	let home = $state<HomeView>(FALLBACK_HOME);
	// Which regional overlays this deployment has data for. AQPI's three
	// geographic ones are all Northern California; a profile without them
	// should not offer a toggle that silently does nothing.
	let overlays = $state<readonly Overlay[]>(ALL_OVERLAYS);
	const hasOverlay = (o: Overlay) => overlays.includes(o);
	// Names the per-profile burn-scar asset. Validated in $lib/site against a
	// slug pattern, so it is safe to interpolate into a path here. Empty
	// until /api/version answers, and stays empty if it never names a
	// profile, which loadBurnScars treats as "draw nothing".
	let siteProfile = $state('');
	/** Can this composite be put on the map at all? */
	function placeable(id: string): boolean {
		if (id === 'none') return true;
		return compExtent ? id in compExtent : id in COMP_EXTENT;
	}
	function firstPlaceable(): Composite {
		if (!compExtent) return 'comp_ref';
		const first = Object.keys(compExtent)[0];
		return (first ?? 'none') as Composite;
	}
	// One box for every composite, when the profile supplies it; otherwise the
	// per-product COMP_EXTENT table below, which is AQPI's and is sourced.
	let compExtent = $state<Record<string, Extent> | null>(null);
	// True when that box is an assumption rather than surveyed corners. Shown
	// on the map: a guessed overlay is pixel-for-pixel as convincing as a
	// sourced one, and a QPE layer off by kilometres attributes rain to the
	// wrong watershed.
	let compExtentProvisional = $state<readonly string[]>([]);
	// What this deployment is. Drives the header, the page title and the tilt
	// control, which were all hard-coded to AQPI.
	let siteName = $state('AQPI Sentinel');
	let dataSource = $state('radarca.engr.colostate.edu');
	/**
	 * What the tilt picker calls the display tier.
	 *
	 * Not `dataSource`, which is a hostname meant for the header. The picker
	 * is offered only where that tier exists, so naming it rather than
	 * printing its address keeps a 28-character string out of an option label
	 * narrow enough to clip it.
	 */
	const RADARCA_LABEL = 'RadarCA';
	let hasRadarca = $state(true);
	// The products this deployment publishes, when its ids are not the ones
	// COMPOSITE_GROUPS below knows. Null keeps that table.
	let siteProducts = $state<Product[] | null>(null);

	// --- persisted map settings -------------------------------------------
	// Restored on mount, saved on change, cleared by Reset. Playback position
	// and `playing` are deliberately excluded: restoring a scrubber to a
	// timestamp that has scrolled out of the window shows stale weather, and
	// auto-playing on arrival is hostile.
	let prefsReady = $state(false);

	function prefsVocabulary() {
		return {
			radars: radars.map((r) => r.id),
			// The profile's own ids when it supplies them, exactly as
			// `placeable` resolves them. Reading COMP_EXTENT here described
			// AQPI on every deployment, so XQPI accepted a stored `comp_ref`
			// as valid and restored a product it cannot place.
			composites: Object.keys(compExtent ?? COMP_EXTENT),
			moments: MOMENTS.map((m) => m.key)
		};
	}

	function applyPrefs(p: ReturnType<typeof loadPrefs>) {
		composite       = p.composite as Composite;
		currentMoment   = p.moment as Moment;
		activeRadars    = [...p.activeRadars];
		nexradEnabled   = p.nexrad;
		overlayOpacity  = p.opacity;
		panelOpen       = p.panelOpen;
		watershedsOn    = p.watersheds;
		reservoirsOn    = p.reservoirs;
		terrainOn       = p.terrain;
		terrain3DOn     = p.terrain3D;
		streamGaugesOn  = p.streamGauges;
		burnScarsOn     = p.burnScars;
		burnScarYears   = p.burnScarYears;
		tiltEl          = p.tiltEl;
		// Camera is deliberately NOT applied here. On mount the map is
		// constructed at the saved camera instead (no visible snap), and Reset
		// moves it explicitly. Calling jumpTo from here would also fire during
		// the pre-radar-list pass, when `map` does not exist yet.
	}

	function currentPrefs() {
		const c = map?.getCenter();
		return {
			composite, moment: currentMoment, activeRadars: [...activeRadars],
			nexrad: nexradEnabled, opacity: overlayOpacity, panelOpen,
			watersheds: watershedsOn, reservoirs: reservoirsOn, terrain: terrainOn,
			terrain3D: terrain3DOn, streamGauges: streamGaugesOn,
			burnScars: burnScarsOn, burnScarYears, tiltEl,
			camera: c && map
				? { lng: c.lng, lat: c.lat, zoom: map.getZoom(),
				    bearing: map.getBearing(), pitch: map.getPitch() }
				: null
		};
	}

	// One writer for the whole blob. Gated on prefsReady so the empty state
	// during mount cannot overwrite what we are still in the middle of
	// restoring — without that gate, opening the page erases your settings.
	$effect(() => {
		void composite; void currentMoment; void activeRadars; void nexradEnabled;
		void overlayOpacity; void panelOpen; void watershedsOn; void reservoirsOn;
		void terrainOn; void terrain3DOn; void streamGaugesOn; void tiltEl;
		void burnScarsOn; void burnScarYears;
		if (!prefsReady) return;
		savePrefs(currentPrefs());
	});

	function resetMapSettings() {
		const d = clearPrefs(prefsVocabulary());
		applyPrefs(d);
		if (map) map.jumpTo({ center: home.center, zoom: home.zoom, bearing: 0, pitch: 0 });
	}
	const isActive = (id: string) => activeRadars.includes(id);

	const STYLE_DARK = 'https://tiles.stadiamaps.com/styles/alidade_smooth_dark.json';
	const STYLE_LIGHT = 'https://tiles.stadiamaps.com/styles/alidade_smooth.json';
	const styleUrl = () => (theme.resolved === 'light' ? STYLE_LIGHT : STYLE_DARK);
	// The projection, the 5-minute snap and the GetMap builder live in
	// $lib/map/nexrad, where a runner can reach them. Both the TIME rule and
	// the Mercator latitude term look right while being wrong, and neither
	// shows in a screenshot.
	const nexradUrlFor = (timeIso: string | null) =>
		nexradImageUrl(nexradExtent, timeIso);

	const EXTENT_LARGE = { west: -124.005, east: -121.195, south: 36.5, north: 39.505 };
	const EXTENT_BAY = { west: -122.6427, east: -121.8509, south: 37.33298, north: 38.34444 };
	// Radar + forecast composites use the regional X-band extent.
	// CoSMoS coastal-storm-modeling products (water_depth, water_level,
	// max_water_*) cover only the Bay and use EXTENT_BAY — same picker,
	// the per-composite extent table here routes the right one through.
	const COMP_EXTENT: Record<Composite, typeof EXTENT_LARGE | null> = {
		none: null,
		qpe_15min:             EXTENT_LARGE,
		qpe_1hr:               EXTENT_LARGE,
		precip_rate_radar:     EXTENT_LARGE,
		comp_ref:              EXTENT_LARGE,
		comp_now:              EXTENT_LARGE,
		fcst_total_precip:     EXTENT_LARGE,
		fcst_total_precip_cum: EXTENT_LARGE,
		fcst_precip_rate:      EXTENT_LARGE,
		fcst_temp:             EXTENT_LARGE,
		water_depth:           EXTENT_BAY,
		water_level:           EXTENT_BAY,
		max_water_depth:       EXTENT_BAY,
		max_water_level:       EXTENT_BAY
	};

	/**
	 * The box the NEXRAD mosaic is requested and placed over.
	 *
	 * This was EXTENT_LARGE on every deployment, which is Northern
	 * California. XQPI's domain is -119.564..-117.0, 33.288..35.027, so the
	 * two boxes do not even touch: EXTENT_LARGE bottoms out at 36.5N against
	 * XQPI's 35.027N. Turning NEXRAD on at the XQPI deployment fetched a mosaic over
	 * Sacramento and placed it 250 km off screen, which reads as a toggle
	 * that does nothing.
	 *
	 * Derived from the profile's own composite boxes rather than configured
	 * separately. On AQPI, EXTENT_LARGE already IS the comp_ref box, so this
	 * reproduces the Northern California request by construction. The union
	 * is taken because a profile may publish several products over different
	 * footprints, and the mosaic should cover all of them.
	 */
	const nexradExtent = $derived(
		unionExtent(Object.values(compExtent ?? {}), EXTENT_LARGE)
	);

	// Theme-keyed status colors. MapLibre paint expressions can't read CSS
	// custom properties, so we keep two parallel tables and pick the right
	// one based on theme.resolved. Keep these in sync with app.css's status
	// tokens for each mode (Issue #4 redesign).
	const VERDICT_DARK: Record<string, string> = {
		pass: '#4ade80', warn: '#fbbf24', fail: '#f87171',
		error: '#f87171', skip: '#8a9389'
	};
	const VERDICT_LIGHT: Record<string, string> = {
		pass: '#16a34a', warn: '#9a6905', fail: '#B91C1C',
		error: '#B91C1C', skip: '#57584C'
	};
	const verdictColor = $derived.by(() => {
		const table = theme.resolved === 'light' ? VERDICT_LIGHT : VERDICT_DARK;
		return (status: string) => table[status] ?? table.skip;
	});

	// Per-status icon split: the center dot conveys the worst-case verdict
	// at a glance, the surrounding halo conveys the staleness mode.
	//   pass        → green / green   (UP)
	//   warn        → red   / yellow  (GHOST UP — data exists but stale)
	//   fail/error  → red   / red     (DOWN)
	//   skip        → muted / muted
	// User spec 2026-05-18: "center icon be red and the inner ring be yellow
	// for ghost states." This makes "is the radar actually broken?" answerable
	// from across the room.
	const radarCenterColor = $derived.by(() => {
		const table = theme.resolved === 'light' ? VERDICT_LIGHT : VERDICT_DARK;
		return (status: string) => {
			if (status === 'pass') return table.pass;
			if (status === 'warn' || status === 'fail' || status === 'error') return table.fail;
			return table.skip;
		};
	});
	const radarHaloColor = $derived.by(() => {
		const table = theme.resolved === 'light' ? VERDICT_LIGHT : VERDICT_DARK;
		return (status: string) => {
			if (status === 'pass') return table.pass;
			if (status === 'warn') return table.warn;
			if (status === 'fail' || status === 'error') return table.fail;
			return table.skip;
		};
	});

	// One verdict per radar. Where the deployment reads the filesystem, LB2
	// (volumes landing on disk) decides and LB3 (composite input) covers the
	// radars LB2 cannot see; radarca paints only where there is no backend row
	// at all. $lib/radarStatus holds the rule and the reasons.
	const radarVerdictMap = $derived(radarVerdicts(sentinel.rollup?.stages));
	const radarStatus = $derived(radarStatusByTarget(sentinel.rollup?.stages));

	// What the panel header counts as on.
	//
	// It counted the composite, NEXRAD and the active radars, and the six
	// geography toggles sat under it uncounted. With every overlay on, the
	// header read "2 on" above a list showing eight. Derived once, because
	// the open panel and its collapsed badge each carried the expression and
	// a count in two places is a count that will disagree with itself.
	const layersOnCount = $derived(
		(composite !== 'none' ? 1 : 0) + (nexradEnabled ? 1 : 0)
		+ activeRadars.length
		+ (watershedsOn ? 1 : 0) + (reservoirsOn ? 1 : 0)
		+ (terrainOn ? 1 : 0) + (terrain3DOn ? 1 : 0)
		+ (streamGaugesOn ? 1 : 0) + (burnScarsOn ? 1 : 0)
	);

	const selectableRadars = $derived(
		radars.filter((r) => r.kind === 'xband' || r.kind === 'cband')
	);

	// Hover tooltip for radar pins — name + scan elevations + range.
	// Created on mouseenter, removed on mouseleave. closeButton off since
	// it's transient. Reuses the theme-aware .maplibregl-popup-content CSS.
	let radarHoverPopup: maplibregl.Popup | null = null;

	/**
	 * Every stage's reading for one radar, with the pin's own row brightened.
	 *
	 * The pin collapses to one color, which is what a map needs and leaves the
	 * operator a question. A radar arriving on time and missing from the
	 * composite is green, and that reading is in the second row here.
	 */
	function radarStatusRowsHtml(r: RadarMeta): string {
		const rows = radarStageReadings(sentinel.rollup?.stages, r.id);
		if (!rows.length) {
			return '<div class="gauge-muted">no stage reports this radar</div>';
		}
		const decided = radarVerdictMap[r.id]?.from ?? 'none';
		const body = rows
			.map((row) => {
				const pin = row.stage === decided ? ' radar-pop-pin' : '';
				return `<span class="radar-pop-row${pin}">`
					+ `<i class="radar-pop-dot" style="background:${verdictColor(row.status)}"></i>`
					+ `<span class="radar-pop-src">${row.subject}</span> ${row.status}</span>`;
			})
			.join('');
		return `<div class="radar-pop-rows">${body}</div>`;
	}

	function radarHoverHtml(r: RadarMeta): string {
		const rangeKm = Math.round(r.range_m / 1000);
		const scans =
			r.elevations && r.elevations.length
				? `<div><span class="gauge-muted">scans:</span> ${r.elevations
						.map((e) => (Number.isInteger(e) ? e.toFixed(1) : String(e)))
						.join(' ')}°</div>`
				: '';
		return `
			<div class="radar-pop">
				<div class="radar-pop-title">${r.name} · ${r.id}</div>
				${radarStatusRowsHtml(r)}
				${scans}
				<div><span class="gauge-muted">range:</span> ${rangeKm} km</div>
			</div>`;
	}
	function showRadarHover(e: maplibregl.MapMouseEvent & { features?: maplibregl.MapGeoJSONFeature[] }) {
		if (!map || !e.features || !e.features.length) return;
		const id = (e.features[0].properties as { id: string }).id;
		const r = radars.find((x) => x.id === id);
		if (!r) return;
		radarHoverPopup?.remove();
		radarHoverPopup = new maplibregl.Popup({
			closeButton: false,
			closeOnClick: false,
			offset: 12,
			className: 'radar-hover-popup'
		})
			.setLngLat([r.lon, r.lat])
			.setHTML(radarHoverHtml(r))
			.addTo(map);
	}
	function hideRadarHover() {
		radarHoverPopup?.remove();
		radarHoverPopup = null;
	}

	// Markers and rings come from $lib/radarGeo, which the dashboard locator
	// has used since it was written. This file kept its own copy of the same
	// geometry, including a second `radarExtent`, and the module's own
	// docstring named that as the duplication to end here.
	//
	// The colors stay on this side. The locator resolves theme differently
	// and neither surface should adopt the other's palette, which is why the
	// module takes a paintFor callback rather than a table.
	const radarPaint = (status: string) => ({
		// The legacy single color, still read by range-fill and range-stroke.
		verdict: verdictColor(status),
		// Split: the dot carries the worst-case verdict, the halo staleness.
		center: radarCenterColor(status),
		halo: radarHaloColor(status)
	});
	const statusForRadar = (id: string) => radarStatusOf(radarStatus, id);

	const pointsGeo = () => radarPointsGeo(radars, statusForRadar, radarPaint, isActive);
	const rangesGeo = () => radarRangesGeo(radars, statusForRadar, radarPaint, isActive);

	function syncBaseSources() {
		if (!map?.getSource('radars')) return;
		(map.getSource('radars') as maplibregl.GeoJSONSource).setData(pointsGeo());
		(map.getSource('ranges') as maplibregl.GeoJSONSource).setData(rangesGeo());
	}

	// Synthesize a step list for NEXRAD-only play. Each step ts feeds the
	// nexradImageUrl TIME param. n0q-t publishes frames on each 5-min mark
	// but has ~5 min of upstream lag — asking for the current 5-min mark
	// returns blank/stale on a fresh frame which manifests as a flash
	// every cycle on playback. We therefore back the latest frame off by
	// `NEXRAD_LAG_MS` before snapping, and the "live" position (stepIdx ==
	// last) renders the latest published frame rather than the in-flight
	// one. Frames are blob-cached in nexradBlobs so the loop is smooth
	// after the first pass.
	const NEXRAD_LAG_MS = 6 * 60_000;
	function synthesizeNexradSteps(): Step[] {
		const out: Step[] = [];
		const now = Date.now();
		// Snap "now - lag" down to the previous 5-min mark to be sure the
		// requested frame already exists upstream.
		const latest = now - NEXRAD_LAG_MS;
		const snapped = latest - (latest % 300_000);
		const COUNT = 18;     // 18 frames × 5 min = 90 min span
		for (let i = COUNT - 1; i >= 0; i--) {
			const d = new Date(snapped - i * 300_000);
			out.push({
				i: COUNT - 1 - i,
				ts: d.toISOString(),
				imageName: '',
				day:  d.toUTCString().slice(0, 3).toUpperCase(),
				date: d.toISOString().slice(0, 10),
				time: d.toISOString().slice(11, 19)
			});
		}
		return out;
	}

	// Pick the playback driver radar: first active radar whose upstream
	// manifest comes back non-empty. Resolves the "selecting All/X-Band
	// doesn't activate playback" report when the alphabetically-first radar
	// happens to be the down one with 0 historical scans. Returns
	// {steps, idx} or null if every active radar is empty.
	async function loadStepsFromAnyActiveRadar(): Promise<{ steps: Step[]; idx: number } | null> {
		// Shared timebase across ALL active radars rather than whichever one
		// answered first. Radars scan on independent cadences and phases, so
		// driving the scrubber from a single radar left every other one
		// permanently snapped to a nearby frame — including at the newest
		// step, where a radar that had just published sat a frame behind.
		// See $lib/radarTimeline for the merge rule.
		const merged = await buildSharedTimeline(activeRadars, currentMoment);
		if (!merged) return null;
		// The activity sparkline is a single trace, so it follows the first
		// active radar; it is indicative rather than per-radar.
		if (activeRadars.length > 0) {
			loadActivity(`radar=${activeRadars[0]}&moment=${encodeURIComponent(currentMoment)}`);
		}
		return { steps: merged.steps as Step[], idx: merged.idx };
	}

	/**
	 * Serialises step loads. Each call takes a ticket and drops its results if
	 * a later call has started, so a slow response for the product the user
	 * has already moved on from cannot overwrite the current one. Switching
	 * products twice in quick succession used to leave whichever request
	 * finished last in charge, which on a slow upstream was the earlier one.
	 */
	let stepsToken = 0;

	/**
	 * Reload the step list on a timer, keeping the operator where they are.
	 *
	 * The list is otherwise built once per selection change and never again,
	 * so an open page's timeline stops at whatever the newest frame was when
	 * it loaded. Measured against the deployed instance: the scrubber read
	 * 22:14 for eight minutes while the API advanced to 22:22, and only a
	 * reload moved it. That is a map quietly showing an older frame the
	 * longer it is left open, which is the one thing it must not do.
	 *
	 * Parked on a step means parked on that TIMESTAMP, not that index: new
	 * frames arrive at the live end and shift the indices under it.
	 */
	async function refreshStepsInPlace(): Promise<void> {
		// Playback and tilt both drive the scrubber themselves; a reload
		// underneath either would fight whoever is holding it.
		if (playing || tiltEngaged) return;
		const wasLive = stepIdx >= steps.length - 1;
		const heldTs = wasLive ? null : (steps[stepIdx]?.ts ?? null);
		await loadComposite({ keepPosition: { wasLive, heldTs } });
	}

	type KeepPosition = { wasLive: boolean; heldTs: string | null };

	async function loadComposite(
		opts: { keepPosition?: KeepPosition } = {}
	): Promise<void> {
		const token = ++stepsToken;
		const keep = opts.keepPosition;
		// A background reload must not stop playback or wipe the activity
		// trace; only a real selection change means those are now wrong.
		if (!keep) {
			stopPlay();
			activity = [];
		}
		// Tilt mode owns the scrubber when engaged — its 7 radar-display
		// frames replace the radarca/composite step list. Highest priority.
		if (tiltEngaged) {
			const r = singleTiltRadar();
			const tiltMoment = MOMENT_TO_TILT[currentMoment];
			if (r && tiltMoment) {
				try {
					const resp = await fetch(
						`/api/upstream/tilt_steps?radar=${r.id}&el=${tiltEl}&moment=${tiltMoment}`
					);
					const j = await resp.json();
					if (token !== stepsToken) return;
					_tiltGeo = { lon: j.center[0], lat: j.center[1], range_km: j.range_km };
					const frames = (j.steps ?? []) as { frame: number; ts: string }[];
					// radar-display frame 0 = newest. Order oldest→newest so the
					// rightmost scrubber position (steps[last]) is the live frame.
					const ordered = [...frames].sort((a, b) => b.frame - a.frame);
					setSteps(ordered.map((f, idx) => tiltStep(f.frame, f.ts, idx)), '');
					stepIdx = steps.length - 1;
				} catch {
					if (token !== stepsToken) return;
					_tiltGeo = null;
					setSteps([], '');
					stepIdx = -1;
				}
			}
			syncAllToStep();
			return;
		}
		_tiltGeo = null;
		if (composite !== 'none') {
			// Captured before the await: `composite` can change while this is
			// in flight, and the list has to be labelled with what it actually
			// describes rather than with whatever is selected when it lands.
			const want = composite;
			try {
				const r = await fetch(`/api/upstream/product_steps?product_id=${want}`);
				const j = await r.json();
				if (token !== stepsToken) return;
				setSteps(j.steps as Step[], want);
				stepIdx = j.current_idx ?? steps.length - 1;
			} catch {
				if (token !== stepsToken) return;
				setSteps([], want);
				stepIdx = -1;
			}
			loadActivity(`product_id=${want}`);
		} else if (activeRadars.length > 0) {
			const found = await loadStepsFromAnyActiveRadar();
			if (token !== stepsToken) return;
			if (found) {
				setSteps(found.steps, '');
				stepIdx = found.idx;
			} else {
				setSteps([], '');
				stepIdx = -1;
			}
		} else if (nexradEnabled) {
			// NEXRAD-only mode: no composite, no radars — synthesize the
			// step list from NEXRAD's 5-min frame cadence so the operator
			// gets a working playback control instead of a dead scrubber.
			setSteps(synthesizeNexradSteps(), '');
			stepIdx = steps.length - 1;
			activity = [];
		} else {
			setSteps([], '');
			stepIdx = -1;
		}
		if (keep) restorePosition(keep);
		syncAllToStep();
	}

	/**
	 * Put the scrubber back where it was after a background reload.
	 *
	 * @param keep Where the operator was before the list was replaced.
	 */
	function restorePosition(keep: KeepPosition): void {
		if (steps.length === 0) {
			stepIdx = -1;
			return;
		}
		// At the live end, stay at the live end: that is a request for "the
		// newest frame", so it should follow the newest frame.
		if (keep.wasLive || !keep.heldTs) {
			stepIdx = steps.length - 1;
			return;
		}
		const same = steps.findIndex((s) => s.ts === keep.heldTs);
		// A held frame that has aged out of the window is gone, so fall back
		// to the oldest frame still available rather than jumping to live,
		// which would look like the map had decided to move on by itself.
		stepIdx = same >= 0 ? same : 0;
	}

	/**
	 * Replace the step list and say which composite it describes.
	 *
	 * Paired so the two cannot drift: anything reading an age off `steps` has
	 * to know whether the list belongs to the product now selected.
	 *
	 * @param next The new step list.
	 * @param forProduct The composite id it describes, or '' for any other
	 *     source (radars, NEXRAD, tilt).
	 */
	function setSteps(next: Step[], forProduct: string): void {
		steps = next;
		stepsFor = forProduct;
	}

	let activityToken = 0;
	async function loadActivity(qs: string) {
		const token = ++activityToken;
		try {
			const r = await fetch(`/api/upstream/activity?${qs}`);
			if (token !== activityToken) return;
			const j = await r.json();
			activity = (j.steps as { activity: number }[]).map((s) => s.activity);
		} catch {
			if (token === activityToken) activity = [];
		}
	}

	/**
	 * A layer's swap, loaded and ready, held until the whole frame is.
	 *
	 * The prepare* functions below return one of these rather than swapping
	 * as soon as their own image decodes. Each layer used to apply the moment
	 * it was ready, so a step appeared on screen in pieces: measured during
	 * playback with six radars, 65% of frames landed in two or more visible
	 * stages up to 232 ms apart, against 30% at one radar. The cadence was
	 * never the problem (3-4% of the intended interval, nothing dropped); the
	 * reassembly inside each frame was.
	 */

	let _compositeToken = 0;
	async function prepareComposite(): Promise<FrameApply | null> {
		if (!map || !styleReady) return null;
		// The profile's box replaces the per-product table wholesale: its
		// product ids are not AQPI's, so a lookup would miss and silently draw
		// nothing. `composite === 'none'` still means no overlay.
		const e = composite === 'none'
			? null
			: (compExtent?.[composite] ?? (compExtent ? null : COMP_EXTENT[composite]));
		if (!e || stepIdx < 0) {
			const m = map;
			return () => {
				if (m.getLayer('comp-overlay-layer')) m.removeLayer('comp-overlay-layer');
				if (m.getSource('comp-overlay')) m.removeSource('comp-overlay');
			};
		}
		const myToken = ++_compositeToken;
		const isCurrent = () => myToken === _compositeToken && !!map && styleReady;
		// === Load-bearing: key the URL on CONTENT, not on Date.now() ===
		//
		// A per-render cache-buster meant every render was a unique URL, so
		// nothing was ever reused: scrubbing back over a frame already viewed
		// re-fetched ~1MB, re-decoded it, and (once responses became
		// cacheable) wrote it to disk cache for nothing. Measured: revisiting
		// five already-seen steps cost five more server hits and dropped the
		// scrub to ~17fps with 1.2s main-thread blocks.
		//
		// The step's timestamp identifies the frame, so revisits are cache
		// hits with no refetch and no re-decode. It is also SAFER than
		// caching on step index alone: when the rolling window advances, a
		// given index maps to a new ts, so the URL changes and we cannot
		// serve a stale frame under a shifted index.
		//
		// _forceBuster (30s-quantized, bumped by pokeOverlays) still forces a
		// genuine refresh of the live frame — same mechanism as xbandScanUrl.
		const url = apiUrl(compositeImagePath({
			product: composite, step: stepIdx,
			ts: steps[stepIdx]?.ts, buster: _forceBuster
		}));
		const coords = extentCorners(e);
		// Preload first so the inflate+premultiply lands off the main path,
		// then ask MapLibre to swap in the cached bytes.
		const ok = await preloadImage(url, isCurrent);
		if (!ok || !isCurrent()) return null;
		return () => {
			if (!map) return;
			const src = map.getSource('comp-overlay') as maplibregl.ImageSource | undefined;
			if (src && typeof src.updateImage === 'function') {
				src.updateImage({ url, coordinates: coords });
			} else {
				if (map.getLayer('comp-overlay-layer')) map.removeLayer('comp-overlay-layer');
				if (map.getSource('comp-overlay')) map.removeSource('comp-overlay');
				map.addSource('comp-overlay', { type: 'image', url, coordinates: coords });
				addOverlayBelowMarkers(map, {
					id: 'comp-overlay-layer',
					type: 'raster',
					source: 'comp-overlay',
					paint: { 'raster-opacity': 0.7 }
				});
			}
		};
	}

	/** Load and apply the composite on its own, for callers outside a frame. */
	async function renderCompositeFrame(): Promise<void> {
		(await prepareComposite())?.();
	}

	function refreshComposite() {
		// kept for back-compat (called from theme swap + initial mount) —
		// just reissues the current step.
		void renderCompositeFrame();
	}

	// ---- time-control actions ---------------------------------------------
	function stopPlay() {
		playing = false;
		// Retire the generation as well: a loop already awaiting a frame
		// checks this before scheduling the next one.
		_playLoopId++;
	}
	// `activity` is the per-step non-empty-pixel fraction returned from
	// /api/upstream/activity. Dead-still steps (activity ≈ 0) carry no info
	// for the user but cost a full image fetch + decode round. In play mode
	// we skip them — capped to avoid jumping past everything when the whole
	// timeline is quiet.
	// The skip rule lives in $lib/map/playback, where a runner reaches it.
	// A short all-blank list parks on the current frame there, which is how
	// it has always behaved; see the module's own note.
	const _nextInterestingStep = (from: number) =>
		nextInterestingStep(from, steps.length, activity);
	/**
	 * How long a frame stays on screen before the next one is requested.
	 *
	 * The interval used to grow by 180 ms per radar, on the reasoning that
	 * more layers need more time. Measurement says otherwise: at one, three
	 * and six radars the displayed cadence held to within 3-4% of its target
	 * and no frame ever ran late, so the penalty was buying nothing and cost
	 * six radars nearly half the playback speed (1.88 s a frame against
	 * 0.98 s at one radar).
	 *
	 * The loop now waits for the frame instead of guessing at it, so this is
	 * a dwell rather than a budget: the real interval is whichever is longer,
	 * this or the time the frame took to load.
	 */

	let _playLoopId = 0;

	/**
	 * Advance the scrubber for as long as playback is on.
	 *
	 * Each pass awaits the whole frame before starting the dwell, so a slow
	 * frame delays the next one rather than being painted over by it. The
	 * previous setInterval fired on the clock whether or not the last frame
	 * had landed.
	 *
	 * @param id This loop's generation, to stop a superseded loop.
	 */
	async function runPlayLoop(id: number): Promise<void> {
		// service loop: exits when `playing` goes false or a newer loop starts
		while (playing && id === _playLoopId) {
			const started = performance.now();
			if (steps.length === 0) return;
			stepIdx = _nextInterestingStep(stepIdx);
			await syncAllToStep();
			if (!playing || id !== _playLoopId) return;
			const left = remainingDwell(started, performance.now(), FRAME_DWELL_MS);
			if (left > 0) {
				await new Promise<void>((resolve) => setTimeout(resolve, left));
			}
		}
	}

	function togglePlay() {
		if (playing) {
			stopPlay();
		} else {
			if (steps.length === 0) return;
			playing = true;
			void runPlayLoop(++_playLoopId);
		}
	}
	let _frameToken = 0;

	/**
	 * A frame that will not finish is not allowed to stall playback. The
	 * deadline only catches a genuinely stuck load, since a warm frame lands
	 * in well under a second and a cold one in a few.
	 */
	const FRAME_DEADLINE_MS = 8000;

	// The image overlays, as frame-barrier participants.
	//
	// They keep their bodies here rather than moving into layer modules,
	// because each reads a dozen pieces of this component's reactive state
	// (the step list, the moment, the active radars, the force-buster) and
	// passing all of it through a context would be the component wearing a
	// different name. What came out instead is the part that is genuinely
	// separable: the frame URLs and the image corners, in $lib/map/frames,
	// where a runner reaches the cache rules.
	//
	// `layers` and `sources` are empty because these never go through
	// applyLayers; they are rebuilt by the barrier, not by a toggle.
	const FRAME_MODULES: LayerModule[] = [
		{
			id: 'composite', layers: [], sources: [],
			build: () => Promise.resolve(),
			prepare: async (): Promise<FrameApply[]> => {
				const apply = await prepareComposite();
				return apply ? [apply] : [];
			}
		},
		{
			id: 'nexrad', layers: [], sources: [],
			build: () => Promise.resolve(),
			prepare: async (): Promise<FrameApply[]> => {
				const apply = await prepareNexrad();
				return apply ? [apply] : [];
			}
		},
		{
			id: 'radars', layers: [], sources: [],
			build: () => Promise.resolve(),
			prepare: () => prepareRadars()
		}
	];

	// What failed in the last frame, by participant id. Published by the
	// probe, so a frame that stopped loading shows in the visual capture.
	let frameErrors = $state<Record<string, string>>({});

	/**
	 * Load every layer for the current step, then swap them together.
	 *
	 * The barrier is the point. Each layer used to swap the moment its own
	 * image decoded, so one step arrived on screen in stages and the map
	 * showed a composite from step N beside a radar from step N-1 for up to
	 * 232 ms. Holding the swaps until all of them are ready costs nothing in
	 * cadence, because the loads already ran concurrently, and it removes the
	 * reassembly an operator was reading as jitter.
	 *
	 * A layer that fails or times out contributes no swap and leaves its own
	 * imagery alone, so one bad frame cannot freeze the others.
	 */
	async function syncAllToStep(): Promise<void> {
		// In tilt mode the scrubber drives the radar-display frames only —
		// composite/NEXRAD/per-radar run on a different (radarca) timebase,
		// so we don't try to align them. refreshTilt renders the frame;
		// when not engaged it tears the overlay down.
		if (tiltEngaged) {
			refreshTilt();
			return;
		}
		if (!map) return;
		const m = map;
		const token = ++_frameToken;
		const alive = () => token === _frameToken && map === m && mapAlive() && styleReady;
		// The three participants go through the registry's barrier, so a
		// failed load is named rather than caught to null. The old shape
		// caught composite and NEXRAD to null and the radars to an empty
		// array, which made a frame that failed to fetch look exactly like a
		// layer with nothing to swap.
		const result = await prepareFrame(FRAME_MODULES, { map: m, alive }, FRAME_DEADLINE_MS);
		if (!alive()) return;
		for (const apply of result.applies) apply();
		frameErrors = result.errors;
		refreshTilt();
	}
	function stepMove(move: 'first' | 'last' | 'prev' | 'next') {
		stopPlay();
		stepIdx = stepTo(stepIdx, steps.length, move);
		syncAllToStep();
	}
	const stepFirst = () => stepMove('first');
	const stepLast = () => stepMove('last');
	const stepPrev = () => stepMove('prev');
	const stepNext = () => stepMove('next');
	// `oninput` on the scrubber fires on every drag pixel — collapse a burst
	// into one sync 80 ms after the user comes to rest. stepIdx still
	// updates on every input so the visual scrubber position tracks live.
	const _syncScrubbed = debounce(syncAllToStep, 80);
	function scrub(i: number) { stopPlay(); stepIdx = i; _syncScrubbed(); }

	// Timestamp the scrubber is currently pointing at. null = "latest" / live.
	// Drives NEXRAD's WMS-T and per-radar xband_scan time-alignment alike.
	const nexradTimeIso = $derived.by(() => {
		if (steps.length === 0 || stepIdx < 0) return null;
		if (stepIdx === steps.length - 1) return null;        // latest = live
		return steps[stepIdx]?.ts ?? null;
	});

	function refreshNexrad() {
		if (!map || !styleReady) return;
		if (map.getLayer('nexrad-layer')) map.removeLayer('nexrad-layer');
		if (map.getSource('nexrad')) map.removeSource('nexrad');
		if (!nexradEnabled) return;
		const e = nexradExtent;
		map.addSource('nexrad', {
			type: 'image',
			url: nexradUrlFor(nexradTimeIso),
			coordinates: [
				[e.west, e.north],
				[e.east, e.north],
				[e.east, e.south],
				[e.west, e.south]
			]
		});
		map.addLayer(
			{
				id: 'nexrad-layer',
				type: 'raster',
				source: 'nexrad',
				paint: { 'raster-opacity': 0.6 }
			},
			'range-fill'
		);
	}

	// Cache of recently-fetched WMS frames as blob URLs, keyed by the request
	// URL. updateImage() pointed at a blob is ~instant (no network roundtrip
	// during the swap), so the previous frame stays visible until the new
	// one is fully decoded → no flicker.
	const nexradBlobs = new Map<string, string>();
	let nexradFetchToken = 0;

	// Prefetch every URL the user might scrub to before play starts. With
	// blobs already in memory, syncNexradTime hits the cache on every tick
	// and the only work left at frame-time is the GL texture swap (~1 frame).
	// Without prefetch the first pass through the loop is bottlenecked on
	// per-frame HTTP latency which the user reads as flicker.
	let _nexradPrefetchInFlight = false;
	async function prefetchNexradFrames() {
		if (_nexradPrefetchInFlight || !nexradEnabled) return;
		_nexradPrefetchInFlight = true;
		try {
			const urls = new Set<string>();
			for (const s of steps) {
				const tsForStep = (steps.length > 0 && s === steps[steps.length - 1]) ? null : s.ts;
				urls.add(nexradUrlFor(tsForStep));
			}
			// Cap concurrency to 3 — Iowa Mesonet has been known to throttle
			// faster than that on a cold cache.
			const todo = [...urls].filter((u) => !nexradBlobs.has(u));
			const CONCURRENCY = 3;
			let i = 0;
			async function worker() {
				while (i < todo.length) {
					const u = todo[i++];
					try {
						const r = await fetch(u);
						if (!r.ok) continue;
						const blob = await r.blob();
						if (!nexradBlobs.has(u)) {
							nexradBlobs.set(u, URL.createObjectURL(blob));
						}
					} catch { /* skip; live syncNexradTime will retry */ }
				}
			}
			await Promise.all(Array.from({ length: CONCURRENCY }, () => worker()));
		} finally {
			_nexradPrefetchInFlight = false;
		}
	}

	// Kick prefetch whenever NEXRAD is enabled with a real step list. Also
	// re-runs after loadComposite refreshes steps. Cheap to call repeatedly
	// — the in-flight guard prevents duplicate work.
	$effect(() => {
		void nexradEnabled;
		void steps.length;
		if (nexradEnabled && steps.length > 0) {
			untrack(() => { prefetchNexradFrames(); });
		}
	});

	async function prepareNexrad(): Promise<FrameApply | null> {
		if (!map || !nexradEnabled) return null;
		const src = map.getSource('nexrad') as maplibregl.ImageSource | undefined;
		if (!src || typeof src.updateImage !== 'function') return null;
		const e = nexradExtent;
		const url = nexradUrlFor(nexradTimeIso);
		const token = ++nexradFetchToken;
		let blobUrl = nexradBlobs.get(url);
		if (!blobUrl) {
			try {
				const r = await fetch(url);
				if (token !== nexradFetchToken) return null;  // superseded
				if (!r.ok) return null;
				const blob = await r.blob();
				blobUrl = URL.createObjectURL(blob);
				nexradBlobs.set(url, blobUrl);
				// keep cache small
				if (nexradBlobs.size > 40) {
					const firstKey = nexradBlobs.keys().next().value;
					if (firstKey !== undefined) {
						const u = nexradBlobs.get(firstKey);
						if (u) URL.revokeObjectURL(u);
						nexradBlobs.delete(firstKey);
					}
				}
			} catch {
				return null;
			}
		}
		if (token !== nexradFetchToken || !blobUrl) return null;
		const ready = blobUrl;
		return () => {
			src.updateImage({
				url: ready,
				coordinates: [
					[e.west, e.north],
					[e.east, e.north],
					[e.east, e.south],
					[e.west, e.south]
				]
			});
		};
	}

	/** Load and apply the NEXRAD frame on its own, outside a frame barrier. */
	async function syncNexradTime(): Promise<void> {
		(await prepareNexrad())?.();
	}

	const radarLayerId = (id: string) => `radar-${id}-layer`;
	const radarSrcId = (id: string) => `radar-${id}-src`;

	function xbandScanUrl(id: string): string {
		// Build a URL whose key params (radar/moment/time) are the dedup key.
		// We deliberately omit a `Date.now()` cache-buster so MapLibre and the
		// browser cache the image — every paint-tick / toggle was previously
		// triggering a fresh proxy fetch because the URL kept changing.
		// `pokeOverlays` increments `_forceBuster` to a 30s-quantized value
		// when it actually wants a refetch; including it as a param means
		// multiple force-refreshes within the same 30s share a cache hit.
		return apiUrl(scanImagePath({
			radar: id, moment: currentMoment,
			time: nexradTimeIso, buster: _forceBuster
		}));
	}

	// Track which radar overlays we've added so refresh doesn't have to walk
	// the entire MapLibre style. Previously this called `map.getStyle().layers`
	// which deep-copies the basemap style on every invocation — very expensive
	// when opacity-slider drags fire ~60 of these per second.
	const _addedOverlays = new Set<string>();
	// Last image URL applied per radar — see syncRadarOverlayTime.
	const _appliedRadarUrl = new Map<string, string>();

	// The single X-band radar eligible for tilt mode right now, or null.
	// Conditions: exactly one radar active, it's an X-band with a known
	// elevation list, and the current moment exists in radar-display.
	function singleTiltRadar(): RadarMeta | null {
		if (activeRadars.length !== 1) return null;
		const r = radars.find((x) => x.id === activeRadars[0]);
		if (!r || r.kind !== 'xband' || !r.elevations || !r.elevations.length) return null;
		return r;
	}
	// The radar whose radarca overlay should be suppressed because the tilt
	// overlay is rendering it instead.
	function tiltOverlayRadarId(): string | null {
		if (tiltEl < 1) return null;
		const r = singleTiltRadar();
		if (!r) return null;
		if (!MOMENT_TO_TILT[currentMoment]) return null;  // moment has no tilt source
		return r.id;
	}

	function refreshRadarOverlays() {
		if (!map || !styleReady) return;
		const active = new Set(activeRadars);
		// When tilt mode owns a radar, drop it from the radarca overlay set
		// so the two don't stack — the tilt overlay handles it instead. The
		// removal branch below then tears down any radarca overlay we'd
		// previously added for it.
		const suppressed = tiltOverlayRadarId();
		if (suppressed) active.delete(suppressed);

		// remove overlays no longer active
		for (const id of [..._addedOverlays]) {
			if (active.has(id)) continue;
			if (map.getLayer(radarLayerId(id))) map.removeLayer(radarLayerId(id));
			if (map.getSource(radarSrcId(id))) map.removeSource(radarSrcId(id));
			_addedOverlays.delete(id);
			_appliedRadarUrl.delete(id);
		}
		// add overlays newly active
		for (const id of active) {
			if (_addedOverlays.has(id)) continue;
			const r = radars.find((x) => x.id === id);
			if (!r) continue;
			const e = radarExtent(r);
			// Record the URL the source is created with, so the first
			// syncRadarOverlayTime after an add doesn't immediately re-fetch
			// the frame the source already loaded.
			const addUrl = xbandScanUrl(id);
			_appliedRadarUrl.set(id, addUrl);
			map.addSource(radarSrcId(id), {
				type: 'image',
				url: addUrl,
				coordinates: [
					[e.west, e.north],
					[e.east, e.north],
					[e.east, e.south],
					[e.west, e.south]
				]
			});
			addOverlayBelowMarkers(map, {
				id: radarLayerId(id),
				type: 'raster',
				source: radarSrcId(id),
				paint: { 'raster-opacity': overlayOpacity }
			});
			_addedOverlays.add(id);
		}
		syncBaseSources();
	}

	// Tilt overlay (Phase 2: scrubbable). When a single X-band radar is
	// active and an elevation is picked, the time strip is driven by the
	// radar-display frame list (built in loadComposite); this fn renders
	// the frame at the current stepIdx. Tears down the overlay when tilt
	// isn't engaged. _tiltGeo (center + range) is captured in
	// loadComposite so a scrub/play tick doesn't re-fetch metadata.
	let _tiltToken = 0;
	let _tiltGeo: { lon: number; lat: number; range_km: number } | null = null;
	// Frame number radar-display should serve for the current step. Tilt
	// steps stash their frame index in `imageName` (frame 0 = newest).
	function currentTiltFrame(): number {
		if (stepIdx < 0 || stepIdx >= steps.length) return 0;
		const f = parseInt(steps[stepIdx]?.imageName ?? '0', 10);
		return Number.isFinite(f) ? f : 0;
	}
	// Build a Step for a tilt frame, parsing the radar_plot timestamp for
	// the scrubber labels. ts is radar-local ("Tue, 19 May 2026 20:16:03").
	function tiltStep(frame: number, ts: string | null, i: number): Step {
		const d = ts ? new Date(ts) : null;
		const ok = d && !Number.isNaN(d.getTime());
		return {
			i,
			ts: ok ? d!.toISOString() : null,
			imageName: String(frame),
			day: ok ? d!.toUTCString().slice(0, 3).toUpperCase() : '',
			date: ok ? d!.toISOString().slice(0, 10) : '',
			time: ok ? d!.toISOString().slice(11, 19) : ''
		};
	}
	async function refreshTilt() {
		if (!map || !styleReady) return;
		const id = tiltOverlayRadarId();
		const tiltMoment = MOMENT_TO_TILT[currentMoment];
		// Not engaged → tear down and bail.
		if (!id || !tiltMoment || !_tiltGeo) {
			if (map.getLayer('tilt-overlay-layer')) map.removeLayer('tilt-overlay-layer');
			if (map.getSource('tilt-overlay')) map.removeSource('tilt-overlay');
			return;
		}
		const myToken = ++_tiltToken;
		const isCurrent = () => myToken === _tiltToken && !!map && styleReady;
		const { lon, lat, range_km: km } = _tiltGeo;
		// The scan's own center and range, as /api/upstream/tilt_steps
		// reports them, so the box follows the elevation rather than the
		// radar's nominal range.
		const { dLat, dLon } = rangeSpanDeg(lat, km * 1000);
		const url = apiUrl(tiltImagePath({
			radar: id, elevation: tiltEl, moment: tiltMoment,
			frame: currentTiltFrame()
		}));
		const coords = extentCorners({
			west: lon - dLon, east: lon + dLon, south: lat - dLat, north: lat + dLat
		});
		const ok = await preloadImage(url, isCurrent);
		if (!ok || !isCurrent()) return;
		const src = map.getSource('tilt-overlay') as maplibregl.ImageSource | undefined;
		if (src && typeof src.updateImage === 'function') {
			src.updateImage({ url, coordinates: coords });
		} else {
			if (map.getLayer('tilt-overlay-layer')) map.removeLayer('tilt-overlay-layer');
			if (map.getSource('tilt-overlay')) map.removeSource('tilt-overlay');
			map.addSource('tilt-overlay', { type: 'image', url, coordinates: coords });
			addOverlayBelowMarkers(map, {
				id: 'tilt-overlay-layer',
				type: 'raster',
				source: 'tilt-overlay',
				paint: { 'raster-opacity': overlayOpacity }
			});
		}
	}

	// Opacity changes are a hot path (slider drag) — keep them off the heavy
	// add/remove path. setPaintProperty is O(1) per layer.
	function applyOpacity() {
		if (!map || !styleReady) return;
		for (const id of _addedOverlays) {
			if (map.getLayer(radarLayerId(id))) {
				map.setPaintProperty(radarLayerId(id), 'raster-opacity', overlayOpacity);
			}
		}
		if (map.getLayer('tilt-overlay-layer')) {
			map.setPaintProperty('tilt-overlay-layer', 'raster-opacity', overlayOpacity);
		}
	}

	let _radarSyncToken = 0;

	/**
	 * Load every active radar's frame, concurrently, and return their swaps.
	 *
	 * The loads run together rather than one after another. Awaiting each
	 * radar in turn left the decode semaphore (4 slots) holding one image at
	 * a time, so six radars cost six serial round trips and the play loop
	 * paid for it with a 180 ms per-radar interval penalty.
	 *
	 * @returns One apply per radar whose frame changed and loaded.
	 */
	async function prepareRadars(): Promise<FrameApply[]> {
		if (!map || !styleReady) return [];
		const myToken = ++_radarSyncToken;
		const isCurrent = () => myToken === _radarSyncToken && !!map && styleReady;
		// Snapshot the targets so a later mutation to activeRadars doesn't
		// race the loop.
		const targets = activeRadars
			.map((id) => {
				const r = radars.find((x) => x.id === id);
				if (!r) return null;
				return { id, url: xbandScanUrl(id), e: radarExtent(r) };
			})
			.filter((t): t is { id: string; url: string; e: { west: number; north: number; east: number; south: number } } => t !== null);

		const prepared = await Promise.all(targets.map(async (t) => {
			// Skip frames already on screen. MapLibre re-fetches and re-decodes
			// on every updateImage even when the URL is identical, and this
			// runs on each step change plus the 120s poke. The URL carries the
			// time and _forceBuster, so a genuine refresh still gets through.
			if (_appliedRadarUrl.get(t.id) === t.url) return null;
			const ok = await preloadImage(t.url, isCurrent);
			if (!ok || !isCurrent()) return null;
			return t;
		}));

		const applies: FrameApply[] = [];
		for (const t of prepared) {
			if (!t || !isCurrent()) continue;
			const src = map.getSource(radarSrcId(t.id)) as maplibregl.ImageSource | undefined;
			if (!src) continue;
			applies.push(() => {
				src.updateImage({
					url: t.url,
					coordinates: [
						[t.e.west, t.e.north],
						[t.e.east, t.e.north],
						[t.e.east, t.e.south],
						[t.e.west, t.e.south]
					]
				});
				_appliedRadarUrl.set(t.id, t.url);
			});
		}
		return applies;
	}

	/** Load and apply the radar overlays on their own, outside a barrier. */
	async function syncRadarOverlayTime(): Promise<void> {
		for (const apply of await prepareRadars()) apply();
	}

	// ---- reactive sync ------------------------------------------------------
	// Mutator helpers (setSelection / toggleRadar / etc.) directly call the
	// corresponding refresh fn. The $effects below cover state changes that
	// don't go through those helpers — composite mode, moment, opacity,
	// NEXRAD toggle, theme. We intentionally don't track `activeRadars` here
	// to avoid double-firing on every click.
	$effect(() => {
		void radarStatus;
		syncBaseSources();
	});
	$effect(() => {
		void composite;
		void activeRadars.length;       // crossing 0↔N flips manifest source
		void currentMoment;
		void nexradEnabled;             // toggling NEXRAD when nothing else is on
		void tiltEl;                    // entering/leaving tilt rebuilds the step list
		// Nothing is loaded until onMount has validated the restored state
		// against what this deployment publishes. `composite` holds the
		// module default until then, and that default is AQPI's `comp_ref`,
		// so an ungated first run asked XQPI for a product it does not have
		// and took a 404 on every page load. prefsReady flips inside the
		// map's own load handler, which is after the site payload and after
		// the correction, so the first real run still happens there.
		void prefsReady;
		if (!prefsReady) return;
		untrack(() => { loadComposite(); });
	});
	$effect(() => {
		void nexradEnabled;
		refreshNexrad();
	});
	$effect(() => {
		void nexradTimeIso;
		syncNexradTime();
	});
	// Opacity slider — cheap setPaintProperty per layer, no source churn.
	$effect(() => {
		void overlayOpacity;
		applyOpacity();
	});
	// Moment switch — swaps every active overlay to the new moment.
	$effect(() => {
		void currentMoment;
		syncRadarOverlayTime();
	});
	// Tilt mode — re-evaluate radarca-overlay suppression when the
	// elevation pick / active-radar set / moment changes. The frame render
	// itself is driven by loadComposite → syncAllToStep (which owns the
	// step list), so we don't call refreshTilt here.
	$effect(() => {
		void tiltEl;
		void currentMoment;
		void activeRadars.length;
		refreshRadarOverlays();
	});
	// Watershed + reservoir toggles. Persist immediately so a reload
	// restores the user's choice, and re-run the geography refresh so the
	// layer appears / disappears without a full re-mount.
	$effect(() => {
		void watershedsOn;
		void reservoirsOn;
		void terrainOn;
		void streamGaugesOn;
		void burnScarsOn;
		void applyOverlays(theme.resolved);
	});
	// 3D terrain mesh toggle — drapes the scene + pitches the camera.
	// Independent from hillshade; the underlying DEM source is shared.
	$effect(() => {
		void terrain3DOn;
		refreshTerrain3D();
	});
	// The age slider only pushes a new filter, so dragging it does not
	// refetch or re-add anything.
	$effect(() => {
		void burnScarYears;
		applyBurnScarFilter();
	});

	// ---- bulk-action helpers ------------------------------------------------
	// Fit the viewport to encompass every currently-active radar's range circle
	// (lon/lat ± range_m). Skips the no-op cases (empty selection, single radar
	// already fully on-screen) since they'd just yank the camera around.
	// User spec 2026-05-18: presets should auto-frame the radars they select
	// so the operator doesn't have to scroll to find them.
	function fitToActiveRadars(opts: { animate?: boolean } = {}) {
		if (!map || !styleReady) return;
		const b = selectionBounds(radars, activeRadars);
		// Null means nothing is selected, and the camera is left where the
		// reader put it. $lib/map/selection holds the fold, because an empty
		// one returns an inverted box and fitBounds obeys it.
		if (!b) return;
		map.fitBounds(
			[[b.west, b.south], [b.east, b.north]],
			{ padding: 60, duration: opts.animate === false ? 0 : 600, maxZoom: 10 }
		);
	}

	function setSelection(ids: string[]) {
		activeRadars = uniqueIds(ids);
		// belt-and-suspenders: call the map updates directly so we don't rely
		// solely on $effect tracking through the array reassignment.
		syncBaseSources();
		refreshRadarOverlays();
		// Auto-frame on every preset/bulk selection. Empty = user just
		// cleared, so leave the camera alone; re-fitting to identical
		// bounds is a visual no-op.
		if (activeRadars.length > 0) fitToActiveRadars();
	}
	function toggleRadar(id: string) {
		activeRadars = toggleId(activeRadars, id);
		syncBaseSources();
		refreshRadarOverlays();
	}
	function soloRadar(id: string) {
		setSelection([id]);
	}
	const selectAll = () => setSelection(selectableRadars.map((r) => r.id));
	const selectNone = () => setSelection([]);
	const selectXband = () =>
		setSelection(selectableRadars.filter((r) => r.kind === 'xband').map((r) => r.id));
	// `isAlerting`, not `!== 'pass'`: the two differ exactly on `skip`, and a
	// radar nothing has reported is unknown rather than down. Selecting those
	// would select the whole fleet on any profile before its first run. The
	// default here used to be `pass`, which failed the other way — on XQPI,
	// where no L2 stage exists, every radar read pass and this selected
	// nothing however broken the fleet was.
	const selectDown = () =>
		setSelection(
			selectableRadars
				.filter((r) => isAlerting(radarStatusOf(radarStatus, r.id)))
				.map((r) => r.id)
		);

	// ---- periodic scan refresh ---------------------------------------------
	let scanRefresh: ReturnType<typeof setInterval>;

	// Background `pokeOverlays` runs every ~2 minutes to pull fresh imagery.
	// The previous implementation called `updateImage` directly for every
	// active radar in parallel, with a `Date.now()` cache-buster on the URL
	// — so each cycle fired N simultaneous unthrottled PNG decodes with
	// guaranteed cache misses. On the Live page with 5+ active radars this
	// was the dominant background-CPU cost and the most likely contributor
	// to the idle-tab wedge.
	//
	// We now route through `syncRadarOverlayTime({ force: true })` which
	// applies the decode-throttle semaphore (≤4 in flight), uses token
	// supersession so a newer state change cancels in-flight preloads,
	// and uses a coarse 30s-quantized cache-buster (`_force_buster()`) so
	// multiple users / multiple poke cycles within the same window share
	// browser-cache hits.
	let _forceBuster = 0;
	function _bumpForceBuster() { _forceBuster = Math.floor(Date.now() / 30_000); }
	async function pokeOverlays() {
		if (!mapAlive() || !styleReady) return;
		try {
			_bumpForceBuster();
			// Extend the timeline before repainting it. Refreshing the imagery
			// alone left the scrubber pinned to the frame list this page
			// loaded with, so the newest frame it could reach only got older.
			await refreshStepsInPlace();
			if (composite !== 'none' && map!.getSource('comp-overlay')) {
				await renderCompositeFrame();
			}
			if (activeRadars.length > 0) {
				await syncRadarOverlayTime();
			}
		} catch {
			// WebGL context dropped between the alive check and the mutation —
			// flip the flag so subsequent ticks bail cleanly.
			mapDead = true;
		}
	}

	// ---- theme swap ---------------------------------------------------------
	$effect(() => {
		const t = theme.resolved;
		if (!map) return;
		styleReady = false;
		map.setStyle(styleUrl());
		map.once('styledata', () => {
			if (!map) return;
			if (map.getSource('ranges')) return;
			// Flip styleReady BEFORE re-adding so that refresh* fns called
			// inside addBaseSourcesAndLayers (composite, nexrad, geography,
			// terrain, stream-gauges) don't early-return on the !styleReady
			// guard — they need to actually add their sources here.
			styleReady = true;
			addBaseSourcesAndLayers(t);
		});
	});

	function addBaseSourcesAndLayers(t: 'light' | 'dark') {
		if (!map) return;
		map.addSource('ranges', { type: 'geojson', data: rangesGeo() });
		map.addLayer({
			id: 'range-fill',
			type: 'fill',
			source: 'ranges',
			paint: { 'fill-color': ['get', 'color'], 'fill-opacity': 0.05 }
		});
		map.addLayer({
			id: 'range-stroke',
			type: 'line',
			source: 'ranges',
			paint: {
				'line-color': ['get', 'color'],
				'line-width': ['case', ['get', 'active'], 1.6, 1],
				'line-opacity': ['case', ['get', 'active'], 0.9, 0.35],
				'line-dasharray': [2, 2]
			}
		});
		map.addSource('radars', { type: 'geojson', data: pointsGeo() });
		// Halo = staleness mode (yellow for ghost-up, red for hard down).
		map.addLayer({
			id: 'radar-halo',
			type: 'circle',
			source: 'radars',
			paint: {
				'circle-radius': ['case', ['get', 'active'], 14, 11],
				'circle-color': ['get', 'halo_color'],
				'circle-opacity': ['case', ['get', 'active'], 0.32, 0.16],
				'circle-stroke-color': ['get', 'halo_color'],
				'circle-stroke-width': ['case', ['get', 'active'], 2, 1.4]
			}
		});
		// Point = worst-case verdict (red for warn AND fail; the halo
		// distinguishes which). Lets a viewer read "is this radar working?"
		// in one glance: red center → it's not delivering reliable data.
		map.addLayer({
			id: 'radar-point',
			type: 'circle',
			source: 'radars',
			paint: {
				'circle-radius': ['case', ['get', 'active'], 5.5, 4],
				'circle-color': ['get', 'center_color'],
				'circle-stroke-color': t === 'light' ? '#ffffff' : '#0c100d',  /* --color-canvas per mode */
				'circle-stroke-width': 1
			}
		});
		map.addLayer({
			id: 'radar-label',
			type: 'symbol',
			source: 'radars',
			layout: {
				'text-field': ['get', 'id'],
				'text-size': ['case', ['get', 'active'], 11, 10],
				'text-offset': [0.9, 0.4],
				'text-anchor': 'left',
				'text-font': ['Stadia Regular']
			},
			paint: {
				'text-color': t === 'light' ? '#1c1f1c' : '#d0d6d0',  /* --color-default per mode */
				'text-halo-color': t === 'light' ? '#ffffff' : '#0c100d',  /* --color-canvas per mode */
				'text-halo-width': 1.2
			}
		});
		refreshComposite();
		refreshNexrad();
		refreshRadarOverlays();
		refreshTilt();
		void applyOverlays(t);
		refreshTerrain3D();
	}

	// Stream gauges — USGS sites parsed from radarca's stream_data.csv,
	// served via /api/upstream/stream_gauges. Two visual tiers:
	//   - B-status (basic): small, faint hollow dot. Site exists upstream
	//     but doesn't expose real-time data.
	//   - R-status (real-time): brighter, larger filled circle. Click to
	//     pull live forecast + observed streamflow into a popup.
	function utcHourString(d: Date): string {
		const yyyy = d.getUTCFullYear();
		const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
		const dd = String(d.getUTCDate()).padStart(2, '0');
		const hh = String(d.getUTCHours()).padStart(2, '0');
		return `${yyyy}${mm}${dd}_${hh}`;
	}

	/**
	 * Observed flow for the 25 hours ending now.
	 *
	 * The hour matters. `get_observed_stream_data` takes a bare date too, and
	 * answers it with the day ending at that date's 00Z, which late in the UTC
	 * day stops up to 24 hours short of the forecast and leaves a hole between
	 * the two series. Given the hour it runs to that hour instead, so the two
	 * overlap the way the upstream's own chart shows them.
	 *
	 * Falls back one hour when the current one is not published yet, which the
	 * upstream reports as a `status: unavailable` body rather than an error.
	 *
	 * @param comid The USGS site id.
	 * @param now The current time.
	 * @returns The reply, or null when neither hour could be fetched.
	 */
	async function fetchObserved(comid: string, now: Date): Promise<GaugeFetch | null> {
		const first = await fetchGaugeData(comid, 'observed', utcHourString(now));
		if (first?.headers && first.headers.length > 1) return first;
		const prev = new Date(now.getTime() - 3_600_000);
		return fetchGaugeData(comid, 'observed', utcHourString(prev));
	}
	async function fetchGaugeData(comid: string, kind: 'forecast' | 'observed', ts: string): Promise<GaugeFetch | null> {
		try {
			const r = await fetch(
				`/api/upstream/stream_data?comid=${encodeURIComponent(comid)}&kind=${kind}&ts=${ts}`
			);
			if (!r.ok) return null;
			return (await r.json()) as GaugeFetch;
		} catch {
			return null;
		}
	}
	// The hydrograph and the popup's number formatting live in
	// $lib/map/gaugeChart. It builds markup rather than mounting a
	// component, because the popup belongs to MapLibre and may be torn
	// down at any moment, and the geometry is testable on its own.

	async function onStreamGaugeClick(e: maplibregl.MapMouseEvent & { features?: maplibregl.MapGeoJSONFeature[] }) {
		if (!e.features || e.features.length === 0 || !map) return;
		const f = e.features[0];
		const props = f.properties as { comid: string; status: string };
		const coords = (f.geometry as GeoJSON.Point).coordinates.slice() as [number, number];
		const isLive = props.status === 'R';
		// Popup body. Colors come from the .maplibregl-popup-content CSS
		// in app.css, which routes through Sentinel's theme tokens — so
		// the popup follows light/dark without inline overrides.
		const liveSlot = isLive
			? `<div data-slot="chart"><span class="gauge-muted">loading flow…</span></div>`
			: `<div class="gauge-muted">basic-status site (no live data)</div>`;
		const html = `
			<div class="gauge-popup">
				<div class="gauge-title">COMID ${props.comid}</div>
				<div class="gauge-muted">${isLive ? 'real-time' : 'basic'} site</div>
				<div class="gauge-rows">${liveSlot}</div>
			</div>`;
		const popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true, maxWidth: '320px' })
			.setLngLat(coords)
			.setHTML(html)
			.addTo(map);
		if (!isLive) return;
		const now = new Date();
		const [fcst, obs] = await Promise.all([
			fetchGaugeData(props.comid, 'forecast', utcHourString(now)),
			fetchObserved(props.comid, now)
		]);
		const el = popup.getElement();
		if (!el) return;
		const slot = el.querySelector('[data-slot="chart"]');
		if (!slot) return;
		const chart = gaugeChartSvg(gaugeSeries(obs), gaugeSeries(fcst));
		// One point, or none, cannot be a line. Fall back to the figures
		// rather than showing an empty frame that looks like a broken chart.
		slot.innerHTML = chart || `
			<div><span class="gauge-muted">forecast:</span> ${gaugeSummaryHtml(fcst)}</div>
			<div><span class="gauge-muted">observed:</span> ${gaugeSummaryHtml(obs)}</div>`;
	}

	// Terrain stack — DEM source from AWS Open Data terrarium-format
	// tiles, consumed by two independent toggles:
	//
	//   - Hillshade (terrainOn): a flat raster layer drawn before
	//     'range-fill', so the relief sits below every overlay. CoSMoS
	//     water-depth composites render ON TOP of the shading and read
	//     as "where would this water actually pool given the topography."
	//   - 3D mesh (terrain3DOn): toggles map.setTerrain() to drape the
	//     scene onto a real elevation mesh + eases the camera to a
	//     pitched view. Off → pitch returns to 0 (top-down). Note that
	//     raster overlays draped on the mesh distort at the edges; this
	//     is an A/B exploration knob, not a default.
	//
	// One source, shared. The tiles and the hillshade layer live in
	// $lib/map/layers/terrain; the mesh stays here, beside the camera
	// pitch it moves with.

	// 3D mesh terrain — drape the whole scene over the DEM.
	const PITCH_3D = 50;

	/**
	 * Make the map's terrain match `terrain3DOn`, without moving the camera.
	 *
	 * The pitch lives in toggleTerrain3D instead, because this runs at load
	 * and on every theme swap as well as on a toggle. Pitching from here
	 * flattened any saved pitch on arrival, and the `moveend` handler then
	 * persisted the flattened camera, so a pitched view could not be kept.
	 */
	function refreshTerrain3D() {
		if (!map || !styleReady) return;
		if (terrain3DOn) {
			ensureTerrainSource(map);
			map.setTerrain({ source: 'terrain-dem', exaggeration: 1.2 });
		} else {
			map.setTerrain(null);
			removeTerrainSourceIfUnused(map);
		}
	}

	/**
	 * Flip the 3D mesh and pitch the camera to match.
	 *
	 * Turning it on tilts to PITCH_3D so the relief reads; turning it off
	 * returns to top-down. A saved camera is left alone on load, so a view
	 * the operator pitched by hand comes back as they left it.
	 */
	function toggleTerrain3D() {
		terrain3DOn = !terrain3DOn;
		map?.easeTo({ pitch: terrain3DOn ? PITCH_3D : 0, duration: 600 });
	}

	// Burn scars — CAL FIRE perimeters, shaded by how long ago each burned.
	//
	// Age drives both the filter and the color, and it is computed in the
	// style expression from ALARM_DATE rather than baked into the file, so
	// the layer stays honest between regenerations. ALARM_DATE is the
	// ignition date in epoch milliseconds; YEAR_ alone would bucket a
	// January fire with the December one eleven months later, which is the
	// difference between inside and outside the debris-flow window.
	// The ids, the age expression and the window filter live in
	// $lib/map/burnScars. The filter is evaluated by the style and never by
	// code a test can step through, and a feature with no ALARM_DATE draws
	// at every slider position unless the `has` guard is there.
	//
	// `Date.now()` is read when the layer is built, so a tab left open for
	// days drifts. Irrelevant against a one-to-five-year scale, and the
	// alternative is re-evaluating every feature on a timer.
	const scarFilter = () => burnScarFilter(burnScarYears);

	/** Push a new slider value onto the live layers without refetching. */
	function applyBurnScarFilter() {
		if (!mapAlive() || !map || !styleReady) return;
		for (const id of [BURN_SCAR_FILL, BURN_SCAR_LINE]) {
			if (map.getLayer(id)) map.setFilter(id, scarFilter());
		}
	}

	// Compute a default bearing so the northernmost X-band/CBAND radar sits
	// at the top-left of the viewport and the southernmost at the bottom-
	// right (rotated diagonal). Asked-for 2026-05-18 ("KSCW top-left, KSCR
	// bottom-right; programmatic"). Returns 0 if we can't find two radars.
	function defaultBearingFromRadars(rs: RadarMeta[]): number {
		const xb = rs.filter((r) => r.kind === 'xband' || r.kind === 'cband');
		if (xb.length < 2) return 0;
		const north = xb.reduce((a, b) => (a.lat > b.lat ? a : b));
		const south = xb.reduce((a, b) => (a.lat < b.lat ? a : b));
		const midLat = ((north.lat + south.lat) / 2) * Math.PI / 180;
		const east = (north.lon - south.lon) * Math.cos(midLat);
		const ang = Math.atan2(east, north.lat - south.lat) * 180 / Math.PI;
		// `ang` is the bearing (deg, clockwise from north) of the
		// south→north vector. We want that vector to align with the screen
		// diagonal pointing top-left (screen angle = 315° = -45°).
		// MapLibre bearing rotates the camera clockwise; setting it to
		// (real_bearing - screen_target) achieves the alignment.
		return ang - (-45);
	}

	// ---- visual baseline probe (?diag=probe) -----------------------------
	//
	// Publishes what the map is actually showing, for the comparison in
	// `validation_tests/visual`. Slice 3 of docs/map-console-plan.md moves
	// this file's layers into modules, and what has to hold fixed is the
	// structure: which sources exist, which layers they feed, in what order,
	// and with what paint. A screenshot cannot see a layer inserted at the
	// wrong `beforeId`, and the imagery under it changes every few minutes
	// regardless, so pixels alone would be all noise and no signal.
	//
	// A snapshot rather than the map itself, so a harness reads this surface
	// and cannot drive it.
	interface ProbeWindow {
		__sentinelProbe?: () => Record<string, unknown> | null;
	}

	/** The fixed sources this component adds. A layer on one of these is ours. */
	const PROBE_SOURCES = [
		'comp-overlay', 'tilt-overlay', 'nexrad', 'radars', 'ranges',
		'stream-gauges', 'watersheds', 'reservoirs', 'terrain-dem', 'burn-scars'
	];

	/** The per-radar overlay sources, which are named from the radar id. */
	const PROBE_RADAR_SRC = /^radar-.+-src$/;

	const probeOwns = (id: string) =>
		PROBE_SOURCES.includes(id) || PROBE_RADAR_SRC.test(id);

	/**
	 * Summarize one source, with any inline GeoJSON reduced to its shape.
	 *
	 * The watershed, reservoir, gauge and burn-scar sources carry their whole
	 * FeatureCollection inline, which is 10 MB across a capture and is data
	 * rather than structure. The feature count and the property keys answer
	 * what a capture needs, that the layer is fed from the right fetch, and
	 * they stay still while a gauge reading moves.
	 */
	function probeSource(source: unknown): unknown {
		const s = source as { type?: string; data?: unknown };
		if (s?.type !== 'geojson' || typeof s.data !== 'object' || s.data === null) {
			return source;
		}
		const fc = s.data as { type?: string; features?: { properties?: object }[] };
		const features = fc.features ?? [];
		return {
			...s,
			data: {
				type: fc.type,
				featureCount: features.length,
				propertyKeys: Object.keys(features[0]?.properties ?? {}).sort()
			}
		};
	}

	function probeSnapshot(): Record<string, unknown> | null {
		if (!map) return null;
		let style: maplibregl.StyleSpecification;
		try {
			style = map.getStyle();
		} catch {
			// Called between a style swap and its load event.
			return null;
		}
		const all = style.layers ?? [];
		const sources: Record<string, unknown> = {};
		for (const [id, s] of Object.entries(style.sources ?? {})) {
			if (probeOwns(id)) sources[id] = probeSource(s);
		}
		const layers: Record<string, unknown> = {};
		for (const l of all) {
			const src = (l as { source?: string }).source;
			if (src && probeOwns(src)) layers[l.id] = l;
		}
		return {
			camera: {
				center: map.getCenter().toArray(),
				zoom: map.getZoom(),
				bearing: map.getBearing(),
				pitch: map.getPitch()
			},
			// Every layer in paint order, ours and the basemap's alike. A
			// layer added at the wrong `beforeId` draws under the thing it
			// should cover, and only the order shows that.
			layerOrder: all.map((l) => l.id),
			sources,
			layers,
			terrain: style.terrain ?? null,
			state: {
				composite, moment: currentMoment, activeRadars: [...activeRadars],
				nexradEnabled, overlayOpacity, tiltEl,
				watershedsOn, reservoirsOn, terrainOn, terrain3DOn,
				streamGaugesOn, burnScarsOn, burnScarYears, burnScarThrough,
				stepIdx, stepCount: steps.length, stepsFor, playing,
				panelOpen, mapDead, prefsReady,
				overlayErrors: { ...overlayErrors },
				frameErrors: { ...frameErrors },
				profile: siteProfile, hasRadarca
			}
		};
	}

	function publishProbe(): void {
		(window as unknown as ProbeWindow).__sentinelProbe = probeSnapshot;
	}

	onMount(async () => {
		// Toggles that need no vocabulary can be applied before the radar list
		// arrives; radars and composite are validated once we know what exists.
		applyPrefs(loadPrefs());
		// Both before the map is constructed: building at the right camera
		// avoids rendering the fallback view first and visibly snapping, the
		// same reason the saved camera is applied at construction below.
		const [meta, site] = await Promise.all([
			fetch('/api/radars/meta').then((r) => r.json()),
			getSite(FALLBACK_HOME)
		]);
		radars = meta;
		home = site.home;
		overlays = site.overlays;
		compExtent = site.compExtent;
		compExtentProvisional = site.compExtentProvisional;
		siteName = site.name;
		dataSource = site.dataSource;
		hasRadarca = site.hasRadarca;
		siteProducts = site.products;
		siteProfile = site.profile;
		// Second pass, now able to drop a decommissioned radar or a renamed
		// product rather than handing a stale id to MapLibre.
		const restored = loadPrefs(prefsVocabulary());
		applyPrefs(restored);
		// A composite this deployment cannot place would otherwise stay
		// selected and render nothing with no explanation.
		//
		// === Load-bearing: this runs AFTER applyPrefs, not before ===
		//
		// Same trap as the overlay drops below. applyPrefs assigns `composite`
		// straight from the restored blob, so correcting it first just hands
		// the value back. On XQPI that left `comp_ref` selected, the picker
		// blank with no matching option, and `product_steps` 404ing in a loop.
		if (!placeable(composite)) composite = firstPlaceable();
		// A persisted toggle for an overlay this deployment does not have would
		// otherwise stay on and fetch a dataset for the wrong region.
		//
		// === Load-bearing: this runs AFTER applyPrefs, not before ===
		//
		// applyPrefs assigns every toggle straight from the restored blob, so
		// dropping them first just hands the value back. The toggle then reads
		// on with its row hidden, which leaves no way to turn it off.
		if (!hasOverlay('watersheds')) watershedsOn = false;
		if (!hasOverlay('reservoirs')) reservoirsOn = false;
		if (!hasOverlay('stream_gauges')) streamGaugesOn = false;
		if (!hasOverlay('burn_scars')) burnScarsOn = false;
		// Build the map AT the saved camera rather than jumping to it after
		// load: a jumpTo here would render the default view first and visibly
		// snap, and it would also fight the fitBounds paths on slow loads.
		const cam = restored.camera;
		map = new maplibregl.Map({
			container: mapDiv,
			style: styleUrl(),
			center: cam ? [cam.lng, cam.lat] : home.center,
			zoom: cam ? cam.zoom : home.zoom,
			pitch: cam ? cam.pitch : 0,
			bearing: cam ? cam.bearing : defaultBearingFromRadars(radars),
			attributionControl: { compact: true },
			// Bound MapLibre's tile cache. Default is undefined → grows
			// effectively unbounded. We don't pan, so a small cap suffices.
			maxTileCacheSize: 32,
			// Stop rendering when not in viewport; the default `false` is
			// fine for foreground but doesn't help backgrounded tabs.
			refreshExpiredTiles: false
		});
		requestAnimationFrame(() => map?.resize());
		resizeObs = new ResizeObserver(() => map?.resize());
		resizeObs.observe(mapDiv);

		// WebGL context loss handler — see mapDead comment above.
		const canvas = map.getCanvas() as HTMLCanvasElement;
		canvas.addEventListener('webglcontextlost', (ev: Event) => {
			ev.preventDefault();
			mapDead = true;
		}, false);

		// Persist the camera after the user stops moving, not during — moveend
		// fires once per gesture, where `move` fires every frame.
		map.on('moveend', () => { if (prefsReady) savePrefs(currentPrefs()); });

		map.on('load', () => {
			if (!map) return;
			// === Load-bearing: styleReady goes up BEFORE the layers go on ===
			//
			// addBaseSourcesAndLayers calls refresh* for composite, nexrad,
			// geography, terrain and stream gauges, and every one of those
			// early-returns on `!styleReady`. With the flag set
			// afterwards, a toggle restored from prefs renders nothing on
			// arrival: the panel reads ON, the map is bare, and the layer
			// appears only once something pokes it, which for a persisted
			// toggle means switching it off and on by hand.
			//
			// The theme-swap path above already orders it this way and says
			// why. This one did not, so a saved watershed or stream-gauge
			// toggle has been silently ignored at load since those layers
			// shipped.
			styleReady = true;
			addBaseSourcesAndLayers(theme.resolved);
			// Only now may the prefs writer run. Before this point the
			// component is still assembling restored state, and an early save
			// would persist a half-built snapshot over the user's real one.
			prefsReady = true;

			// === Load-bearing: paint once the style is ready ===
			//
			// Every render path early-returns on `!styleReady`. loadComposite()
			// is async, so if it finishes BEFORE the map fires 'load' its
			// syncAllToStep() call is silently dropped and no overlay is ever
			// drawn — the map sits empty until something unrelated pokes it
			// (a step change, the 120s refresher, a theme swap).
			//
			// This was latent for months and only started reproducing on
			// 2026-08-27, when memoizing /product_steps took it from ~3s
			// (upstream fetch) to ~1.5ms and loadComposite began winning the
			// race routinely. Making an API fast is exactly the kind of change
			// that converts a rare race into a permanent bug, so re-issue the
			// paint here rather than relying on who finishes first.
			syncAllToStep();
			refreshRadarOverlays();

			map.on('click', 'radar-point', (e) => {
				const f = e.features?.[0];
				if (!f) return;
				const p = f.properties as { id: string; clickable?: boolean };
				if (p.clickable) toggleRadar(p.id);
			});
			map.on('click', 'radar-label', (e) => {
				const f = e.features?.[0];
				if (!f) return;
				const p = f.properties as { id: string; clickable?: boolean };
				if (p.clickable) toggleRadar(p.id);
			});
			map.on('click', 'stream-gauge-marker', onStreamGaugeClick);
			for (const layer of ['radar-point', 'radar-label', 'stream-gauge-marker']) {
				map.on('mouseenter', layer, () => {
					if (map) map.getCanvas().style.cursor = 'pointer';
				});
				map.on('mouseleave', layer, () => {
					if (map) map.getCanvas().style.cursor = '';
				});
			}
			// Radar pin hover tooltip (name / scan elevations / range).
			for (const layer of ['radar-point', 'radar-label']) {
				map.on('mouseenter', layer, showRadarHover);
				map.on('mouseleave', layer, hideRadarHover);
			}
		});

		scanRefresh = setInterval(pokeOverlays, 120_000);
		// Suspend the 120s overlay refresher AND any active play loop when
		// the tab is hidden. Without this, an open-but-backgrounded Sentinel
		// tab keeps decoding fresh PNGs in the background indefinitely,
		// which eventually wedges the tab once memory grows enough.
		visHandler = () => {
			if (document.hidden) {
				if (scanRefresh) { clearInterval(scanRefresh); scanRefresh = undefined as any; }
				// Retire the running loop without clearing `playing`, so the
				// control still reads as playing and resumes on return.
				_playLoopId++;
			} else {
				if (!scanRefresh) scanRefresh = setInterval(pokeOverlays, 120_000);
				if (playing) void runPlayLoop(++_playLoopId);
			}
		};
		document.addEventListener('visibilitychange', visHandler);

		if (diag.probe) publishProbe();
	});

	let visHandler: (() => void) | undefined;
	onDestroy(() => {
		stopPlay();
		if (scanRefresh) clearInterval(scanRefresh);
		resizeObs?.disconnect();
		map?.remove();
		if (visHandler) document.removeEventListener('visibilitychange', visHandler);
		if (diag.probe) delete (window as unknown as ProbeWindow).__sentinelProbe;
	});

	// Composites grouped by upstream source so a 10-item dropdown stays
	// readable. Order matches the Live page's grouped Products section.
	// AQPI's product vocabulary. Kept as the fallback for any deployment that
	// does not rename its products; `compGroups` below filters it.
	const COMPOSITE_GROUPS: { label: string; options: { key: Composite; label: string }[] }[] = [
		{
			label: '',  // top-level "Off" — no group heading
			options: [
				{ key: 'none', label: 'Off' }
			]
		},
		{
			label: 'Radar Data',
			options: [
				{ key: 'qpe_15min',         label: 'Total Precip · 15 min QPE' },
				{ key: 'qpe_1hr',           label: 'Total Precip · 1 h QPE' },
				{ key: 'precip_rate_radar', label: 'Precip Rate' },
				{ key: 'comp_ref',          label: 'Reflectivity' },
				{ key: 'comp_now',          label: 'Reflectivity Nowcast' }
			]
		},
		{
			label: 'Atmospheric Forecast',
			options: [
				{ key: 'fcst_total_precip',     label: 'Total Precip' },
				{ key: 'fcst_total_precip_cum', label: 'Total Precip (cumulative)' },
				{ key: 'fcst_precip_rate',      label: 'Precip Rate' },
				{ key: 'fcst_temp',             label: 'Temperature' }
			]
		},
		{
			// CoSMoS coastal-storm-modeling outputs. Bay extent only —
			// these render in a smaller bounding box than the radar /
			// forecast composites, and the picker handles that via
			// COMP_EXTENT.
			label: 'CoSMoS (Bay)',
			options: [
				{ key: 'water_depth',     label: 'Water Depth' },
				{ key: 'water_level',     label: 'Water Level' },
				{ key: 'max_water_depth', label: 'Max Water Depth' },
				{ key: 'max_water_level', label: 'Max Water Level' }
			]
		}
	];

	// What the picker actually offers.
	//
	// Was a fixed list of AQPI's thirteen products, so XQPI showed CoSMoS
	// water-level layers and Bay Area forecasts it does not publish, while two
	// of its own four ids were absent from the type entirely. Now: the
	// profile's products when it renames them, otherwise the table above —
	// and in both cases only those with an extent, because a product that
	// cannot be placed can only draw nothing or draw wrong.
	const compGroups = $derived.by(() => {
		if (siteProducts) {
			const opts = siteProducts
				.filter((p) => placeable(p.id))
				.map((p) => ({ key: p.id as Composite, label: p.label }));
			return [
				{ label: '', options: [{ key: 'none' as Composite, label: 'Off' }] },
				...(opts.length ? [{ label: 'Radar Data', options: opts }] : [])
			];
		}
		return COMPOSITE_GROUPS
			.map((g) => ({ ...g, options: g.options.filter((o) => placeable(o.key)) }))
			.filter((g) => g.options.length > 0);
	});

	const statusLabel = (s: string) =>
		({ pass: 'UP', fail: 'DOWN', warn: 'WARN', error: 'ERR', skip: '—' })[s] ?? s.toUpperCase();
</script>

<div class="flex h-full min-h-[420px] w-full flex-col">
	<!-- map area -->
	<div class="relative flex-1">
		<div bind:this={mapDiv} class="h-full w-full"></div>

		<!-- Provisional-georeferencing marker. The composite overlay is placed
		     from an ASSUMED extent on this deployment — the publisher supplies
		     no bounds — so the pixels may sit kilometres from where the rain
		     actually fell. A placed overlay is pixel-for-pixel as convincing
		     as a surveyed one, which is exactly why this has to be said on the
		     map rather than only in a comment.
		     Bottom-left, unlike the stale banner: that one is about data an
		     operator must not act on at all, this is about data that is real
		     but whose POSITION is unverified. Non-interactive. -->
		{#if composite !== 'none' && compExtentProvisional.includes(composite)}
			<div class="pointer-events-none absolute bottom-3 left-3 z-20 max-w-[60%]">
				<div class="rounded-sm border border-[var(--color-warn)]/70 bg-[var(--color-canvas)]/90 px-2 py-1 shadow-lg backdrop-blur">
					<div class="num text-[9.5px] uppercase tracking-wider text-[var(--color-warn)]">
						⚠ Overlay position provisional
					</div>
					<div class="num mt-0.5 text-[9.5px] text-[var(--color-muted)]">
						Bounds assumed from the radar's range ring; not supplied by the publisher.
					</div>
				</div>
			</div>
		{/if}
		<!-- Stale-composite banner. Deliberately centerd over the map rather
		     than tucked in a corner: the failure this guards against is an
		     operator reading a 50-day-old inundation frame as current, and a
		     subtle marker would not stop that. Non-interactive so it never
		     blocks map controls. -->
		{#if compositeStale}
			<div class="pointer-events-none absolute left-1/2 top-3 z-20 -translate-x-1/2 max-w-[92%]">
				<div class="rounded-sm border border-[var(--color-warn)] bg-[var(--color-warn)]/[0.14] px-3 py-1.5 text-center shadow-xl backdrop-blur">
					<div class="text-[11px] font-semibold uppercase tracking-wider text-[var(--color-warn)]">
						<!-- The age is omitted while the step list still describes the
						     product selected a moment ago, since it would be that
						     one's age. The warning itself is not withheld for it. -->
						⚠ Not current{compositeAgeH !== null ? ` — ${humanAge(compositeAgeH)} old` : ''}
					</div>
					<div class="mt-0.5 text-[10.5px] num text-[var(--color-default)]">
						{productLabel(composite)} last updated {stepLabel || '—'}. Upstream stopped publishing; this is not live data.
					</div>
				</div>
			</div>
		{/if}

	<!-- LAYERS CONTROL -->
	{#if !panelOpen}
		<!-- Collapsed: a single compact badge -->
		<button
			class="pointer-events-auto absolute right-3 top-3 flex items-center gap-2 rounded-sm border border-[var(--color-border-strong)] bg-[var(--color-surface)]/95 px-2 py-1 text-[10.5px] shadow-xl backdrop-blur transition-colors hover:bg-[var(--color-elevated)]"
			onclick={() => { panelOpen = true; }}
			title="show layers"
		>
			<svg viewBox="0 0 12 12" width="11" height="11" fill="currentColor" class="text-[var(--color-muted)]">
				<polygon points="2,2 10,2 8,5 4,5" />
				<polygon points="3,6 9,6 7,9 5,9" />
				<polygon points="4,10 8,10 7,11 5,11" />
			</svg>
			<span class="label tracking-[0.2em] text-[var(--color-default)]">Layers</span>
			<span class="num text-[9.5px] text-[var(--color-faint)]">
				{layersOnCount} on
			</span>
		</button>
	{:else}
	<div
		class="pointer-events-auto absolute right-3 top-3 flex w-[280px] max-h-[calc(100%-1.5rem)] flex-col overflow-y-auto rounded-sm border border-[var(--color-border-strong)] bg-[var(--color-surface)]/95 text-[11px] shadow-xl backdrop-blur"
	>
		<!-- Header -->
		<div class="flex items-center justify-between border-b border-[var(--color-border)] px-3 py-1.5">
			<div class="flex items-baseline gap-2">
				<span class="label tracking-[0.2em]">Layers</span>
				<span class="num text-[9.5px] text-[var(--color-faint)]">
					{layersOnCount} on
				</span>
			</div>
			<div class="flex items-center gap-2">
				<button
					class="border border-[var(--color-border-strong)] px-1.5 py-0 text-[9.5px] uppercase tracking-wider text-[var(--color-muted)] hover:bg-[var(--color-elevated)] hover:text-[var(--color-bright)]"
					title="reset radars, products, layers and view to defaults"
					aria-label="reset map settings"
					onclick={resetMapSettings}
				>
					reset
				</button>
			<button
				class="inline-flex h-4 w-4 items-center justify-center text-[var(--color-muted)] hover:text-[var(--color-bright)]"
				title="hide panel"
				aria-label="hide layers panel"
				onclick={() => { panelOpen = false; }}
			>
				<svg viewBox="0 0 12 12" width="10" height="10" fill="none" stroke="currentColor" stroke-width="1.5">
					<line x1="2.5" y1="6" x2="9.5" y2="6" />
				</svg>
			</button>
			</div>
		</div>

		<!-- Composite — dropdown picker (5 products + Off) -->
		<div class="px-3 py-2">
			<span class="label">Composite</span>
			<div class="mt-1 flex items-center gap-2">
				<span
					class="inline-block h-2 w-2 rounded-full {composite !== 'none'
						? 'bg-[var(--color-info)]'
						: 'bg-[var(--color-faint)]'}"
					aria-hidden="true"
				></span>
				<select
					class="flex-1 rounded-sm border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11px] num text-[var(--color-bright)] focus:outline-none focus:border-[var(--color-info)]"
					value={composite}
					onchange={(e) => (composite = (e.target as HTMLSelectElement).value as Composite)}
					title="Choose the composite overlay to render on the map"
				>
					{#each compGroups as g}
						{#if g.label}
							<optgroup label={g.label}>
								{#each g.options as opt}
									<!-- Mark products whose L1 check is unhealthy, so a stale
									     composite is visible BEFORE it's selected. -->
									<option value={opt.key}>{opt.label}{unhealthyProducts[opt.key] ? '  ⚠ not updating' : ''}</option>
								{/each}
							</optgroup>
						{:else}
							{#each g.options as opt}
								<option value={opt.key}>{opt.label}{unhealthyProducts[opt.key] ? '  ⚠ not updating' : ''}</option>
							{/each}
						{/if}
					{/each}
				</select>
			</div>
		</div>

		<!-- NEXRAD — single row toggle -->
		<button
			class="flex items-center justify-between border-y border-[var(--color-border)] px-3 py-2 text-left transition-colors hover:bg-[var(--color-elevated)]/40"
			onclick={() => (nexradEnabled = !nexradEnabled)}
		>
			<span class="flex items-center gap-2">
				<span
					class="inline-block h-2 w-2 rounded-full {nexradEnabled
						? 'bg-[var(--color-info)]'
						: 'bg-[var(--color-faint)]'}"
				></span>
				<span class="text-[var(--color-default)]">NEXRAD</span>
			</span>
			<span class="num text-[9.5px] {nexradEnabled ? 'text-[var(--color-info)]' : 'text-[var(--color-faint)]'}">
				{nexradEnabled ? 'ON' : 'OFF'}
			</span>
		</button>

		<!-- Geographic reference — flood-relevant context for radar coverage.
		     Outlines for HUC-8 subbasins and markers for the major NorCal
		     reservoirs (Shasta, Oroville, Folsom, …). Both off by default;
		     persisted so a reload restores the user's choice. -->
		<div class="px-3 pt-2 pb-1">
			<span class="label">Geography</span>
		</div>
		{#if hasOverlay('watersheds')}
		<button
			class="flex items-center justify-between px-3 py-1.5 text-left transition-colors hover:bg-[var(--color-elevated)]/40"
			onclick={() => (watershedsOn = !watershedsOn)}
		>
			<span class="flex items-center gap-2">
				<span
					class="inline-block h-2 w-2 rounded-full {watershedsOn
						? 'bg-[var(--color-info)]'
						: 'bg-[var(--color-faint)]'}"
				></span>
				<span class="text-[var(--color-default)]">Watersheds</span>
				<span class="text-[9.5px] text-[var(--color-faint)]">HUC-8</span>
			</span>
			<span class="num text-[9.5px] {watershedsOn ? 'text-[var(--color-info)]' : 'text-[var(--color-faint)]'}">
				{watershedsOn ? 'ON' : 'OFF'}
			</span>
		</button>
		{/if}
		{#if hasOverlay('reservoirs')}
		<button
			class="flex items-center justify-between px-3 py-1.5 text-left transition-colors hover:bg-[var(--color-elevated)]/40"
			onclick={() => (reservoirsOn = !reservoirsOn)}
		>
			<span class="flex items-center gap-2">
				<span
					class="inline-block h-2 w-2 rounded-full {reservoirsOn
						? 'bg-[var(--color-info)]'
						: 'bg-[var(--color-faint)]'}"
				></span>
				<span class="text-[var(--color-default)]">Reservoirs</span>
				<span class="text-[9.5px] text-[var(--color-faint)]">major dams</span>
			</span>
			<span class="num text-[9.5px] {reservoirsOn ? 'text-[var(--color-info)]' : 'text-[var(--color-faint)]'}">
				{reservoirsOn ? 'ON' : 'OFF'}
			</span>
		</button>
		{/if}
		<button
			class="flex items-center justify-between px-3 py-1.5 text-left transition-colors hover:bg-[var(--color-elevated)]/40"
			onclick={() => (terrainOn = !terrainOn)}
		>
			<span class="flex items-center gap-2">
				<span
					class="inline-block h-2 w-2 rounded-full {terrainOn
						? 'bg-[var(--color-info)]'
						: 'bg-[var(--color-faint)]'}"
				></span>
				<span class="text-[var(--color-default)]">Terrain</span>
				<span class="text-[9.5px] text-[var(--color-faint)]">hillshade</span>
			</span>
			<span class="num text-[9.5px] {terrainOn ? 'text-[var(--color-info)]' : 'text-[var(--color-faint)]'}">
				{terrainOn ? 'ON' : 'OFF'}
			</span>
		</button>
		<button
			class="flex items-center justify-between px-3 py-1.5 text-left transition-colors hover:bg-[var(--color-elevated)]/40"
			onclick={toggleTerrain3D}
			title="3D terrain mesh — pitches the camera and drapes the scene over an elevation mesh. Raster overlays may distort at the edges."
		>
			<span class="flex items-center gap-2">
				<span
					class="inline-block h-2 w-2 rounded-full {terrain3DOn
						? 'bg-[var(--color-info)]'
						: 'bg-[var(--color-faint)]'}"
				></span>
				<span class="text-[var(--color-default)]">3D terrain</span>
				<span class="text-[9.5px] text-[var(--color-faint)]">pitched mesh</span>
			</span>
			<span class="num text-[9.5px] {terrain3DOn ? 'text-[var(--color-info)]' : 'text-[var(--color-faint)]'}">
				{terrain3DOn ? 'ON' : 'OFF'}
			</span>
		</button>
		{#if hasOverlay('stream_gauges')}
		<button
			class="flex items-center justify-between border-b border-[var(--color-border)] px-3 py-1.5 text-left transition-colors hover:bg-[var(--color-elevated)]/40"
			onclick={() => (streamGaugesOn = !streamGaugesOn)}
		>
			<span class="flex items-center gap-2">
				<span
					class="inline-block h-2 w-2 rounded-full {streamGaugesOn
						? 'bg-[var(--color-info)]'
						: 'bg-[var(--color-faint)]'}"
				></span>
				<span class="text-[var(--color-default)]">Stream gauges</span>
				<span class="text-[9.5px] text-[var(--color-faint)]">NWM · USGS sites</span>
			</span>
			<span class="num text-[9.5px] {streamGaugesOn ? 'text-[var(--color-info)]' : 'text-[var(--color-faint)]'}">
				{streamGaugesOn ? 'ON' : 'OFF'}
			</span>
		</button>
		{/if}
		{#if hasOverlay('burn_scars')}
		<!-- Burn scars, with the age window below it. The slider shows only
		     while the layer is on: out of context it reads as a control over
		     something else in the Geography group. -->
		<button
			class="flex items-center justify-between px-3 py-1.5 text-left transition-colors hover:bg-[var(--color-elevated)]/40"
			onclick={() => (burnScarsOn = !burnScarsOn)}
			title="CAL FIRE perimeters, shaded by how long ago each burned. Intense rain over a recent scar is the debris-flow case."
		>
			<span class="flex items-center gap-2">
				<span
					class="inline-block h-2 w-2 rounded-full {burnScarsOn
						? 'bg-[var(--color-info)]'
						: 'bg-[var(--color-faint)]'}"
				></span>
				<span class="text-[var(--color-default)]">Burn scars</span>
				<span class="text-[9.5px] text-[var(--color-faint)]">CAL FIRE</span>
			</span>
			<span class="num text-[9.5px] {burnScarsOn ? 'text-[var(--color-info)]' : 'text-[var(--color-faint)]'}">
				{burnScarsOn ? 'ON' : 'OFF'}
			</span>
		</button>
		{#if burnScarsOn}
		<div class="flex items-center gap-2 px-3 pb-1 pt-0.5">
			<span class="text-[9.5px] text-[var(--color-faint)]">age</span>
			<input
				type="range"
				min={BURN_SCAR_YEARS_MIN}
				max={BURN_SCAR_YEARS_MAX}
				step="1"
				value={burnScarYears}
				oninput={(e) => (burnScarYears = Number((e.target as HTMLInputElement).value))}
				class="flex-1 accent-[var(--color-warn)]"
				title="oldest scar to draw, in years since ignition"
				aria-label="burn scar age window, in years"
			/>
			<span class="num w-[3.2rem] text-right text-[9.5px] text-[var(--color-muted)]">
				≤{burnScarYears} yr
			</span>
		</div>
		{#if burnScarThrough}
			<!-- CAL FIRE publishes a season the following spring, so the year in
			     progress is always missing. Without this line an empty recent
			     window reads as "no fires" rather than as a data lag. -->
			<p class="px-3 pb-2 text-[9px] leading-tight text-[var(--color-faint)]">
				perimeters through {burnScarThrough}
			</p>
		{/if}
		{/if}
		<div class="border-b border-[var(--color-border)]"></div>
		{/if}

		<!-- Radars section header with inline moment tabs -->
		<div class="px-3 pt-2 pb-1">
			<div class="flex items-baseline justify-between">
				<span class="label">Radars</span>
				<span class="num text-[9.5px] text-[var(--color-faint)]">
					{activeRadars.length}/{selectableRadars.length}
				</span>
			</div>
			<!-- moment tabs inline. Selected state uses filled OK-tinted
			     background + bold text — the previous underline+color-only
			     treatment was too subtle to read at a glance (2026-05-18). -->
			<div class="mt-1 flex gap-px rounded-sm border border-[var(--color-border-strong)] overflow-hidden">
				{#each MOMENTS as m}
					{@const unavailable = tiltEngaged && !MOMENT_TO_TILT[m.key]}
					<button
						class="num flex-1 py-1 text-[10.5px] font-medium transition-colors {currentMoment === m.key
							? 'bg-[var(--color-ok)]/20 text-[var(--color-bright)] ring-1 ring-inset ring-[var(--color-ok)]/70'
							: 'text-[var(--color-muted)] hover:bg-[var(--color-elevated)]/50 hover:text-[var(--color-default)]'} {unavailable
							? 'opacity-30 cursor-not-allowed'
							: ''}"
						disabled={unavailable}
						title={unavailable
							? `${m.full} — not available in tilt mode (radar-display has Z / V / ρhv only)`
							: `${m.full} — currently selected: ${m.key === currentMoment ? 'yes' : 'no'}`}
						onclick={() => { if (!unavailable) currentMoment = m.key; }}
					>
						{m.short}
					</button>
				{/each}
			</div>
			<div class="mt-1 text-[10px] text-[var(--color-faint)] num text-center">
				moment: <span class="text-[var(--color-bright)]">{MOMENTS.find((m) => m.key === currentMoment)?.full ?? currentMoment}</span>
			</div>

			<!-- Tilt (elevation) selector — only when exactly one X-band radar
			     is active. "Default" = the display tier's single pre-rendered
			     sweep; 1..4 = radar-display per-elevation PPI (Phase 1: newest
			     frame, no scrubbing).
			     Gated on hasRadarca: BOTH sources here are the CSU display
			     stack — the default option comes from radarca and the numbered
			     ones from radar-display — so on a deployment with neither,
			     every option in this control is dead. It listed FLOW's four
			     swept elevations, none of which it could fetch. -->
			{#if tiltRadarMeta && hasRadarca}
				<div class="mt-2 flex items-center gap-2">
					<span class="num text-[9.5px] text-[var(--color-muted)] uppercase tracking-wider">tilt</span>
					<select
						class="flex-1 rounded-sm border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[10.5px] num text-[var(--color-bright)] focus:outline-none focus:border-[var(--color-info)]"
						value={tiltEl}
						onchange={(e) => {
							tiltEl = Number((e.target as HTMLSelectElement).value);
							if (tiltEl >= 1) {
								// Tilt drives the time strip on radar-display's
								// 14-min/7-frame timebase; the composite runs on
								// radarca's hour-long one. Turn the composite off so
								// the scrubber isn't ambiguous.
								composite = 'none';
								// radar-display lacks Zdr/ΦDP — fall back to Z.
								if (!MOMENT_TO_TILT[currentMoment]) currentMoment = 'Reflectivity';
							}
						}}
						title={`Pick a scan elevation. Default uses ${RADARCA_LABEL}'s single sweep. Drives the time strip on the radar's 7-frame loop.`}
					>
						<option value={0}>Default ({RADARCA_LABEL})</option>
						{#each tiltRadarMeta.elevations ?? [] as ang, i}
							<option value={i + 1}>{ang}° EL</option>
						{/each}
					</select>
				</div>
				{#if tiltEngaged}
					<div class="mt-1 text-[9.5px] text-[var(--color-faint)] num text-center">
						radar-display · {steps.length}-frame loop
					</div>
				{/if}
			{/if}
		</div>

		<!-- Quick filters — text links, not buttons -->
		<div class="flex gap-3 px-3 py-1 text-[9.5px] text-[var(--color-faint)]">
			<button class="hover:text-[var(--color-bright)] uppercase tracking-wider" onclick={selectAll}>all</button>
			<button class="hover:text-[var(--color-bright)] uppercase tracking-wider" onclick={selectXband}>x-band</button>
			<button class="hover:text-[var(--color-bright)] uppercase tracking-wider" onclick={selectDown}>down</button>
			<button class="ml-auto hover:text-[var(--color-fail)] uppercase tracking-wider" onclick={selectNone}>clear</button>
		</div>

		<!-- Per-radar rows — full-row click toggles, solo on hover -->
		<ul class="border-t border-[var(--color-border)]">
			{#each selectableRadars as r}
				{@const st = radarStatusOf(radarStatus, r.id)}
				{@const checked = isActive(r.id)}
				<li
					class="group relative flex items-center gap-2 border-b border-[var(--color-border)]/60 px-3 py-1.5 text-[11px] cursor-pointer transition-colors {checked
						? 'bg-[var(--color-elevated)]/40'
						: 'hover:bg-[var(--color-elevated)]/25'}"
					onclick={() => toggleRadar(r.id)}
					role="button"
					tabindex="0"
					onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && toggleRadar(r.id)}
				>
					<!-- check mark indicator -->
					<span
						class="inline-flex h-3 w-3 items-center justify-center rounded-[2px] border {checked
							? 'border-[var(--color-ok)] bg-[var(--color-ok)]/20 text-[var(--color-ok)]'
							: 'border-[var(--color-border-strong)] text-transparent'}"
					>
						<svg viewBox="0 0 10 10" width="8" height="8" fill="none" stroke="currentColor" stroke-width="2">
							<polyline points="2,5 4,7 8,3" />
						</svg>
					</span>
					<!-- status dot -->
					<span
						class="inline-block h-1.5 w-1.5 rounded-full"
						style="background:{verdictColor(st)}"
					></span>
					<!-- id -->
					<span class="num flex-1 text-[var(--color-bright)] tracking-wide">{r.id}</span>
					<!-- status word -->
					<span
						class="text-[9.5px] uppercase tracking-wider {st === 'pass'
							? 'text-[var(--color-ok)]'
							: st === 'fail'
								? 'text-[var(--color-fail)]'
								: st === 'warn'
									? 'text-[var(--color-warn)]'
									: 'text-[var(--color-muted)]'}"
					>
						{statusLabel(st)}
					</span>
					<!-- solo button (hover-only) -->
					<button
						class="absolute right-2 invisible group-hover:visible rounded-sm border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-[var(--color-default)] hover:text-[var(--color-bright)]"
						title="show only this radar"
						onclick={(e) => { e.stopPropagation(); soloRadar(r.id); }}
					>
						solo
					</button>
				</li>
			{/each}
		</ul>

		<!-- Opacity — slim, no label clutter -->
		<div class="flex items-center gap-2 border-t border-[var(--color-border)] px-3 py-2">
			<svg viewBox="0 0 12 12" width="11" height="11" class="text-[var(--color-muted)]">
				<circle cx="6" cy="6" r="5" fill="none" stroke="currentColor" stroke-width="1.2" />
				<path d="M6 1 A5 5 0 0 1 6 11 Z" fill="currentColor" />
			</svg>
			<input
				type="range"
				min="10"
				max="100"
				step="5"
				value={Math.round(overlayOpacity * 100)}
				oninput={(e) =>
					(overlayOpacity = Number((e.target as HTMLInputElement).value) / 100)}
				class="flex-1 accent-[var(--color-ok)]"
				title="overlay opacity"
			/>
			<span class="num w-[2.4rem] text-right text-[9.5px] text-[var(--color-muted)]">
				{Math.round(overlayOpacity * 100)}%
			</span>
		</div>
	</div>
	{/if}
	</div>

	<!-- time controls strip — below the map -->
	<TimeControls
		stepIdx={stepIdx}
		totalSteps={steps.length}
		stepLabel={stepLabel}
		playing={playing}
		activity={activity}
		onFirst={stepFirst}
		onPrev={stepPrev}
		onTogglePlay={togglePlay}
		onNext={stepNext}
		onLast={stepLast}
		onScrub={scrub}
	/>
</div>
