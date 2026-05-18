<script lang="ts">
	import { onMount, onDestroy, untrack } from 'svelte';
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import { sentinel } from '$lib/stores/state.svelte';
	import { theme } from '$lib/stores/theme.svelte';
	import TimeControls from '$lib/components/TimeControls.svelte';
	import { url as apiUrl } from '$lib/origin';

	interface RadarMeta {
		id: string;
		lat: number;
		lon: number;
		range_m: number;
		kind: string;
		name: string;
		folder: string | null;
	}

	let mapDiv: HTMLDivElement;
	let map: maplibregl.Map | undefined;
	let resizeObs: ResizeObserver | undefined;
	let radars = $state<RadarMeta[]>([]);
	// One-shot — flips to true once the style has loaded for the first time
	// and stays true. Don't use isStyleLoaded(): it flickers to false every
	// time we call setData on a source, which would race our own mutators.
	let styleReady = false;

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
		| 'fcst_temp';
	// Default to Reflectivity composite — most useful at-a-glance view.
	let composite = $state<Composite>('comp_ref');
	let nexradEnabled = $state(false);
	let overlayOpacity = $state(0.8);

	// Layers panel show/hide — persisted per browser.
	let panelOpen = $state(true);
	const PANEL_KEY = 'sentinel-layers-open';

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

	// time controls — owned by MapView, drive composite overlay frame
	interface Step { i: number; ts: string | null; imageName: string; day: string; date: string; time: string }
	let steps = $state<Step[]>([]);
	let stepIdx = $state(-1);
	let playing = $state(false);
	let playTimer: ReturnType<typeof setInterval> | undefined;
	let activity = $state<number[]>([]);     // non-empty pixel fraction per step
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
	// All radar + forecast composites use the regional X-band extent.
	// EXTENT_BAY is reserved for hydro products (water_depth,
	// max_water_depth) which are not surfaced in this picker — they're a
	// separate product family.
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
		fcst_temp:             EXTENT_LARGE
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
		for (const id of activeRadars) {
			try {
				const r = await fetch(
					`/api/upstream/radar_steps?radar=${id}` +
					`&moment=${encodeURIComponent(currentMoment)}`
				);
				const j = await r.json();
				const s = (j.steps ?? []) as Step[];
				if (s.length > 0) {
					loadActivity(`radar=${id}&moment=${encodeURIComponent(currentMoment)}`);
					return { steps: s, idx: j.current_idx ?? s.length - 1 };
				}
			} catch { /* try next radar */ }
		}
		return null;
	}

	async function loadComposite() {
		stopPlay();
		activity = [];
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
		const url = apiUrl(`/api/upstream/product_image.png?product_id=${composite}&step=${stepIdx}&_=${Date.now()}`);
		const coords: [number, number][] = [
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
			map!.addLayer(
				{
					id: 'comp-overlay-layer',
					type: 'raster',
					source: 'comp-overlay',
					paint: { 'raster-opacity': 0.7 }
				},
				'range-fill'
			);
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
		renderCompositeFrame();
		syncNexradTime();
		syncRadarOverlayTime();
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

	function refreshRadarOverlays() {
		if (!map || !styleReady) return;
		const active = new Set(activeRadars);

		// remove overlays no longer active
		for (const id of [..._addedOverlays]) {
			if (active.has(id)) continue;
			if (map.getLayer(radarLayerId(id))) map.removeLayer(radarLayerId(id));
			if (map.getSource(radarSrcId(id))) map.removeSource(radarSrcId(id));
			_addedOverlays.delete(id);
		}
		// add overlays newly active
		for (const id of active) {
			if (_addedOverlays.has(id)) continue;
			const r = radars.find((x) => x.id === id);
			if (!r) continue;
			const e = radarExtent(r);
			map.addSource(radarSrcId(id), {
				type: 'image',
				url: xbandScanUrl(id),
				coordinates: [
					[e.west, e.north],
					[e.east, e.north],
					[e.east, e.south],
					[e.west, e.south]
				]
			});
			map.addLayer({
				id: radarLayerId(id),
				type: 'raster',
				source: radarSrcId(id),
				paint: { 'raster-opacity': overlayOpacity }
			});
			_addedOverlays.add(id);
		}
		syncBaseSources();
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
		if (!map || !styleReady) return;
		_bumpForceBuster();
		if (composite !== 'none' && map.getSource('comp-overlay')) {
			await renderCompositeFrame();
		}
		if (activeRadars.length > 0) {
			await syncRadarOverlayTime();
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
			addBaseSourcesAndLayers(t);
			styleReady = true;
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

		map.on('load', () => {
			if (!map) return;
			addBaseSourcesAndLayers(theme.resolved);
			styleReady = true;

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
			for (const layer of ['radar-point', 'radar-label']) {
				map.on('mouseenter', layer, () => {
					if (map) map.getCanvas().style.cursor = 'pointer';
				});
				map.on('mouseleave', layer, () => {
					if (map) map.getCanvas().style.cursor = '';
				});
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
		}
	];

	const statusLabel = (s: string) =>
		({ pass: 'UP', fail: 'DOWN', warn: 'WARN', error: 'ERR', skip: '—' })[s] ?? s.toUpperCase();
</script>

<div class="flex h-full min-h-[420px] w-full flex-col">
	<!-- map area -->
	<div class="relative flex-1">
		<div bind:this={mapDiv} class="h-full w-full"></div>

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
									<option value={opt.key}>{opt.label}</option>
								{/each}
							</optgroup>
						{:else}
							{#each g.options as opt}
								<option value={opt.key}>{opt.label}</option>
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
					<button
						class="num flex-1 py-1 text-[10.5px] font-medium transition-colors {currentMoment === m.key
							? 'bg-[var(--color-ok)]/20 text-[var(--color-bright)] ring-1 ring-inset ring-[var(--color-ok)]/70'
							: 'text-[var(--color-muted)] hover:bg-[var(--color-elevated)]/50 hover:text-[var(--color-default)]'}"
						title={`${m.full} — currently selected: ${m.key === currentMoment ? 'yes' : 'no'}`}
						onclick={() => (currentMoment = m.key)}
					>
						{m.short}
					</button>
				{/each}
			</div>
			<div class="mt-1 text-[10px] text-[var(--color-faint)] num text-center">
				moment: <span class="text-[var(--color-bright)]">{MOMENTS.find((m) => m.key === currentMoment)?.full ?? currentMoment}</span>
			</div>
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
