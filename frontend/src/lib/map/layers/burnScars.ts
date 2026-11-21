/**
 * CAL FIRE burn perimeters, shaded by how long ago each one burned.
 *
 * The age expression and the window filter live in `$lib/map/burnScars`,
 * where a runner reaches them without a map. This module is the layer
 * building that sits on top of them.
 *
 * The perimeter file is per profile, unlike the watershed and reservoir
 * ones: the NorCal and SoCal sets are different data clipped to different
 * boxes. A deployment that has not said which network it is draws nothing,
 * because one region's perimeters on another region's map look exactly as
 * authoritative as the right ones.
 */
import { burnScarAge, burnScarFilter, newestBurnScar } from '$lib/map/burnScars';
import { BURN_SCAR_FILL, BURN_SCAR_LINE, BURN_SCAR_SOURCE } from '$lib/map/burnScars';
import type { LayerCtx, LayerModule, Theme } from './registry';

const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] };

/** Fill, newest to oldest. Reads as fresh burn through to old scar. */
const FILL = {
	light: ['#b4310f', '#8a6a52'],
	dark: ['#ff7a45', '#9c7f66']
} as const;
const LINE = {
	light: ['#8f2409', '#7d5f49'],
	dark: ['#ff8d5c', '#8e735c']
} as const;

/** Where the age ramp reaches its old end, in years. */
const RAMP_YEARS = 5;

/** What the burn-scar module needs that `LayerCtx` does not carry. */
export interface BurnScarOptions {
	/** The deployment's profile slug, which names the perimeter file. */
	profile: string;
	/** The slider's window, in years. */
	years: () => number;
	/** Called with the newest ignition date once a file has loaded. */
	onLoaded?: (through: string) => void;
	/** Overridable for tests. */
	fetcher?: typeof fetch;
}

/**
 * The perimeter file for one profile.
 *
 * @param profile The deployment's profile slug.
 * @returns A path under the frontend's static tree.
 */
export function burnScarUrl(profile: string): string {
	return `/data/burn-scars-${profile}.json`;
}

/**
 * Fetch the perimeters, answering empty rather than throwing.
 *
 * A deployment whose file has not been generated draws nothing. The file is
 * also checked for a `features` array before it is cached, so a document
 * that parses as JSON without being a FeatureCollection is never handed to
 * `addSource` on a later call.
 *
 * @param profile The deployment's profile slug. Empty draws nothing.
 * @param fetcher The fetch to use.
 * @returns The collection, or an empty one.
 */
export async function loadBurnScars(
	profile: string,
	fetcher: typeof fetch = fetch
): Promise<GeoJSON.FeatureCollection> {
	if (!profile) return EMPTY;
	try {
		const r = await fetcher(burnScarUrl(profile));
		if (!r.ok) return EMPTY;
		const data = (await r.json()) as GeoJSON.FeatureCollection;
		if (!Array.isArray(data?.features)) return EMPTY;
		return data;
	} catch {
		// A missing or malformed file draws nothing. Every other layer on
		// the map still builds, which is the behavior this module exists in
		// a registry to get.
		return EMPTY;
	}
}

const ramp = (table: Record<Theme, readonly string[]>, t: Theme) => table[t];

/**
 * The burn-scar overlay.
 *
 * @param opts The profile, the window, and where to report the newest date.
 * @returns A module the registry can apply.
 */
export function burnScarsLayer(opts: BurnScarOptions): LayerModule {
	let cached: GeoJSON.FeatureCollection | null = null;
	return {
		id: 'burnScars',
		// The line is removed before the fill, and both before the source,
		// because MapLibre refuses to drop a source a layer still holds.
		layers: [BURN_SCAR_LINE, BURN_SCAR_FILL],
		sources: [BURN_SCAR_SOURCE],
		async build(ctx: LayerCtx): Promise<void> {
			if (!cached) {
				cached = await loadBurnScars(opts.profile, opts.fetcher ?? fetch);
				opts.onLoaded?.(newestBurnScar(cached));
			}
			if (!ctx.alive() || ctx.map.getSource(BURN_SCAR_SOURCE)) return;
			ctx.map.addSource(BURN_SCAR_SOURCE, { type: 'geojson', data: cached });

			const age = burnScarAge();
			const filter = burnScarFilter(opts.years());
			const fill = ramp(FILL, ctx.theme);
			const line = ramp(LINE, ctx.theme);
			// The fill stays faint because these overlap heavily: 392
			// perimeters in the XQPI box, many nested inside a larger fire's
			// footprint. At the opacity an isolated polygon wants, a stack of
			// four reads as solid and hides the composite underneath. The
			// outline carries the shape.
			ctx.map.addLayer(
				{
					id: BURN_SCAR_FILL,
					type: 'fill',
					source: BURN_SCAR_SOURCE,
					filter,
					// Newer scars draw above older ones, so a fresh burn
					// inside an old footprint is not buried by it.
					layout: { 'fill-sort-key': ['-', 0, age] },
					paint: {
						'fill-color': ['interpolate', ['linear'], age,
							0, fill[0], RAMP_YEARS, fill[1]],
						'fill-opacity': ['interpolate', ['linear'], age,
							0, 0.3, RAMP_YEARS, 0.1]
					}
				},
				ctx.before(BURN_SCAR_FILL)
			);
			ctx.map.addLayer(
				{
					id: BURN_SCAR_LINE,
					type: 'line',
					source: BURN_SCAR_SOURCE,
					filter,
					layout: { 'line-sort-key': ['-', 0, age] },
					paint: {
						'line-color': ['interpolate', ['linear'], age,
							0, line[0], RAMP_YEARS, line[1]],
						'line-width': ['interpolate', ['linear'], age,
							0, 1.5, RAMP_YEARS, 0.9],
						'line-opacity': ['interpolate', ['linear'], age,
							0, 0.9, RAMP_YEARS, 0.45]
					}
				},
				ctx.before(BURN_SCAR_LINE)
			);
		}
	};
}
