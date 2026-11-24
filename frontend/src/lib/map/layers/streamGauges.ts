/**
 * National Water Model stream gauge sites.
 *
 * Two populations in one layer. A site with status `R` publishes real-time
 * flow and opens a hydrograph when clicked; the rest carry a status and
 * nothing else. The real-time ones are drawn larger, brighter and with a
 * heavier stroke, because a reader looking for a creek's flow needs to see
 * which dots will answer before clicking one.
 */
import type { ExpressionSpecification } from 'maplibre-gl';

import { PALETTE } from '../../palette';
import type { LayerCtx, LayerModule, Theme } from './registry';

export const GAUGE_SOURCE = 'stream-gauges';
export const GAUGE_LAYER = 'stream-gauge-marker';

/** Where the backend proxies the site list. */
export const GAUGES_URL = '/api/upstream/stream_gauges';

/** The status that marks a site as publishing real-time flow. */
export const LIVE_STATUS = 'R';

const LIVE_FILL = { light: '#0a6f9b', dark: '#6ec2e0' };
const BASIC_FILL = { light: '#5d8aa8', dark: '#6e8aa0' };
const STROKE = { light: PALETTE.light.canvas, dark: PALETTE.dark.canvas };

/** One site, as the backend reports it. */
export interface GaugeSite {
	comid: string;
	lat: number;
	lon: number;
	status: string;
}

/**
 * Turn the backend's site list into point features.
 *
 * Only `comid` and `status` are carried into properties. The click handler
 * needs the first to fetch a hydrograph and the paint needs the second, and
 * a feature collection of 468 points is cheaper to hand MapLibre without
 * fields nothing reads.
 *
 * @param sites The site list.
 * @returns A FeatureCollection ready for a GeoJSON source.
 */
export function gaugesToGeo(sites: readonly GaugeSite[]): GeoJSON.FeatureCollection {
	return {
		type: 'FeatureCollection',
		features: sites.map((s) => ({
			type: 'Feature' as const,
			properties: { comid: s.comid, status: s.status },
			geometry: { type: 'Point' as const, coordinates: [s.lon, s.lat] }
		}))
	};
}

/**
 * Fetch the site list.
 *
 * @param fetcher The fetch to use.
 * @returns The sites as features.
 * @raises Error If the upstream answers with a non-OK status.
 */
export async function loadStreamGauges(
	fetcher: typeof fetch = fetch
): Promise<GeoJSON.FeatureCollection> {
	const r = await fetcher(GAUGES_URL);
	if (!r.ok) throw new Error(`${GAUGES_URL} returned HTTP ${r.status}`);
	const j = (await r.json()) as { sites?: GaugeSite[] };
	return gaugesToGeo(j.sites ?? []);
}

const pick = (table: Record<Theme, string>, t: Theme) => table[t];

/** Whether a feature is a real-time site, as a style expression. */
const isLive: ExpressionSpecification = ['==', ['get', 'status'], LIVE_STATUS];

/**
 * The stream-gauge overlay.
 *
 * @param fetcher The fetch to use. Overridable for tests.
 * @returns A module the registry can apply.
 */
export function streamGaugesLayer(fetcher: typeof fetch = fetch): LayerModule {
	let cached: GeoJSON.FeatureCollection | null = null;
	return {
		id: 'streamGauges',
		layers: [GAUGE_LAYER],
		sources: [GAUGE_SOURCE],
		async build(ctx: LayerCtx): Promise<void> {
			// An upstream that is unreachable leaves the toggle on and the
			// layer absent, so the next pass tries again. The registry
			// records the error either way.
			cached ??= await loadStreamGauges(fetcher);
			// `isStyleLoaded()` is deliberately not checked here: it flickers
			// false whenever any other source is processing tiles, which on
			// this map means every time the watershed GeoJSON finishes
			// parsing, and it would race our own additions.
			if (!ctx.alive() || ctx.map.getSource(GAUGE_SOURCE)) return;
			ctx.map.addSource(GAUGE_SOURCE, { type: 'geojson', data: cached });
			ctx.map.addLayer(
				{
					id: GAUGE_LAYER,
					type: 'circle',
					source: GAUGE_SOURCE,
					paint: {
						'circle-radius': ['case', isLive, 5.5, 3],
						'circle-color': ['case', isLive,
							pick(LIVE_FILL, ctx.theme), pick(BASIC_FILL, ctx.theme)],
						'circle-opacity': ['case', isLive, 0.95, 0.6],
						'circle-stroke-color': pick(STROKE, ctx.theme),
						'circle-stroke-width': ['case', isLive, 1.2, 0.6]
					}
				},
				ctx.before(GAUGE_LAYER)
			);
		}
	};
}
