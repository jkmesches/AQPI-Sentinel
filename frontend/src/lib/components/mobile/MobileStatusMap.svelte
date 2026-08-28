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
	// Tapping a radar pin should do what tapping its row in the list does —
	// open that radar's drilldown. The mobile map has no per-radar image
	// overlays (it shows status pins plus one composite raster), so the
	// desktop's toggle-overlay behaviour would be invisible here.
	// Tapping a radar toggles its overlay, mirroring desktop. Multiple radars
	// can be active at once. Showing any radar hides the composite: the two
	// draw over the same ground and a regional composite on top of a
	// single-radar sweep is unreadable on a phone screen.
	let activeRadars = $state<string[]>([]);
	const radarsActive = $derived(activeRadars.length > 0);
	const _addedOverlays = new Set<string>();

	// Same extent math as the desktop map — km offsets from the radar centre.
	function radarExtent(r: RadarMeta) {
		const km = r.range_m / 1000;
		const dLat = km / 111;
		const dLon = km / (111 * Math.cos((r.lat * Math.PI) / 180));
		return { west: r.lon - dLon, east: r.lon + dLon, south: r.lat - dLat, north: r.lat + dLat };
	}
	const radarSrcId = (id: string) => `radar-img-${id}`;
	const radarLayerId = (id: string) => `radar-img-layer-${id}`;

	function radarScanUrl(id: string): string {
		const qs = new URLSearchParams({ radar: id, moment: 'Reflectivity' });
		const t = steps[stepIdx]?.ts;
		if (t) qs.set('time', t);
		return apiUrl(`/api/upstream/xband_scan.png?${qs.toString()}`);
	}

	function toggleRadar(id: string) {
		activeRadars = activeRadars.includes(id)
			? activeRadars.filter((x) => x !== id)
			: [...activeRadars, id];
		if (map?.getSource('radars')) (map.getSource('radars') as any).setData(geo());
		// Selecting the first radar (or clearing the last) swaps what drives
		// the scrubber between radar frames and composite steps.
		void refreshForSelection();
	}

	async function refreshForSelection() {
		await loadSteps();
		await renderComposite();
		refreshRadarOverlays();
	}

	function refreshRadarOverlays() {
		if (!mapAlive() || !styleReady || !maplibregl) return;
		const active = new Set(activeRadars);
		for (const id of [..._addedOverlays]) {
			if (active.has(id)) continue;
			if (map.getLayer(radarLayerId(id))) map.removeLayer(radarLayerId(id));
			if (map.getSource(radarSrcId(id))) map.removeSource(radarSrcId(id));
			_addedOverlays.delete(id);
		}
		for (const id of active) {
			const r = radars.find((x) => x.id === id);
			if (!r) continue;
			const e = radarExtent(r);
			const coords: [number, number][] = [
				[e.west, e.north], [e.east, e.north],
				[e.east, e.south], [e.west, e.south]
			];
			const src = map.getSource(radarSrcId(id)) as any;
			if (src && typeof src.updateImage === 'function') {
				src.updateImage({ url: radarScanUrl(id), coordinates: coords });
				continue;
			}
			if (_addedOverlays.has(id)) continue;
			map.addSource(radarSrcId(id), {
				type: 'image', url: radarScanUrl(id), coordinates: coords
			});
			// Below the halo so the status pins stay readable on top.
			const beforeId = map.getLayer('radar-halo') ? 'radar-halo' : undefined;
			map.addLayer({
				id: radarLayerId(id), type: 'raster', source: radarSrcId(id),
				paint: { 'raster-opacity': 0.82 }
			}, beforeId);
			_addedOverlays.add(id);
		}
	}

	let radars = $state<RadarMeta[]>([]);
	let loadError = $state<string | null>(null);
	let styleReady = $state(false);
	// iOS Safari freely drops the WebGL context (backgrounding, memory
	// pressure, leaving the tab too long). After context loss MapLibre's
	// `map.style` becomes undefined and any subsequent .getLayer() throws
	// `TypeError: this.style is undefined`. We listen for the canvas event,
	// flip this flag, and short-circuit every map mutation. A user-visible
	// banner offers a one-tap reload — MapLibre does not reliably recover
	// on its own even after `webglcontextrestored`.
	let mapDead = $state(false);
	function mapAlive(): boolean {
		return !!(map && !mapDead && map.style);
	}

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
						halo_color:   haloColorFor(s),
						// Drives the selection ring — without visible feedback a
						// tap that toggles an overlay off looks like a dead tap.
						selected: activeRadars.includes(r.id) ? 1 : 0
					}
				};
			})
		};
	}

	function refreshRadars() {
		if (!mapAlive() || !maplibregl) return;
		try {
			const src = map.getSource('radars');
			if (src) { src.setData(geo()); return; }
		} catch { mapDead = true; return; }
		// Touch targets: a radar circle is ~7px, which is far below the ~44px
		// a finger can reliably hit, so query a padded box around the tap
		// rather than requiring a direct hit on the rendered geometry.
		const TAP_PAD = 18;
		map.on('click', (e: maplibregl.MapMouseEvent) => {
			if (!map) return;
			const box: [maplibregl.PointLike, maplibregl.PointLike] = [
				[e.point.x - TAP_PAD, e.point.y - TAP_PAD],
				[e.point.x + TAP_PAD, e.point.y + TAP_PAD]
			];
			const hits = map.queryRenderedFeatures(box, {
				layers: ['radar-circles', 'radar-label', 'radar-halo'].filter((l) => !!map!.getLayer(l))
			});
			const id = hits[0]?.properties?.id as string | undefined;
			if (id) toggleRadar(id);
		});

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
				// Selected radars grow and take an accent ring. A tap that
				// toggles an overlay needs visible feedback on the pin itself,
				// or turning one OFF reads as an unresponsive tap.
				'circle-radius': ['case', ['==', ['get', 'selected'], 1], 8, 6],
				'circle-color': ['get', 'center_color'],
				'circle-stroke-color': [
					'case', ['==', ['get', 'selected'], 1],
					'#8ABE82',
					theme.resolved === 'light' ? '#ffffff' : '#0c100d'
				],
				'circle-stroke-width': ['case', ['==', ['get', 'selected'], 1], 3, 1.5]
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
		// With radars selected the scrubber follows radar frames, not the
		// composite's — otherwise play/scrub would move a timeline that no
		// longer corresponds to anything on screen.
		if (activeRadars.length > 0) {
			try {
				const r = await fetch(
					`/api/upstream/radar_steps?radar=${activeRadars[0]}&moment=Reflectivity`
				);
				if (!r.ok) throw new Error(`HTTP ${r.status}`);
				const j = await r.json();
				steps = (j.steps ?? []) as Step[];
				stepIdx = j.current_idx ?? steps.length - 1;
			} catch {
				steps = []; stepIdx = -1;
			}
			return;
		}
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
		if (!mapAlive() || !styleReady || !maplibregl) return;
		try {
			// Radars win: their sweeps and the regional composite overlap the
			// same ground, and stacking them is unreadable on a phone.
			if (radarsActive || composite === 'none' || stepIdx < 0) {
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
		} catch (err) {
			// Style raced to undefined between the alive check and the mutation —
			// flip the flag so subsequent ticks bail out cleanly.
			mapDead = true;
			stopPlay();
		}
	}

	function stopPlay() {
		playing = false;
		if (playTimer) { clearInterval(playTimer); playTimer = undefined; }
	}
	function tick() {
		if (steps.length === 0) return;
		stepIdx = (stepIdx + 1) % steps.length;
		syncFrame();
	}
	function togglePlay() {
		if (playing) { stopPlay(); return; }
		if (steps.length === 0) return;
		playing = true;
		playTimer = setInterval(tick, 800);
	}
	// syncFrame, not renderComposite: with radars selected the composite is
	// suppressed and the radar overlays are what actually need re-pointing.
	function syncFrame() { renderComposite(); refreshRadarOverlays(); }
	function stepPrev() { stopPlay(); if (steps.length) { stepIdx = (stepIdx - 1 + steps.length) % steps.length; syncFrame(); } }
	function stepNext() { stopPlay(); if (steps.length) { stepIdx = (stepIdx + 1) % steps.length; syncFrame(); } }

	// Re-fetch step list whenever composite changes, then render the latest.
	$effect(() => {
		void composite;
		(async () => {
			stopPlay();
			await loadSteps();
			syncFrame();
		})();
	});

	// Re-render the current frame when style/map become ready.
	$effect(() => {
		void styleReady;
		if (styleReady) syncFrame();
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
			// Capture WebGL context-loss BEFORE MapLibre's internal handlers
			// run — preventDefault tells the browser we'd accept a restored
			// context, but in practice MapLibre's style is gone either way.
			const canvas = map.getCanvas() as HTMLCanvasElement;
			canvas.addEventListener('webglcontextlost', (ev: Event) => {
				ev.preventDefault();
				mapDead = true;
				stopPlay();
			}, false);
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
		if (mapAlive() && map.isStyleLoaded()) refreshRadars();
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
		<div class="relative">
			<div bind:this={mapDiv} class="map-canvas"></div>
			{#if mapDead}
				<!-- WebGL context lost — usually because iOS Safari paused the
				     tab. MapLibre can't recover its style on its own; the
				     cleanest path is a hard reload of the page. -->
				<div class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-black/60 text-center text-[12px]" style="-webkit-backdrop-filter: blur(4px); backdrop-filter: blur(4px);">
					<div class="text-[var(--color-bright)]">Map paused (graphics context dropped)</div>
					<button type="button" onclick={() => location.reload()}
						class="rounded-md border border-[var(--color-ok)] bg-[var(--color-ok)]/20 px-4 py-2 text-[12px] uppercase tracking-wider text-[var(--color-bright)] active:bg-[var(--color-ok)]/30"
						style="-webkit-tap-highlight-color: transparent;">
						Reload map
					</button>
				</div>
			{/if}
		</div>
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
					oninput={(e) => { stopPlay(); stepIdx = Number((e.target as HTMLInputElement).value); syncFrame(); }}
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
