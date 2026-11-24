/**
 * Watershed outlines and reservoir markers.
 *
 * Both sit under the radar markers and stay muted against the basemap in
 * either theme, because they frame the rainfall drawn over them rather than
 * being the subject of the map.
 *
 * Each is its own module. They shared one `refreshGeography` before, and the
 * watershed branch's mid-fetch bail returned out of the whole function, so a
 * theme swap at the wrong moment left the reservoirs unbuilt until something
 * else triggered a rebuild.
 */
import { PALETTE } from '../../palette';
import type { LayerCtx, LayerModule, Theme } from './registry';

/** Where each file lives, served from the frontend's static tree. */
export const WATERSHEDS_URL = '/data/watersheds-huc8-norcal.geojson';
export const RESERVOIRS_URL = '/data/reservoirs-norcal.json';

/**
 * Fetch and cache one collection.
 *
 * The files are static and a few hundred kilobytes, and the overlays are
 * rebuilt on every theme swap, so refetching on each would spend the network
 * for nothing.
 */
function cachedLoader(url: string): (fetcher: typeof fetch) => Promise<GeoJSON.FeatureCollection> {
	let cached: GeoJSON.FeatureCollection | null = null;
	return async (fetcher: typeof fetch) => {
		if (cached) return cached;
		const r = await fetcher(url);
		if (!r.ok) throw new Error(`${url} returned HTTP ${r.status}`);
		cached = (await r.json()) as GeoJSON.FeatureCollection;
		return cached;
	};
}

const loadWatersheds = cachedLoader(WATERSHEDS_URL);
const loadReservoirs = cachedLoader(RESERVOIRS_URL);

/** Theme-keyed colors, kept beside the layer that paints with them. */
const WATERSHED_LINE = { light: '#3f6478', dark: '#5a8aa0' };
const RESERVOIR_DOT = { light: '#1f6d8e', dark: '#5fa8c4' };
const RESERVOIR_TEXT = { light: '#1f6d8e', dark: '#9bc4d2' };
const HALO = { light: PALETTE.light.canvas, dark: PALETTE.dark.canvas };

const pick = (table: Record<Theme, string>, t: Theme) => table[t];

/**
 * HUC-8 watershed boundaries.
 *
 * Thin and faint. They cover the whole domain, and at full weight they read
 * as the subject rather than as the reference frame for the rainfall drawn
 * over them.
 */
export const watershedsLayer: LayerModule = {
	id: 'watersheds',
	layers: ['watershed-line'],
	sources: ['watersheds'],
	async build(ctx: LayerCtx): Promise<void> {
		const data = await loadWatersheds(fetch);
		// The theme may have swapped while the fetch was in flight, which
		// replaces the style and with it every source this would attach to.
		if (!ctx.alive() || ctx.map.getSource('watersheds')) return;
		ctx.map.addSource('watersheds', { type: 'geojson', data });
		ctx.map.addLayer(
			{
				id: 'watershed-line',
				type: 'line',
				source: 'watersheds',
				paint: {
					'line-color': pick(WATERSHED_LINE, ctx.theme),
					'line-width': 0.7,
					'line-opacity': 0.4
				}
			},
			ctx.before('watershed-line')
		);
	}
};

/**
 * Major dams, as a marker and its name.
 *
 * Two layers over one source, and the label is removed first, because
 * MapLibre refuses to drop a source that any layer still references.
 */
export const reservoirsLayer: LayerModule = {
	id: 'reservoirs',
	layers: ['reservoir-label', 'reservoir-marker'],
	sources: ['reservoirs'],
	async build(ctx: LayerCtx): Promise<void> {
		const data = await loadReservoirs(fetch);
		if (!ctx.alive() || ctx.map.getSource('reservoirs')) return;
		ctx.map.addSource('reservoirs', { type: 'geojson', data });
		ctx.map.addLayer(
			{
				id: 'reservoir-marker',
				type: 'circle',
				source: 'reservoirs',
				paint: {
					'circle-radius': 3.5,
					'circle-color': pick(RESERVOIR_DOT, ctx.theme),
					'circle-stroke-color': pick(HALO, ctx.theme),
					'circle-stroke-width': 1,
					'circle-opacity': 0.9
				}
			},
			ctx.before('reservoir-marker')
		);
		ctx.map.addLayer(
			{
				id: 'reservoir-label',
				type: 'symbol',
				source: 'reservoirs',
				layout: {
					'text-field': ['get', 'name'],
					'text-size': 12,
					'text-offset': [0.6, 0.3],
					'text-anchor': 'left',
					'text-font': ['Stadia Regular']
				},
				paint: {
					'text-color': pick(RESERVOIR_TEXT, ctx.theme),
					'text-halo-color': pick(HALO, ctx.theme),
					'text-halo-width': 1
				}
			},
			ctx.before('reservoir-label')
		);
	}
};
