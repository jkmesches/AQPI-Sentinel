<script lang="ts">
	import { onMount, onDestroy, untrack } from 'svelte';
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import { sentinel } from '$lib/stores/state.svelte';
	import { theme } from '$lib/stores/theme.svelte';
	import TimeControls from '$lib/components/TimeControls.svelte';

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

	type Composite = 'none' | 'comp_ref' | 'comp_now' | 'water_depth';
	let composite = $state<Composite>('none');
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
	const COMP_EXTENT: Record<Composite, typeof EXTENT_LARGE | null> = {
		none: null,
		comp_ref: EXTENT_LARGE,
		comp_now: EXTENT_LARGE,
		water_depth: EXTENT_BAY
	};

	function radarExtent(r: RadarMeta) {
		const km = r.range_m / 1000;
		const dLat = km / 111;
		const dLon = km / (111 * Math.cos((r.lat * Math.PI) / 180));
		return { west: r.lon - dLon, east: r.lon + dLon, south: r.lat - dLat, north: r.lat + dLat };
	}

	const verdictColor = (status: string) =>
		({
			pass: '#34d399',
			warn: '#fbbf24',
			fail: '#f87171',
			error: '#f87171',
			skip: '#6b7280'
		})[status] ?? '#6b7280';

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
						color: verdictColor(status),
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
			try {
				const r = await fetch(
					`/api/upstream/radar_steps?radar=${activeRadars[0]}` +
					`&moment=${encodeURIComponent(currentMoment)}`
				);
				const j = await r.json();
				steps = j.steps as Step[];
				stepIdx = j.current_idx ?? steps.length - 1;
			} catch {
				steps = [];
				stepIdx = -1;
			}
			loadActivity(`radar=${activeRadars[0]}&moment=${encodeURIComponent(currentMoment)}`);
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

	function renderCompositeFrame() {
		if (!map || !styleReady) return;
		const e = COMP_EXTENT[composite];
		if (!e || stepIdx < 0) {
			if (map.getLayer('comp-overlay-layer')) map.removeLayer('comp-overlay-layer');
			if (map.getSource('comp-overlay')) map.removeSource('comp-overlay');
			return;
		}
		const url = `/api/upstream/product_image.png?product_id=${composite}&step=${stepIdx}&_=${Date.now()}`;
		const coords: [number, number][] = [
			[e.west, e.north],
			[e.east, e.north],
			[e.east, e.south],
			[e.west, e.south]
		];
		const src = map.getSource('comp-overlay') as maplibregl.ImageSource | undefined;
		if (src && typeof src.updateImage === 'function') {
			// Hot-swap the image bytes in place — no flash, no layer churn.
			src.updateImage({ url, coordinates: coords });
		} else {
			if (map.getLayer('comp-overlay-layer')) map.removeLayer('comp-overlay-layer');
			if (map.getSource('comp-overlay')) map.removeSource('comp-overlay');
			map.addSource('comp-overlay', { type: 'image', url, coordinates: coords });
			map.addLayer(
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
	function tick() {
		if (steps.length === 0) return;
		stepIdx = (stepIdx + 1) % steps.length;
		syncAllToStep();
	}
	function togglePlay() {
		if (playing) {
			stopPlay();
		} else {
			if (steps.length === 0) return;
			playing = true;
			playTimer = setInterval(tick, 750);
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
	function scrub(i: number) { stopPlay(); stepIdx = i; syncAllToStep(); }

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
		const t = nexradTimeIso;   // null when latest / no composite — get latest
		const qs = new URLSearchParams({
			radar: id,
			moment: currentMoment,
			_: String(Date.now())
		});
		if (t) qs.set('time', t);
		return `/api/upstream/xband_scan.png?${qs.toString()}`;
	}

	function refreshRadarOverlays() {
		if (!map || !styleReady) return;
		const active = new Set(activeRadars);
		const present = new Set<string>();
		map.getStyle().layers.forEach((l) => {
			const m = l.id.match(/^radar-(.+)-layer$/);
			if (m) present.add(m[1]);
		});
		// remove ones no longer active
		for (const id of present) {
			if (!active.has(id)) {
				if (map.getLayer(radarLayerId(id))) map.removeLayer(radarLayerId(id));
				if (map.getSource(radarSrcId(id))) map.removeSource(radarSrcId(id));
			}
		}
		// add new
		for (const id of active) {
			if (present.has(id) && map.getSource(radarSrcId(id))) continue;
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
		}
		// keep existing layers' opacity in sync
		for (const id of active) {
			if (map.getLayer(radarLayerId(id))) {
				map.setPaintProperty(radarLayerId(id), 'raster-opacity', overlayOpacity);
			}
		}
		syncBaseSources();
	}

	function syncRadarOverlayTime() {
		if (!map || !styleReady) return;
		for (const id of activeRadars) {
			const src = map.getSource(radarSrcId(id)) as maplibregl.ImageSource | undefined;
			if (!src) continue;
			const r = radars.find((x) => x.id === id);
			if (!r) continue;
			const e = radarExtent(r);
			src.updateImage({
				url: xbandScanUrl(id),
				coordinates: [
					[e.west, e.north],
					[e.east, e.north],
					[e.east, e.south],
					[e.west, e.south]
				]
			});
		}
	}

	// ---- reactive sync ------------------------------------------------------
	// Defensive: $effect tracking on array reads can be unreliable in some
	// Svelte 5 builds. Mutator functions call refreshRadarOverlays() directly
	// too, so the map updates regardless.
	$effect(() => {
		void radarStatus;
		void activeRadars;
		syncBaseSources();
	});
	$effect(() => {
		void composite;
		void activeRadars.length;
		void currentMoment;            // re-load manifest when moment changes (radar-driven timeline only)
		untrack(() => { loadComposite(); });
	});
	$effect(() => {
		void nexradEnabled;
		refreshNexrad();
	});
	// keep NEXRAD's time-aware tiles in sync with the scrubber
	$effect(() => {
		void nexradTimeIso;
		syncNexradTime();
	});
	$effect(() => {
		void activeRadars;
		void overlayOpacity;
		refreshRadarOverlays();
	});
	// switching the moment swaps every active overlay to that moment.
	$effect(() => {
		void currentMoment;
		syncRadarOverlayTime();
	});

	// ---- bulk-action helpers ------------------------------------------------
	function setSelection(ids: string[]) {
		activeRadars = [...new Set(ids)];     // dedupe, fresh array reference
		// belt-and-suspenders: call the map updates directly so we don't rely
		// solely on $effect tracking through the array reassignment.
		syncBaseSources();
		refreshRadarOverlays();
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
	function pokeOverlays() {
		// Periodically pulls fresh imagery for whatever is on-screen, via
		// updateImage() so there's no flash.
		if (composite !== 'none' && map?.getSource('comp-overlay')) {
			renderCompositeFrame();
		}
		if (activeRadars.length > 0 && styleReady && map) {
			for (const id of activeRadars) {
				const src = map.getSource(radarSrcId(id)) as maplibregl.ImageSource | undefined;
				if (!src) continue;
				const r = radars.find((x) => x.id === id);
				if (!r) continue;
				const e = radarExtent(r);
				src.updateImage({
					url: `/api/upstream/xband_scan.png?radar=${id}&_=${Date.now()}`,
					coordinates: [
						[e.west, e.north],
						[e.east, e.north],
						[e.east, e.south],
						[e.west, e.south]
					]
				});
			}
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
		map.addLayer({
			id: 'radar-halo',
			type: 'circle',
			source: 'radars',
			paint: {
				'circle-radius': ['case', ['get', 'active'], 14, 11],
				'circle-color': ['get', 'color'],
				'circle-opacity': ['case', ['get', 'active'], 0.32, 0.16],
				'circle-stroke-color': ['get', 'color'],
				'circle-stroke-width': ['case', ['get', 'active'], 2, 1.4]
			}
		});
		map.addLayer({
			id: 'radar-point',
			type: 'circle',
			source: 'radars',
			paint: {
				'circle-radius': ['case', ['get', 'active'], 5.5, 4],
				'circle-color': ['get', 'color'],
				'circle-stroke-color': t === 'light' ? '#ffffff' : '#07080b',
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
				'text-color': t === 'light' ? '#1d2330' : '#d4d8e0',
				'text-halo-color': t === 'light' ? '#ffffff' : '#07080b',
				'text-halo-width': 1.2
			}
		});
		refreshComposite();
		refreshNexrad();
		refreshRadarOverlays();
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
			attributionControl: { compact: true }
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
	});

	onDestroy(() => {
		stopPlay();
		if (scanRefresh) clearInterval(scanRefresh);
		resizeObs?.disconnect();
		map?.remove();
	});

	const compOptions: { key: Composite; label: string; short: string }[] = [
		{ key: 'none',        label: 'Off',                       short: 'Off' },
		{ key: 'comp_ref',    label: 'X-band Composite',          short: 'Z' },
		{ key: 'comp_now',    label: 'X-band Nowcast (+30m)',     short: 'Ż' },
		{ key: 'water_depth', label: 'Water Depth',               short: 'H₂O' }
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

		<!-- Composite — single pill row -->
		<div class="px-3 py-2">
			<span class="label">Composite</span>
			<div class="mt-1 grid grid-cols-4 overflow-hidden rounded-sm border border-[var(--color-border-strong)]">
				{#each compOptions as opt, i}
					<button
						class="px-1 py-1 text-[10.5px] transition-colors {composite === opt.key
							? 'bg-[var(--color-elevated)] text-[var(--color-bright)]'
							: 'text-[var(--color-muted)] hover:bg-[var(--color-elevated)]/60 hover:text-[var(--color-default)]'} {i > 0 ? 'border-l border-[var(--color-border)]' : ''}"
						title={opt.label}
						onclick={() => (composite = opt.key)}
					>
						{opt.short}
					</button>
				{/each}
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
			<!-- moment tabs inline -->
			<div class="mt-1 flex gap-px">
				{#each MOMENTS as m}
					<button
						class="num flex-1 py-0.5 text-[10px] transition-colors {currentMoment === m.key
							? 'border-b border-[var(--color-bright)] text-[var(--color-bright)]'
							: 'border-b border-transparent text-[var(--color-muted)] hover:text-[var(--color-default)]'}"
						title={m.full}
						onclick={() => (currentMoment = m.key)}
					>
						{m.short}
					</button>
				{/each}
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
