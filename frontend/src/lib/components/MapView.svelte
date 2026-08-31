<script lang="ts">
	import { onMount, onDestroy, untrack } from 'svelte';
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import { sentinel } from '$lib/stores/state.svelte';
	import { theme } from '$lib/stores/theme.svelte';
	import TimeControls from '$lib/components/TimeControls.svelte';
	import { url as apiUrl } from '$lib/origin';
	import { productLabel } from '$lib/format';
	import { buildSharedTimeline } from '$lib/radarTimeline';

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

	/** MapLibre's ImageSource wants exactly four corners, clockwise from the
	 *  top-left. A plain [number, number][] loses that arity and will not
	 *  typecheck against it. */
	type ImageCorners = [
		[number, number], [number, number], [number, number], [number, number]
	];
	let watershedsData: GeoJSON.FeatureCollection | null = null;
	let reservoirsData: GeoJSON.FeatureCollection | null = null;
	let streamGaugesData: GeoJSON.FeatureCollection | null = null;
	async function loadWatersheds(): Promise<GeoJSON.FeatureCollection> {
		if (watershedsData) return watershedsData;
		const r = await fetch('/data/watersheds-huc8-norcal.geojson');
		const data = (await r.json()) as GeoJSON.FeatureCollection;
		watershedsData = data;
		return data;
	}
	async function loadReservoirs(): Promise<GeoJSON.FeatureCollection> {
		if (reservoirsData) return reservoirsData;
		const r = await fetch('/data/reservoirs-norcal.json');
		const data = (await r.json()) as GeoJSON.FeatureCollection;
		reservoirsData = data;
		return data;
	}
	async function loadStreamGauges(): Promise<GeoJSON.FeatureCollection> {
		if (streamGaugesData) return streamGaugesData;
		const r = await fetch('/api/upstream/stream_gauges');
		const j = (await r.json()) as { sites: { comid: string; lat: number; lon: number; status: string }[] };
		streamGaugesData = {
			type: 'FeatureCollection',
			features: j.sites.map((s) => ({
				type: 'Feature' as const,
				properties: { comid: s.comid, status: s.status },
				geometry: { type: 'Point' as const, coordinates: [s.lon, s.lat] }
			}))
		};
		return streamGaugesData;
	}

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
	let playing = $state(false);
	let playTimer: ReturnType<typeof setInterval> | undefined;
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
	// Derived from the loaded step list rather than a new API: forecast
	// products legitimately carry FUTURE steps (negative age), so only a
	// newest-step that has fallen into the past counts as stale.
	const STALE_AFTER_H = 3;

	const compositeAgeH = $derived.by(() => {
		if (composite === 'none' || steps.length === 0) return null;
		const ts = steps
			.map((s) => (s.ts ? Date.parse(s.ts) : NaN))
			.filter((n) => !Number.isNaN(n));
		if (ts.length === 0) return null;
		return (Date.now() - Math.max(...ts)) / 3_600_000;
	});
	const compositeStale = $derived(compositeAgeH !== null && compositeAgeH > STALE_AFTER_H);

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
	const isActive = (id: string) => activeRadars.includes(id);

	const STYLE_DARK = 'https://tiles.stadiamaps.com/styles/alidade_smooth_dark.json';
	const STYLE_LIGHT = 'https://tiles.stadiamaps.com/styles/alidade_smooth.json';
	const styleUrl = () => (theme.resolved === 'light' ? STYLE_LIGHT : STYLE_DARK);
	// Iowa Mesonet WMS-T endpoint — single-image GetMap requests are smoother
	// than tile fetches when scrubbing (no per-tile blank-out flicker).
	const NEXRAD_WMS = 'https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q-t.cgi';

	function lon2x(lon: number): number {
		return (lon * 20037508.34) / 180;
	}
	function lat2y(lat: number): number {
		const r = (Math.log(Math.tan((90 + lat) * Math.PI / 360)) * 20037508.34) / Math.PI;
		return r;
	}

	function nexradImageUrl(timeIso: string | null): string {
		const e = EXTENT_LARGE;
		const bbox = [lon2x(e.west), lat2y(e.south), lon2x(e.east), lat2y(e.north)].join(',');
		const params = new URLSearchParams({
			SERVICE: 'WMS',
			VERSION: '1.1.1',
			REQUEST: 'GetMap',
			LAYERS: 'nexrad-n0q-wmst',
			STYLES: '',
			FORMAT: 'image/png',
			TRANSPARENT: 'true',
			SRS: 'EPSG:3857',
			WIDTH: '1024',
			HEIGHT: '1100',
			BBOX: bbox
		});
		if (timeIso) params.set('TIME', timeIso);
		return `${NEXRAD_WMS}?${params.toString()}`;
	}

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

	function radarExtent(r: RadarMeta) {
		const km = r.range_m / 1000;
		const dLat = km / 111;
		const dLon = km / (111 * Math.cos((r.lat * Math.PI) / 180));
		return { west: r.lon - dLon, east: r.lon + dLon, south: r.lat - dLat, north: r.lat + dLat };
	}

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

	const radarStatus = $derived.by(() => {
		const out: Record<string, string> = {};
		for (const r of sentinel.rollup?.stages?.L2 ?? []) out[r.target] = r.status;
		return out;
	});

	const selectableRadars = $derived(
		radars.filter((r) => r.kind === 'xband' || r.kind === 'cband')
	);

	// Hover tooltip for radar pins — name + scan elevations + range.
	// Created on mouseenter, removed on mouseleave. closeButton off since
	// it's transient. Reuses the theme-aware .maplibregl-popup-content CSS.
	let radarHoverPopup: maplibregl.Popup | null = null;
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

	function pointsGeo() {
		return {
			type: 'FeatureCollection' as const,
			features: radars.map((r) => {
				const status = radarStatus[r.id] ?? (r.kind === 'nexrad' ? 'skip' : 'pass');
				const active = isActive(r.id);
				return {
					type: 'Feature' as const,
					geometry: { type: 'Point' as const, coordinates: [r.lon, r.lat] },
					properties: {
						id: r.id,
						name: r.name,
						kind: r.kind,
						status,
						active,
						// Legacy single color (still used by range-fill/range-stroke).
						color:        verdictColor(status),
						// Split: dot = worst-case verdict, halo = staleness mode.
						center_color: radarCenterColor(status),
						halo_color:   radarHaloColor(status),
						clickable: r.kind === 'xband' || r.kind === 'cband'
					}
				};
			})
		};
	}

	function rangesGeo() {
		const features = radars.map((r) => {
			const km = r.range_m / 1000;
			const dLat = km / 111;
			const dLon = km / (111 * Math.cos((r.lat * Math.PI) / 180));
			const coords: number[][] = [];
			const N = 64;
			for (let i = 0; i <= N; i++) {
				const a = (i / N) * 2 * Math.PI;
				coords.push([r.lon + dLon * Math.cos(a), r.lat + dLat * Math.sin(a)]);
			}
			const status = radarStatus[r.id] ?? (r.kind === 'nexrad' ? 'skip' : 'pass');
			return {
				type: 'Feature' as const,
				geometry: { type: 'Polygon' as const, coordinates: [coords] },
				properties: { id: r.id, color: verdictColor(status), active: isActive(r.id) }
			};
		});
		return { type: 'FeatureCollection' as const, features };
	}

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

	async function loadComposite() {
		stopPlay();
		activity = [];
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
					_tiltGeo = { lon: j.center[0], lat: j.center[1], range_km: j.range_km };
					const frames = (j.steps ?? []) as { frame: number; ts: string }[];
					// radar-display frame 0 = newest. Order oldest→newest so the
					// rightmost scrubber position (steps[last]) is the live frame.
					const ordered = [...frames].sort((a, b) => b.frame - a.frame);
					steps = ordered.map((f, idx) => tiltStep(f.frame, f.ts, idx));
					stepIdx = steps.length - 1;
				} catch {
					_tiltGeo = null;
					steps = [];
					stepIdx = -1;
				}
			}
			syncAllToStep();
			return;
		}
		_tiltGeo = null;
		if (composite !== 'none') {
			try {
				const r = await fetch(`/api/upstream/product_steps?product_id=${composite}`);
				const j = await r.json();
				steps = j.steps as Step[];
				stepIdx = j.current_idx ?? steps.length - 1;
			} catch {
				steps = [];
				stepIdx = -1;
			}
			loadActivity(`product_id=${composite}`);
		} else if (activeRadars.length > 0) {
			const found = await loadStepsFromAnyActiveRadar();
			if (found) {
				steps = found.steps;
				stepIdx = found.idx;
			} else {
				steps = [];
				stepIdx = -1;
			}
		} else if (nexradEnabled) {
			// NEXRAD-only mode: no composite, no radars — synthesize the
			// step list from NEXRAD's 5-min frame cadence so the operator
			// gets a working playback control instead of a dead scrubber.
			steps = synthesizeNexradSteps();
			stepIdx = steps.length - 1;
			activity = [];
		} else {
			steps = [];
			stepIdx = -1;
		}
		syncAllToStep();
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

	let _compositeToken = 0;
	async function renderCompositeFrame() {
		if (!map || !styleReady) return;
		const e = COMP_EXTENT[composite];
		if (!e || stepIdx < 0) {
			if (map.getLayer('comp-overlay-layer')) map.removeLayer('comp-overlay-layer');
			if (map.getSource('comp-overlay')) map.removeSource('comp-overlay');
			return;
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
		// _forceBuster (30s-quantised, bumped by pokeOverlays) still forces a
		// genuine refresh of the live frame — same mechanism as xbandScanUrl.
		const cq = new URLSearchParams({ product_id: composite, step: String(stepIdx) });
		const cts = steps[stepIdx]?.ts;
		if (cts) cq.set('ts', cts);
		if (_forceBuster) cq.set('_t', String(_forceBuster));
		const url = apiUrl(`/api/upstream/product_image.png?${cq.toString()}`);
		const coords: ImageCorners = [
			[e.west, e.north],
			[e.east, e.north],
			[e.east, e.south],
			[e.west, e.south]
		];
		// Preload first so the inflate+premultiply lands off the main path,
		// then ask MapLibre to swap in the cached bytes.
		const ok = await preloadImage(url, isCurrent);
		if (!ok || !isCurrent()) return;
		const src = map!.getSource('comp-overlay') as maplibregl.ImageSource | undefined;
		if (src && typeof src.updateImage === 'function') {
			src.updateImage({ url, coordinates: coords });
		} else {
			if (map!.getLayer('comp-overlay-layer')) map!.removeLayer('comp-overlay-layer');
			if (map!.getSource('comp-overlay')) map!.removeSource('comp-overlay');
			map!.addSource('comp-overlay', { type: 'image', url, coordinates: coords });
			addOverlayBelowMarkers(map!, {
				id: 'comp-overlay-layer',
				type: 'raster',
				source: 'comp-overlay',
				paint: { 'raster-opacity': 0.7 }
			});
		}
	}

	function refreshComposite() {
		// kept for back-compat (called from theme swap + initial mount) —
		// just reissues the current step.
		renderCompositeFrame();
	}

	// ---- time-control actions ---------------------------------------------
	function stopPlay() {
		playing = false;
		if (playTimer) { clearInterval(playTimer); playTimer = undefined; }
	}
	// `activity` is the per-step non-empty-pixel fraction returned from
	// /api/upstream/activity. Dead-still steps (activity ≈ 0) carry no info
	// for the user but cost a full image fetch + decode round. In play mode
	// we skip them — capped to avoid jumping past everything when the whole
	// timeline is quiet.
	const ACTIVITY_FLOOR = 0.005;     // <0.5% of pixels = blank
	const MAX_SKIP_PER_TICK = 6;
	function _nextInterestingStep(from: number): number {
		const n = steps.length;
		if (n === 0) return 0;
		let i = (from + 1) % n;
		let skipped = 0;
		// Only skip if we *have* activity data; otherwise step linearly.
		while (
			skipped < MAX_SKIP_PER_TICK &&
			activity.length === n &&
			activity[i] !== undefined &&
			activity[i] < ACTIVITY_FLOOR &&
			i !== from
		) {
			i = (i + 1) % n;
			skipped++;
		}
		return i;
	}
	function tick() {
		if (steps.length === 0) return;
		stepIdx = _nextInterestingStep(stepIdx);
		syncAllToStep();
	}
	// Per-frame load count under typical settings: 1 composite + N radars +
	// 1 NEXRAD ≈ 1+N+1 image decodes per tick. Keep the average load below
	// ~5 images per second so the main thread never falls behind.
	function _playIntervalMs(): number {
		const n = activeRadars.length;
		const ms = 600 + n * 180 + (composite !== 'none' ? 200 : 0) + (nexradEnabled ? 200 : 0);
		return Math.min(ms, 3000);
	}
	function togglePlay() {
		if (playing) {
			stopPlay();
		} else {
			if (steps.length === 0) return;
			playing = true;
			playTimer = setInterval(tick, _playIntervalMs());
		}
	}
	function syncAllToStep() {
		// In tilt mode the scrubber drives the radar-display frames only —
		// composite/NEXRAD/per-radar run on a different (radarca) timebase,
		// so we don't try to align them. refreshTilt renders the frame;
		// when not engaged it tears the overlay down.
		if (!tiltEngaged) {
			renderCompositeFrame();
			syncNexradTime();
			syncRadarOverlayTime();
		}
		refreshTilt();
	}
	function stepFirst() { stopPlay(); stepIdx = 0; syncAllToStep(); }
	function stepLast()  { stopPlay(); stepIdx = steps.length - 1; syncAllToStep(); }
	function stepPrev()  { stopPlay(); stepIdx = (stepIdx - 1 + steps.length) % steps.length; syncAllToStep(); }
	function stepNext()  { stopPlay(); stepIdx = (stepIdx + 1) % steps.length; syncAllToStep(); }
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
		const e = EXTENT_LARGE;
		map.addSource('nexrad', {
			type: 'image',
			url: nexradImageUrl(nexradTimeIso),
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
				urls.add(nexradImageUrl(tsForStep));
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

	async function syncNexradTime() {
		if (!map || !nexradEnabled) return;
		const src = map.getSource('nexrad') as maplibregl.ImageSource | undefined;
		if (!src || typeof src.updateImage !== 'function') return;
		const e = EXTENT_LARGE;
		const url = nexradImageUrl(nexradTimeIso);
		const token = ++nexradFetchToken;
		let blobUrl = nexradBlobs.get(url);
		if (!blobUrl) {
			try {
				const r = await fetch(url);
				if (token !== nexradFetchToken) return;   // a newer step superseded us
				if (!r.ok) return;
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
				return;
			}
		}
		if (token !== nexradFetchToken || !blobUrl) return;
		src.updateImage({
			url: blobUrl,
			coordinates: [
				[e.west, e.north],
				[e.east, e.north],
				[e.east, e.south],
				[e.west, e.south]
			]
		});
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
		const t = nexradTimeIso;
		const qs = new URLSearchParams({ radar: id, moment: currentMoment });
		if (t) qs.set('time', t);
		if (_forceBuster) qs.set('_t', String(_forceBuster));
		return apiUrl(`/api/upstream/xband_scan.png?${qs.toString()}`);
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
		const dLat = km / 111;
		const dLon = km / (111 * Math.cos((lat * Math.PI) / 180));
		const frame = currentTiltFrame();
		const url = apiUrl(
			`/api/upstream/tilt_image.png?radar=${id}&el=${tiltEl}&moment=${tiltMoment}&frame=${frame}`
		);
		const coords: ImageCorners = [
			[lon - dLon, lat + dLat],
			[lon + dLon, lat + dLat],
			[lon + dLon, lat - dLat],
			[lon - dLon, lat - dLat]
		];
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
	async function syncRadarOverlayTime() {
		if (!map || !styleReady) return;
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

		for (const t of targets) {
			if (!isCurrent()) return;
			// Skip frames already on screen. MapLibre re-fetches and re-decodes
			// on every updateImage even when the URL is identical, and this
			// runs on each step change plus the 120s poke. The URL carries the
			// time and _forceBuster, so a genuine refresh still gets through.
			if (_appliedRadarUrl.get(t.id) === t.url) continue;
			const ok = await preloadImage(t.url, isCurrent);
			if (!ok || !isCurrent()) continue;
			const src = map!.getSource(radarSrcId(t.id)) as maplibregl.ImageSource | undefined;
			if (!src) continue;
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
		}
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
		try {
			localStorage.setItem(WATERSHEDS_KEY, watershedsOn ? 'on' : 'off');
			localStorage.setItem(RESERVOIRS_KEY, reservoirsOn ? 'on' : 'off');
		} catch { /* private mode etc. */ }
		refreshGeography(theme.resolved);
	});
	// Terrain hillshade toggle — same shape as the geography one.
	$effect(() => {
		void terrainOn;
		try {
			localStorage.setItem(TERRAIN_KEY, terrainOn ? 'on' : 'off');
		} catch { /* */ }
		refreshTerrain(theme.resolved);
	});
	// 3D terrain mesh toggle — drapes the scene + pitches the camera.
	// Independent from hillshade; the underlying DEM source is shared.
	$effect(() => {
		void terrain3DOn;
		try {
			localStorage.setItem(TERRAIN_3D_KEY, terrain3DOn ? 'on' : 'off');
		} catch { /* */ }
		refreshTerrain3D();
	});
	// Stream gauges toggle. Fetch is lazy — first time the toggle flips
	// on, the loader hits /api/upstream/stream_gauges (cached server-side
	// for an hour) and renders the markers.
	$effect(() => {
		void streamGaugesOn;
		try {
			localStorage.setItem(STREAM_GAUGES_KEY, streamGaugesOn ? 'on' : 'off');
		} catch { /* */ }
		refreshStreamGauges(theme.resolved);
	});

	// ---- bulk-action helpers ------------------------------------------------
	// Fit the viewport to encompass every currently-active radar's range circle
	// (lon/lat ± range_m). Skips the no-op cases (empty selection, single radar
	// already fully on-screen) since they'd just yank the camera around.
	// User spec 2026-05-18: presets should auto-frame the radars they select
	// so the operator doesn't have to scroll to find them.
	function fitToActiveRadars(opts: { animate?: boolean } = {}) {
		if (!map || !styleReady) return;
		const ids = new Set(activeRadars);
		const rs = radars.filter((r) => ids.has(r.id));
		if (rs.length === 0) return;
		let west = +Infinity, east = -Infinity, south = +Infinity, north = -Infinity;
		for (const r of rs) {
			const e = radarExtent(r);
			if (e.west  < west)  west  = e.west;
			if (e.east  > east)  east  = e.east;
			if (e.south < south) south = e.south;
			if (e.north > north) north = e.north;
		}
		if (!isFinite(west)) return;
		map.fitBounds(
			[[west, south], [east, north]],
			{ padding: 60, duration: opts.animate === false ? 0 : 600, maxZoom: 10 }
		);
	}

	function setSelection(ids: string[]) {
		activeRadars = [...new Set(ids)];     // dedupe, fresh array reference
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
		activeRadars = activeRadars.includes(id)
			? activeRadars.filter((x) => x !== id)
			: [...activeRadars, id];
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
	const selectDown = () =>
		setSelection(
			selectableRadars.filter((r) => (radarStatus[r.id] ?? 'pass') !== 'pass').map((r) => r.id)
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
		refreshGeography(t);
		refreshTerrain(t);
		refreshTerrain3D();
		refreshStreamGauges(t);
	}

	// Stream gauges — USGS sites parsed from radarca's stream_data.csv,
	// served via /api/upstream/stream_gauges. Two visual tiers:
	//   - B-status (basic): small, faint hollow dot. Site exists upstream
	//     but doesn't expose real-time data.
	//   - R-status (real-time): brighter, larger filled circle. Click to
	//     pull live forecast + observed streamflow into a popup.
	async function refreshStreamGauges(t: 'light' | 'dark') {
		if (!map || !styleReady) return;
		// Tear down first so toggling off cleans up cleanly.
		for (const lyr of ['stream-gauge-marker']) {
			if (map.getLayer(lyr)) map.removeLayer(lyr);
		}
		if (map.getSource('stream-gauges')) map.removeSource('stream-gauges');
		if (!streamGaugesOn) return;
		let data: GeoJSON.FeatureCollection;
		try {
			data = await loadStreamGauges();
		} catch {
			return;  // upstream unreachable; toggle stays on, retry on next refresh
		}
		// Don't check isStyleLoaded() — it flickers false whenever any
		// other source is processing tiles (e.g. watersheds GeoJSON
		// finishing parse), which races our own additions.
		if (!map || !styleReady || map.getSource('stream-gauges')) return;
		map.addSource('stream-gauges', { type: 'geojson', data });
		map.addLayer(
			{
				id: 'stream-gauge-marker',
				type: 'circle',
				source: 'stream-gauges',
				paint: {
					'circle-radius': ['case', ['==', ['get', 'status'], 'R'], 5.5, 3],
					'circle-color': [
						'case',
						['==', ['get', 'status'], 'R'],
						t === 'light' ? '#0a6f9b' : '#6ec2e0',
						t === 'light' ? '#5d8aa8' : '#6e8aa0'
					],
					'circle-opacity': ['case', ['==', ['get', 'status'], 'R'], 0.95, 0.6],
					'circle-stroke-color': t === 'light' ? '#ffffff' : '#0c100d',
					'circle-stroke-width': ['case', ['==', ['get', 'status'], 'R'], 1.2, 0.6]
				}
			},
			'radar-halo'
		);
	}

	// Click handler for stream-gauge markers. Renders a MapLibre popup
	// with the COMID + status; for R-status sites, fires the two
	// per-COMID time-series fetches and populates the popup body in
	// place once both land (or both fail). Forecast wants YYYYMMDD_HH
	// (UTC); observed wants YYYYMMDD (UTC).
	function utcHourString(d: Date): string {
		const yyyy = d.getUTCFullYear();
		const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
		const dd = String(d.getUTCDate()).padStart(2, '0');
		const hh = String(d.getUTCHours()).padStart(2, '0');
		return `${yyyy}${mm}${dd}_${hh}`;
	}
	function utcDayString(d: Date): string {
		const yyyy = d.getUTCFullYear();
		const mm = String(d.getUTCMonth() + 1).padStart(2, '0');
		const dd = String(d.getUTCDate()).padStart(2, '0');
		return `${yyyy}${mm}${dd}`;
	}
	type GaugeFetch = { headers?: string[]; values?: (string | number)[] };
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
	function gaugeSummaryHtml(j: GaugeFetch | null): string {
		if (!j || !j.headers || j.headers.length <= 1) {
			return '<span class="gauge-muted">no data</span>';
		}
		// First column is COMID echo; skip it. Show a compact key:value
		// list of the remaining columns. Numerical values rounded to 2dp
		// for readability.
		const pairs: string[] = [];
		const hdrs = j.headers.slice(1);
		const vals = (j.values ?? []).slice(1);
		for (let i = 0; i < hdrs.length && i < vals.length; i++) {
			const v = vals[i];
			const rendered = typeof v === 'number' ? v.toFixed(2) : String(v);
			pairs.push(`<span class="gauge-muted">${hdrs[i]}:</span> ${rendered}`);
		}
		return pairs.join('  ·  ');
	}
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
			? `<div data-slot="forecast"><span class="gauge-muted">loading forecast…</span></div>
			   <div data-slot="observed"><span class="gauge-muted">loading observed…</span></div>`
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
			fetchGaugeData(props.comid, 'observed', utcDayString(now))
		]);
		const el = popup.getElement();
		if (!el) return;
		const fEl = el.querySelector('[data-slot="forecast"]');
		const oEl = el.querySelector('[data-slot="observed"]');
		if (fEl) fEl.innerHTML = `<span class="gauge-muted">forecast:</span> ${gaugeSummaryHtml(fcst)}`;
		if (oEl) oEl.innerHTML = `<span class="gauge-muted">observed:</span> ${gaugeSummaryHtml(obs)}`;
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
	// One source, shared. Free public tiles; no API key.
	const DEM_TILE_URL = 'https://s3.amazonaws.com/elevation-tiles-prod/v2/terrarium/{z}/{x}/{y}.png';

	function ensureTerrainSource() {
		if (!map) return;
		if (map.getSource('terrain-dem')) return;
		map.addSource('terrain-dem', {
			type: 'raster-dem',
			tiles: [DEM_TILE_URL],
			encoding: 'terrarium',
			tileSize: 256,
			maxzoom: 15,
			attribution: 'Terrain: AWS / Mapzen Open Data'
		});
	}

	function removeTerrainSourceIfUnused() {
		if (!map) return;
		// Source can only be removed if nothing references it. Both the
		// hillshade layer AND the 3D terrain (map.getTerrain()) hold
		// references; bail if either is still live.
		if (map.getLayer('hillshade-layer')) return;
		if (map.getTerrain && map.getTerrain()) return;
		if (map.getSource('terrain-dem')) map.removeSource('terrain-dem');
	}

	function refreshTerrain(t: 'light' | 'dark') {
		if (!map || !styleReady) return;
		if (map.getLayer('hillshade-layer')) map.removeLayer('hillshade-layer');
		if (terrainOn) {
			ensureTerrainSource();
			map.addLayer(
				{
					id: 'hillshade-layer',
					type: 'hillshade',
					source: 'terrain-dem',
					paint: {
						'hillshade-exaggeration': 0.55,
						'hillshade-shadow-color': t === 'light' ? '#3a3a3a' : '#000000',
						'hillshade-highlight-color': t === 'light' ? '#ffffff' : '#3a3a3a',
						'hillshade-accent-color': t === 'light' ? '#202020' : '#0a0a0a'
					}
				},
				'range-fill'
			);
		} else {
			removeTerrainSourceIfUnused();
		}
	}

	// 3D mesh terrain — drape the whole scene over the DEM and pitch the
	// camera. Disabling resets pitch to 0 so the operator goes back to a
	// clean top-down view.
	const PITCH_3D = 50;
	function refreshTerrain3D() {
		if (!map || !styleReady) return;
		if (terrain3DOn) {
			ensureTerrainSource();
			map.setTerrain({ source: 'terrain-dem', exaggeration: 1.2 });
			map.easeTo({ pitch: PITCH_3D, duration: 700 });
		} else {
			map.setTerrain(null);
			map.easeTo({ pitch: 0, duration: 500 });
			removeTerrainSourceIfUnused();
		}
	}

	// Watershed outlines + reservoir markers. Inserted before the radar
	// halo layer so radar pins always render on top — these are
	// reference overlays, not the focus. Theme-aware colors keep them
	// readable on both basemap styles.
	async function refreshGeography(t: 'light' | 'dark') {
		if (!map || !styleReady) return;

		// Watersheds: remove first so toggling off cleans up.
		if (map.getLayer('watershed-line')) map.removeLayer('watershed-line');
		if (map.getSource('watersheds')) map.removeSource('watersheds');
		if (watershedsOn) {
			const data = await loadWatersheds();
			// Guard: theme may have swapped while the fetch was in flight.
			if (!map || !styleReady || map.getSource('watersheds')) return;
			map.addSource('watersheds', { type: 'geojson', data });
			map.addLayer(
				{
					id: 'watershed-line',
					type: 'line',
					source: 'watersheds',
					paint: {
						'line-color': t === 'light' ? '#3f6478' : '#5a8aa0',
						'line-width': 0.7,
						'line-opacity': 0.4
					}
				},
				'radar-halo'
			);
		}

		// Reservoirs: marker + label. Both removed up-front, re-added if on.
		for (const lyr of ['reservoir-label', 'reservoir-marker']) {
			if (map.getLayer(lyr)) map.removeLayer(lyr);
		}
		if (map.getSource('reservoirs')) map.removeSource('reservoirs');
		if (reservoirsOn) {
			const data = await loadReservoirs();
			if (!map || !styleReady || map.getSource('reservoirs')) return;
			map.addSource('reservoirs', { type: 'geojson', data });
			map.addLayer(
				{
					id: 'reservoir-marker',
					type: 'circle',
					source: 'reservoirs',
					paint: {
						'circle-radius': 3.5,
						'circle-color': t === 'light' ? '#1f6d8e' : '#5fa8c4',
						'circle-stroke-color': t === 'light' ? '#ffffff' : '#0c100d',
						'circle-stroke-width': 1,
						'circle-opacity': 0.9
					}
				},
				'radar-halo'
			);
			map.addLayer(
				{
					id: 'reservoir-label',
					type: 'symbol',
					source: 'reservoirs',
					layout: {
						'text-field': ['get', 'name'],
						'text-size': 9.5,
						'text-offset': [0.6, 0.3],
						'text-anchor': 'left',
						'text-font': ['Stadia Regular']
					},
					paint: {
						'text-color': t === 'light' ? '#1f6d8e' : '#9bc4d2',
						'text-halo-color': t === 'light' ? '#ffffff' : '#0c100d',
						'text-halo-width': 1
					}
				},
				'radar-halo'
			);
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

	onMount(async () => {
		try {
			const saved = localStorage.getItem(PANEL_KEY);
			if (saved === 'closed') panelOpen = false;
			if (localStorage.getItem(WATERSHEDS_KEY) === 'on') watershedsOn = true;
			if (localStorage.getItem(RESERVOIRS_KEY) === 'on') reservoirsOn = true;
			if (localStorage.getItem(TERRAIN_KEY) === 'on') terrainOn = true;
			if (localStorage.getItem(TERRAIN_3D_KEY) === 'on') terrain3DOn = true;
			if (localStorage.getItem(STREAM_GAUGES_KEY) === 'on') streamGaugesOn = true;
		} catch { /* */ }
		radars = await fetch('/api/radars/meta').then((r) => r.json());
		map = new maplibregl.Map({
			container: mapDiv,
			style: styleUrl(),
			center: [-122.6, 37.95],
			zoom: 7.2,
			bearing: defaultBearingFromRadars(radars),
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

		map.on('load', () => {
			if (!map) return;
			addBaseSourcesAndLayers(theme.resolved);
			styleReady = true;

			// === Load-bearing: paint once the style is ready ===
			//
			// Every render path early-returns on `!styleReady`. loadComposite()
			// is async, so if it finishes BEFORE the map fires 'load' its
			// syncAllToStep() call is silently dropped and no overlay is ever
			// drawn — the map sits empty until something unrelated pokes it
			// (a step change, the 120s refresher, a theme swap).
			//
			// This was latent for months and only started reproducing on
			// 2026-08-27, when memoising /product_steps took it from ~3s
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
				if (playTimer) { clearInterval(playTimer); playTimer = undefined; }
			} else {
				if (!scanRefresh) scanRefresh = setInterval(pokeOverlays, 120_000);
				if (playing && !playTimer) playTimer = setInterval(tick, _playIntervalMs());
			}
		};
		document.addEventListener('visibilitychange', visHandler);
	});

	let visHandler: (() => void) | undefined;
	onDestroy(() => {
		stopPlay();
		if (scanRefresh) clearInterval(scanRefresh);
		resizeObs?.disconnect();
		map?.remove();
		if (visHandler) document.removeEventListener('visibilitychange', visHandler);
	});

	// Composites grouped by upstream source so a 10-item dropdown stays
	// readable. Order matches the Live page's grouped Products section.
	const compGroups: { label: string; options: { key: Composite; label: string }[] }[] = [
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

	const statusLabel = (s: string) =>
		({ pass: 'UP', fail: 'DOWN', warn: 'WARN', error: 'ERR', skip: '—' })[s] ?? s.toUpperCase();
</script>

<div class="flex h-full min-h-[420px] w-full flex-col">
	<!-- map area -->
	<div class="relative flex-1">
		<div bind:this={mapDiv} class="h-full w-full"></div>

		<!-- Stale-composite banner. Deliberately centred over the map rather
		     than tucked in a corner: the failure this guards against is an
		     operator reading a 50-day-old inundation frame as current, and a
		     subtle marker would not stop that. Non-interactive so it never
		     blocks map controls. -->
		{#if compositeStale && compositeAgeH !== null}
			<div class="pointer-events-none absolute left-1/2 top-3 z-20 -translate-x-1/2 max-w-[92%]">
				<div class="rounded-sm border border-[var(--color-warn)] bg-[var(--color-warn)]/[0.14] px-3 py-1.5 text-center shadow-xl backdrop-blur">
					<div class="text-[11px] font-semibold uppercase tracking-wider text-[var(--color-warn)]">
						⚠ Not current — {humanAge(compositeAgeH)} old
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
			onclick={() => { panelOpen = true; try { localStorage.setItem(PANEL_KEY, 'open'); } catch { /* */ } }}
			title="show layers"
		>
			<svg viewBox="0 0 12 12" width="11" height="11" fill="currentColor" class="text-[var(--color-muted)]">
				<polygon points="2,2 10,2 8,5 4,5" />
				<polygon points="3,6 9,6 7,9 5,9" />
				<polygon points="4,10 8,10 7,11 5,11" />
			</svg>
			<span class="label tracking-[0.2em] text-[var(--color-default)]">Layers</span>
			<span class="num text-[9.5px] text-[var(--color-faint)]">
				{(composite !== 'none' ? 1 : 0) + (nexradEnabled ? 1 : 0) + activeRadars.length} on
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
					{(composite !== 'none' ? 1 : 0) + (nexradEnabled ? 1 : 0) + activeRadars.length} on
				</span>
			</div>
			<button
				class="inline-flex h-4 w-4 items-center justify-center text-[var(--color-muted)] hover:text-[var(--color-bright)]"
				title="hide panel"
				aria-label="hide layers panel"
				onclick={() => { panelOpen = false; try { localStorage.setItem(PANEL_KEY, 'closed'); } catch { /* */ } }}
			>
				<svg viewBox="0 0 12 12" width="10" height="10" fill="none" stroke="currentColor" stroke-width="1.5">
					<line x1="2.5" y1="6" x2="9.5" y2="6" />
				</svg>
			</button>
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
				<span class="text-[9.5px] text-[var(--color-faint)]">ground truth</span>
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
			onclick={() => (terrain3DOn = !terrain3DOn)}
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
			     is active. "Default" = radarca's single pre-rendered sweep;
			     1..4 = radar-display per-elevation PPI (Phase 1: newest frame,
			     no scrubbing). -->
			{#if tiltRadarMeta}
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
						title="Pick a scan elevation. Default uses radarca's single sweep. Drives the time strip on the radar's 7-frame loop."
					>
						<option value={0}>Default (radarca)</option>
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
				{@const st = radarStatus[r.id] ?? 'skip'}
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
