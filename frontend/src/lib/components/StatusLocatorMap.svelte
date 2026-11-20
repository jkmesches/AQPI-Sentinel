<script lang="ts">
	/** Where a failing radar is, and nothing else.
	 *
	 *  The dashboard lost its map when the console moved to /map, and with it
	 *  the only answer to "where is XSCR". This is that answer and no more: no
	 *  imagery, no picker, no playback, no layer panel. An operator who wants
	 *  those opens the console.
	 *
	 *  Markers and rings come from $lib/radarGeo, which the console and the
	 *  mobile map will share once the console is decomposed. Status comes from
	 *  $lib/radarStatus, the one copy of the rule that decides what a pin's
	 *  color means.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { sentinel } from '$lib/stores/state.svelte';
	import { theme } from '$lib/stores/theme.svelte';
	import { url as apiUrl } from '$lib/origin';
	import { resolveHomeView, type HomeView } from '$lib/site';
	import { radarStatusByTarget, radarStatusOf } from '$lib/radarStatus';
	import { radarPointsGeo, radarRangesGeo, type RadarSite, type RadarPaint } from '$lib/radarGeo';

	// The panel header counts what this map drew, so the two cannot disagree.
	// Deriving the count from the rollup instead counted radars with a reading,
	// which on a deployment with no backend tier is six against nine pins: the
	// three NEXRADs are drawn from /api/radars/meta and reported by no stage.
	let { onRadars }: { onRadars?: (radars: RadarSite[]) => void } = $props();

	const STYLE_LIGHT = 'https://tiles.stadiamaps.com/styles/alidade_smooth.json';
	const STYLE_DARK = 'https://tiles.stadiamaps.com/styles/alidade_smooth_dark.json';
	const FALLBACK_HOME: HomeView = { center: [-122.6, 37.95], zoom: 7.2 };

	// Same palette the console uses, so one radar is not two colors on two
	// pages. Duplicated rather than imported because the console still holds
	// its own copy; both move to one place with the decomposition.
	const VERDICT_LIGHT: Record<string, string> = {
		pass: '#16a34a', warn: '#9a6905', fail: '#B91C1C',
		error: '#B91C1C', skip: '#57584C'
	};
	const VERDICT_DARK: Record<string, string> = {
		pass: '#4ade80', warn: '#fbbf24', fail: '#f87171',
		error: '#f87171', skip: '#8a9389'
	};

	let mapDiv: HTMLDivElement;
	let map: maplibregl.Map | undefined;
	let maplibregl: typeof import('maplibre-gl');
	let radars = $state<RadarSite[]>([]);
	let styleReady = $state(false);
	let failed = $state('');
	let resizeObs: ResizeObserver | undefined;

	const radarStatus = $derived(radarStatusByTarget(sentinel.rollup?.stages));

	function paintFor(status: string): RadarPaint {
		const table = theme.resolved === 'light' ? VERDICT_LIGHT : VERDICT_DARK;
		const c = table[status] ?? table.skip;
		return { verdict: c, center: c, halo: c };
	}

	const statusFor = (id: string) => radarStatusOf(radarStatus, id);

	/** Push current status onto the map without rebuilding its sources. */
	function refresh(): void {
		if (!map || !styleReady) return;
		const pts = map.getSource('radars') as maplibregl.GeoJSONSource | undefined;
		const rings = map.getSource('ranges') as maplibregl.GeoJSONSource | undefined;
		pts?.setData(radarPointsGeo(radars, statusFor, paintFor));
		rings?.setData(radarRangesGeo(radars, statusFor, paintFor));
	}

	function addLayers(): void {
		if (!map) return;
		const light = theme.resolved === 'light';
		map.addSource('ranges', {
			type: 'geojson', data: radarRangesGeo(radars, statusFor, paintFor)
		});
		map.addLayer({
			id: 'range-fill', type: 'fill', source: 'ranges',
			paint: { 'fill-color': ['get', 'color'], 'fill-opacity': 0.06 }
		});
		map.addLayer({
			id: 'range-stroke', type: 'line', source: 'ranges',
			paint: {
				'line-color': ['get', 'color'], 'line-width': 1, 'line-opacity': 0.45,
				'line-dasharray': [2, 2]
			}
		});
		map.addSource('radars', {
			type: 'geojson', data: radarPointsGeo(radars, statusFor, paintFor)
		});
		map.addLayer({
			id: 'radar-halo', type: 'circle', source: 'radars',
			paint: {
				'circle-radius': 9, 'circle-color': ['get', 'halo_color'],
				'circle-opacity': 0.25,
				'circle-stroke-color': ['get', 'halo_color'], 'circle-stroke-width': 1.2
			}
		});
		map.addLayer({
			id: 'radar-dot', type: 'circle', source: 'radars',
			paint: {
				'circle-radius': 5, 'circle-color': ['get', 'center_color'],
				'circle-stroke-color': light ? '#ffffff' : '#0c100d',
				'circle-stroke-width': 1.4
			}
		});
		map.addLayer({
			id: 'radar-label', type: 'symbol', source: 'radars',
			layout: {
				'text-field': ['get', 'id'], 'text-size': 9.5,
				'text-offset': [0.85, 0.35], 'text-anchor': 'left',
				'text-font': ['Stadia Regular']
			},
			paint: {
				'text-color': light ? '#1c1f1c' : '#d0d6d0',
				'text-halo-color': light ? '#ffffff' : '#0c100d',
				'text-halo-width': 1.1
			}
		});
	}

	onMount(() => {
		let alive = true;
		(async () => {
			try {
				const resp = await fetch(apiUrl('/api/radars/meta'));
				if (!resp.ok) throw new Error(`radars/meta HTTP ${resp.status}`);
				radars = (await resp.json()) as RadarSite[];
				onRadars?.(radars);
				const site = await resolveHomeView(FALLBACK_HOME, fetch, apiUrl('/api/version'));
				if (!alive) return;

				const mod = await import('maplibre-gl');
				maplibregl = (mod as { default?: typeof import('maplibre-gl') }).default
					?? (mod as typeof import('maplibre-gl'));
				await import('maplibre-gl/dist/maplibre-gl.css');
				if (!alive) return;

				map = new maplibregl.Map({
					container: mapDiv,
					style: theme.resolved === 'light' ? STYLE_LIGHT : STYLE_DARK,
					center: site.home.center,
					zoom: site.home.zoom,
					attributionControl: { compact: true },
					// A locator is read, not driven. Leaving rotation and pitch
					// off keeps a stray trackpad gesture from tilting the one
					// thing on the page that is meant to be glanceable.
					dragRotate: false,
					pitchWithRotate: false,
					touchPitch: false
				});
				map.on('load', () => {
					addLayers();
					styleReady = true;
				});
				// The dashboard's grid resizes this panel as rails grow, and
				// MapLibre does not notice on its own.
				resizeObs = new ResizeObserver(() => map?.resize());
				resizeObs.observe(mapDiv);
			} catch (e) {
				failed = e instanceof Error ? e.message : String(e);
			}
		})();
		return () => { alive = false; };
	});

	onDestroy(() => {
		resizeObs?.disconnect();
		map?.remove();
		map = undefined;
	});

	// Status arrives on the websocket and the poll tick, long after mount.
	$effect(() => {
		void radarStatus;
		void theme.resolved;
		refresh();
	});
</script>

<div class="relative h-full w-full">
	<div bind:this={mapDiv} class="h-full w-full"></div>
	{#if failed}
		<div class="absolute inset-0 flex items-center justify-center px-4 text-center
		            text-[11px] text-[var(--color-muted)]">
			locator unavailable · {failed}
		</div>
	{/if}
</div>
