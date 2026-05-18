<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { sentinel } from '$lib/stores/state.svelte';
	import { theme } from '$lib/stores/theme.svelte';
	import { url } from '$lib/origin';

	// Stadia styles — same as desktop MapView. Theme-locked at mount;
	// the mobile UI doesn't try to re-style on theme flips (cheap to
	// just close + reopen the More page → return to /m).
	const STYLE_LIGHT = 'https://tiles.stadiamaps.com/styles/alidade_smooth.json';
	const STYLE_DARK = 'https://tiles.stadiamaps.com/styles/alidade_smooth_dark.json';

	// Mirrors MapView.VERDICT_LIGHT/DARK — keep in sync if the palette
	// shifts (a single source of truth for both would mean exposing
	// MapView's internals — not worth the coupling for 6 colors).
	const VERDICT_LIGHT: Record<string, string> = {
		pass: '#16a34a', warn: '#9a6905', fail: '#B91C1C',
		error: '#B91C1C', skip: '#57584C'
	};
	const VERDICT_DARK: Record<string, string> = {
		pass: '#4ade80', warn: '#fbbf24', fail: '#f87171',
		error: '#f87171', skip: '#8a9389'
	};

	interface RadarMeta { id: string; lat: number; lon: number; range_m: number; kind: string; name: string }

	let mapDiv: HTMLDivElement;
	let map: any = null;
	let maplibregl: any = null;
	let radars = $state<RadarMeta[]>([]);
	let loadError = $state<string | null>(null);

	const statusByRadar = $derived.by(() => {
		const out: Record<string, string> = {};
		for (const r of sentinel.rollup?.stages?.L2 ?? []) out[r.target] = r.status;
		return out;
	});

	function colorFor(status: string): string {
		const table = theme.resolved === 'light' ? VERDICT_LIGHT : VERDICT_DARK;
		return table[status] ?? table.skip;
	}

	// Per-status icon split. Mirrors desktop MapView (Group 3c): center dot
	// = worst-case verdict, halo = staleness mode. Ghost-up (warn) reads as
	// red-center + yellow-ring so the eye still parses "broken" at a glance
	// without losing the "we're not sure yet" nuance.
	function centerColorFor(status: string): string {
		const table = theme.resolved === 'light' ? VERDICT_LIGHT : VERDICT_DARK;
		if (status === 'pass') return table.pass;
		if (status === 'warn' || status === 'fail' || status === 'error') return table.fail;
		return table.skip;
	}
	function haloColorFor(status: string): string {
		const table = theme.resolved === 'light' ? VERDICT_LIGHT : VERDICT_DARK;
		if (status === 'pass') return table.pass;
		if (status === 'warn') return table.warn;
		if (status === 'fail' || status === 'error') return table.fail;
		return table.skip;
	}

	function strokeFor(): string {
		// Match the cell-stroke logic in desktop MapView (visible against
		// either basemap palette).
		return theme.resolved === 'light' ? '#ffffff' : '#0c100d';
	}

	function geo() {
		const features = radars.map((r) => {
			const status = statusByRadar[r.id] ?? 'skip';
			return {
				type: 'Feature' as const,
				geometry: { type: 'Point' as const, coordinates: [r.lon, r.lat] },
				properties: {
					id: r.id,
					name: r.name,
					status,
					color:        colorFor(status),     // legacy
					center_color: centerColorFor(status),
					halo_color:   haloColorFor(status)
				}
			};
		});
		return { type: 'FeatureCollection' as const, features };
	}

	function refreshSource() {
		if (!map || !maplibregl) return;
		const src = map.getSource('radars');
		if (src) {
			src.setData(geo());
		} else {
			map.addSource('radars', { type: 'geojson', data: geo() });
			// Halo (wider ring) reads staleness mode.
			map.addLayer({
				id: 'radar-halo',
				type: 'circle',
				source: 'radars',
				paint: {
					'circle-radius': 11,
					'circle-color': ['get', 'halo_color'],
					'circle-opacity': 0.28,
					'circle-stroke-color': ['get', 'halo_color'],
					'circle-stroke-width': 1.6
				}
			});
			// Center dot reads worst-case verdict (red for warn AND fail).
			map.addLayer({
				id: 'radar-circles',
				type: 'circle',
				source: 'radars',
				paint: {
					'circle-radius': 6,
					'circle-color': ['get', 'center_color'],
					'circle-stroke-color': strokeFor(),
					'circle-stroke-width': 1.5
				}
			});
			map.addLayer({
				id: 'radar-label',
				type: 'symbol',
				source: 'radars',
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
	}

	onMount(async () => {
		try {
			const resp = await fetch(url('/api/radars/meta'));
			if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
			const all = (await resp.json()) as RadarMeta[];
			// Only the radars we actually monitor — NEXRAD entries
			// aren't covered by L2 status and would render as 'skip'.
			radars = all.filter((r) => r.kind === 'xband' || r.kind === 'cband');

			// Lazy-load maplibre so the ~700 KB chunk doesn't block
			// /m or /m/alarms first paint.
			const mod = await import('maplibre-gl');
			maplibregl = (mod as any).default ?? mod;
			await import('maplibre-gl/dist/maplibre-gl.css');

			map = new maplibregl.Map({
				container: mapDiv,
				style: theme.resolved === 'light' ? STYLE_LIGHT : STYLE_DARK,
				center: [-122.1, 37.7],
				zoom: 6.7,
				attributionControl: false,
				dragRotate: false,
				pitchWithRotate: false,
				touchPitch: false,
				cooperativeGestures: false
			});
			map.on('load', () => refreshSource());
		} catch (e) {
			loadError = (e as Error).message;
		}
	});

	onDestroy(() => map?.remove());

	// Re-paint dots when L2 status updates (status changes per WS / poll).
	$effect(() => {
		statusByRadar;
		if (map && map.isStyleLoaded()) refreshSource();
	});
</script>

<div class="overflow-hidden rounded-md border border-[var(--color-border)] bg-[var(--color-surface)]">
	<div class="px-3 py-2 text-[10px] uppercase tracking-[0.18em] text-[var(--color-muted)] border-b border-[var(--color-border)]">
		radars · geographic view
	</div>
	{#if loadError}
		<div class="px-3 py-4 text-[12px] text-[var(--color-fail)]">map failed: {loadError}</div>
	{:else}
		<div bind:this={mapDiv} class="map-canvas"></div>
	{/if}
</div>

<style>
	.map-canvas {
		width: 100%;
		height: 240px;
	}
</style>
