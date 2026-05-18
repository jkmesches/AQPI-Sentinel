<script lang="ts">
	/** Mobile status map.
	 *
	 *  Shows the X-band radar network with status-coloured icons (red center
	 *  + yellow halo for ghost-up; full red for hard down). Now also carries
	 *  a compact composite picker + playback strip so the operator can scrub
	 *  through historical radar imagery on phone without bouncing to the
	 *  desktop view. Default composite: Reflectivity, matching desktop
	 *  (2026-05-18 review).
	 */
	import { onMount, onDestroy } from 'svelte';
	import { sentinel } from '$lib/stores/state.svelte';
	import { theme } from '$lib/stores/theme.svelte';
	import { url as apiUrl } from '$lib/origin';

	// Stadia styles — same as desktop MapView. Theme-locked at mount.
	const STYLE_LIGHT = 'https://tiles.stadiamaps.com/styles/alidade_smooth.json';
	const STYLE_DARK = 'https://tiles.stadiamaps.com/styles/alidade_smooth_dark.json';

	// Mirrors MapView's status palette.
	const VERDICT_LIGHT: Record<string, string> = {
		pass: '#16a34a', warn: '#9a6905', fail: '#B91C1C',
		error: '#B91C1C', skip: '#57584C'
	};
	const VERDICT_DARK: Record<string, string> = {
		pass: '#4ade80', warn: '#fbbf24', fail: '#f87171',
		error: '#f87171', skip: '#8a9389'
	};

	interface RadarMeta { id: string; lat: number; lon: number; range_m: number; kind: string; name: string }
	interface Step { i: number; ts: string | null; imageName: string; day: string; date: string; time: string }

	// Same regional extent the desktop composite uses for X-band + forecast
	// products. Source of truth: backend/checks/imaging.py.
	const EXTENT_LARGE = { west: -124.005, east: -121.195, south: 36.5, north: 39.505 };

	let mapDiv: HTMLDivElement;
	let map: any = null;
	let maplibregl: any = null;
	let radars = $state<RadarMeta[]>([]);
	let loadError = $state<string | null>(null);
	let styleReady = $state(false);

	// --- composite + playback ----------------------------------------------
	type Composite = 'none' | 'qpe_15min' | 'qpe_1hr' | 'precip_rate_radar'
		| 'comp_ref' | 'comp_now'
		| 'fcst_total_precip' | 'fcst_total_precip_cum'
		| 'fcst_precip_rate' | 'fcst_temp';
	const COMPOSITES: { key: Composite; short: string; group: 'r' | 'f' | 'off' }[] = [
		{ key: 'none',                  short: 'Off',         group: 'off' },
		{ key: 'comp_ref',              short: 'Z',           group: 'r' },
		{ key: 'comp_now',              short: 'Ż',           group: 'r' },
		{ key: 'qpe_15min',             short: 'QPE 15m',     group: 'r' },
		{ key: 'qpe_1hr',               short: 'QPE 1h',      group: 'r' },
		{ key: 'precip_rate_radar',     short: 'Rate',        group: 'r' },
		{ key: 'fcst_total_precip',     short: 'F·Precip',    group: 'f' },
		{ key: 'fcst_total_precip_cum', short: 'F·Cum',       group: 'f' },
		{ key: 'fcst_precip_rate',      short: 'F·Rate',      group: 'f' },
		{ key: 'fcst_temp',             short: 'F·Temp',      group: 'f' }
	];
	let composite = $state<Composite>('comp_ref');
	let steps = $state<Step[]>([]);
	let stepIdx = $state(-1);
	let playing = $state(false);
	let playTimer: ReturnType<typeof setInterval> | undefined;

	const statusByRadar = $derived.by(() => {
		const out: Record<string, string> = {};
		for (const r of sentinel.rollup?.stages?.L2 ?? []) out[r.target] = r.status;
		return out;
	});

	function centerColorFor(s: string): string {
		const t = theme.resolved === 'light' ? VERDICT_LIGHT : VERDICT_DARK;
		if (s === 'pass') return t.pass;
		if (s === 'warn' || s === 'fail' || s === 'error') return t.fail;
		return t.skip;
	}
	function haloColorFor(s: string): string {
		const t = theme.resolved === 'light' ? VERDICT_LIGHT : VERDICT_DARK;
		if (s === 'pass') return t.pass;
		if (s === 'warn') return t.warn;
		if (s === 'fail' || s === 'error') return t.fail;
		return t.skip;
	}

	// Programmatic bearing — northernmost top-left, southernmost bottom-right.
	// Same algorithm as desktop MapView's defaultBearingFromRadars.
	function defaultBearing(rs: RadarMeta[]): number {
		const xb = rs.filter((r) => r.kind === 'xband' || r.kind === 'cband');
		if (xb.length < 2) return 0;
		const north = xb.reduce((a, b) => (a.lat > b.lat ? a : b));
		const south = xb.reduce((a, b) => (a.lat < b.lat ? a : b));
		const midLat = ((north.lat + south.lat) / 2) * Math.PI / 180;
		const east = (north.lon - south.lon) * Math.cos(midLat);
		const ang = Math.atan2(east, north.lat - south.lat) * 180 / Math.PI;
		return ang - (-45);
	}

	function geo() {
		return {
			type: 'FeatureCollection' as const,
			features: radars.map((r) => {
				const s = statusByRadar[r.id] ?? 'skip';
				return {
					type: 'Feature' as const,
					geometry: { type: 'Point' as const, coordinates: [r.lon, r.lat] },
					properties: {
						id: r.id, name: r.name, status: s,
						center_color: centerColorFor(s),
						halo_color:   haloColorFor(s)
					}
				};
			})
		};
	}

	function refreshRadars() {
		if (!map || !maplibregl) return;
		const src = map.getSource('radars');
		if (src) { src.setData(geo()); return; }
		map.addSource('radars', { type: 'geojson', data: geo() });
		map.addLayer({
			id: 'radar-halo', type: 'circle', source: 'radars',
			paint: {
				'circle-radius': 11,
				'circle-color': ['get', 'halo_color'],
				'circle-opacity': 0.28,
				'circle-stroke-color': ['get', 'halo_color'],
				'circle-stroke-width': 1.6
			}
		});
		map.addLayer({
			id: 'radar-circles', type: 'circle', source: 'radars',
			paint: {
				'circle-radius': 6,
				'circle-color': ['get', 'center_color'],
				'circle-stroke-color': theme.resolved === 'light' ? '#ffffff' : '#0c100d',
				'circle-stroke-width': 1.5
			}
		});
		map.addLayer({
			id: 'radar-label', type: 'symbol', source: 'radars',
			layout: {
				'text-field': ['get', 'id'],
				'text-size': 10,
				'text-offset': [0.95, 0.4],
				'text-anchor': 'left',
				'text-font': ['Stadia Regular']
			},
			paint: {
				'text-color': theme.resolved === 'light' ? '#1c1f1c' : '#d0d6d0',
				'text-halo-color': theme.resolved === 'light' ? '#ffffff' : '#0c100d',
				'text-halo-width': 1.2
			}
		});
	}

	async function loadSteps() {
		if (composite === 'none') {
			steps = [];
			stepIdx = -1;
			return;
		}
		try {
			const r = await fetch(`/api/upstream/product_steps?product_id=${composite}`);
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			const j = await r.json();
			steps = (j.steps ?? []) as Step[];
			stepIdx = j.current_idx ?? steps.length - 1;
		} catch {
			steps = []; stepIdx = -1;
		}
	}

	function compositeImageUrl(idx: number): string {
		return apiUrl(`/api/upstream/product_image.png?product_id=${composite}&step=${idx}&_=${Date.now()}`);
	}

	async function renderComposite() {
		if (!map || !styleReady || !maplibregl) return;
		if (composite === 'none' || stepIdx < 0) {
			if (map.getLayer('comp-layer')) map.removeLayer('comp-layer');
			if (map.getSource('comp-src')) map.removeSource('comp-src');
			return;
		}
		const url = compositeImageUrl(stepIdx);
		const e = EXTENT_LARGE;
		const coords: [number, number][] = [
			[e.west, e.north], [e.east, e.north],
			[e.east, e.south], [e.west, e.south]
		];
		const src = map.getSource('comp-src') as any;
		if (src && typeof src.updateImage === 'function') {
			src.updateImage({ url, coordinates: coords });
		} else {
			if (map.getLayer('comp-layer')) map.removeLayer('comp-layer');
			if (map.getSource('comp-src')) map.removeSource('comp-src');
			map.addSource('comp-src', { type: 'image', url, coordinates: coords });
			// Insert below the radar halo so dots remain visible on top.
			const beforeId = map.getLayer('radar-halo') ? 'radar-halo' : undefined;
			map.addLayer({
				id: 'comp-layer', type: 'raster', source: 'comp-src',
				paint: { 'raster-opacity': 0.78 }
			}, beforeId);
		}
	}

	function stopPlay() {
		playing = false;
		if (playTimer) { clearInterval(playTimer); playTimer = undefined; }
	}
	function tick() {
		if (steps.length === 0) return;
		stepIdx = (stepIdx + 1) % steps.length;
		renderComposite();
	}
	function togglePlay() {
		if (playing) { stopPlay(); return; }
		if (steps.length === 0) return;
		playing = true;
		playTimer = setInterval(tick, 800);
	}
	function stepPrev() { stopPlay(); if (steps.length) { stepIdx = (stepIdx - 1 + steps.length) % steps.length; renderComposite(); } }
	function stepNext() { stopPlay(); if (steps.length) { stepIdx = (stepIdx + 1) % steps.length; renderComposite(); } }

	// Re-fetch step list whenever composite changes, then render the latest.
	$effect(() => {
		void composite;
		(async () => {
			stopPlay();
			await loadSteps();
			renderComposite();
		})();
	});

	// Re-render the current frame when style/map become ready.
	$effect(() => {
		void styleReady;
		if (styleReady) renderComposite();
	});

	onMount(async () => {
		try {
			const resp = await fetch(apiUrl('/api/radars/meta'));
			if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
			const all = (await resp.json()) as RadarMeta[];
			radars = all.filter((r) => r.kind === 'xband' || r.kind === 'cband');

			const mod = await import('maplibre-gl');
			maplibregl = (mod as any).default ?? mod;
			await import('maplibre-gl/dist/maplibre-gl.css');

			map = new maplibregl.Map({
				container: mapDiv,
				style: theme.resolved === 'light' ? STYLE_LIGHT : STYLE_DARK,
				center: [-122.5, 37.75],
				zoom: 6.7,
				bearing: defaultBearing(radars),
				attributionControl: false,
				dragRotate: false,
				pitchWithRotate: false,
				touchPitch: false,
				cooperativeGestures: false
			});
			map.on('load', () => {
				refreshRadars();
				styleReady = true;
			});
		} catch (e) {
			loadError = (e as Error).message;
		}
	});

	onDestroy(() => {
		stopPlay();
		map?.remove();
	});

	// Re-paint radar dots when L2 status updates.
	$effect(() => {
		void statusByRadar;
		if (map && map.isStyleLoaded()) refreshRadars();
	});

	const stepLabel = $derived(
		stepIdx >= 0 && stepIdx < steps.length
			? `${(steps[stepIdx].time ?? '').slice(0, 5)}  ${steps[stepIdx].date ?? ''}`.trim()
			: ''
	);
	const compLabel = $derived(COMPOSITES.find((c) => c.key === composite)?.short ?? '');
</script>

<div class="overflow-hidden rounded-md border border-[var(--color-border)] bg-[var(--color-surface)]">
	<!-- Composite chip strip — horizontally scrollable so all 10 chips fit
	     without breaking layout on narrow phones. Active chip uses the
	     same OK-tinted treatment as the desktop moment selector. -->
	<div class="flex items-center gap-1 overflow-x-auto border-b border-[var(--color-border)] px-2 py-1.5"
		style="-webkit-overflow-scrolling: touch;">
		{#each COMPOSITES as c}
			{@const sel = composite === c.key}
			<button
				type="button"
				onclick={() => (composite = c.key)}
				class="shrink-0 rounded-md border px-2.5 py-1 text-[11px] num {sel
					? 'border-[var(--color-ok)] bg-[var(--color-ok)]/20 text-[var(--color-bright)] ring-1 ring-inset ring-[var(--color-ok)]/60'
					: 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
				style="-webkit-tap-highlight-color: transparent; min-height: 32px;"
				title={c.key}
			>
				{c.short}
			</button>
		{/each}
	</div>

	{#if loadError}
		<div class="px-3 py-4 text-[12px] text-[var(--color-fail)]">map failed: {loadError}</div>
	{:else}
		<div bind:this={mapDiv} class="map-canvas"></div>
	{/if}

	<!-- Playback strip. Always visible when a composite is on. -->
	{#if composite !== 'none'}
		<div class="flex items-center gap-2 border-t border-[var(--color-border)] px-2 py-1.5">
			<button type="button" onclick={stepPrev} disabled={steps.length === 0}
				class="inline-flex h-8 w-8 items-center justify-center rounded-md text-[var(--color-default)] active:bg-[var(--color-elevated)] disabled:opacity-30"
				style="-webkit-tap-highlight-color: transparent;"
				aria-label="previous frame">
				<svg viewBox="0 0 16 16" width="13" height="13" fill="currentColor"><polygon points="4,8 13,3 13,13" /></svg>
			</button>
			<button type="button" onclick={togglePlay} disabled={steps.length === 0}
				class="inline-flex h-9 w-9 items-center justify-center rounded-full border {playing
					? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-ok)]'
					: 'border-[var(--color-border-strong)] text-[var(--color-bright)]'} disabled:opacity-30"
				style="-webkit-tap-highlight-color: transparent;"
				aria-label={playing ? 'pause' : 'play'}>
				{#if playing}
					<svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor"><rect x="4" y="3" width="3" height="10" /><rect x="9" y="3" width="3" height="10" /></svg>
				{:else}
					<svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor"><polygon points="4,3 13,8 4,13" /></svg>
				{/if}
			</button>
			<button type="button" onclick={stepNext} disabled={steps.length === 0}
				class="inline-flex h-8 w-8 items-center justify-center rounded-md text-[var(--color-default)] active:bg-[var(--color-elevated)] disabled:opacity-30"
				style="-webkit-tap-highlight-color: transparent;"
				aria-label="next frame">
				<svg viewBox="0 0 16 16" width="13" height="13" fill="currentColor"><polygon points="12,8 3,3 3,13" /></svg>
			</button>
			<div class="num ml-1 flex-1 truncate text-[11.5px] text-[var(--color-bright)]">
				{compLabel}
				{#if stepLabel} · <span class="text-[var(--color-muted)]">{stepLabel}Z</span>{/if}
			</div>
			{#if steps.length > 0}
				<input
					type="range"
					min="0"
					max={steps.length - 1}
					step="1"
					value={stepIdx}
					oninput={(e) => { stopPlay(); stepIdx = Number((e.target as HTMLInputElement).value); renderComposite(); }}
					class="w-24 accent-[var(--color-ok)]"
					aria-label="scrub frames"
				/>
			{/if}
		</div>
	{/if}
</div>

<style>
	.map-canvas {
		width: 100%;
		height: 260px;
	}
</style>
