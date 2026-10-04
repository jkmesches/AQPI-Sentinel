/**
 * What this deployment is: its name, its data source, where its map opens and
 * which products it can place.
 *
 * The Bay Area centre and zoom were hard-coded in both map components, so an
 * XQPI instance — watching one radar in Pasadena, 375 km outside both AQPI
 * map extents — opened on an empty stretch of Northern California with its
 * only radar off-screen.
 *
 * The backend knows which network it is serving; the frontend did not. So the
 * profile supplies a home view over /api/version and the components fall back
 * to their own tuned constants when it does not. Deliberately NOT derived from
 * the radar list: fitting AQPI's nine radars plus their range rings centres
 * near 38.5N 121.9W, because the three 100 km NEXRADs drag it ~85 km
 * north-east of the view AQPI has always opened at. Deriving would have been
 * self-maintaining and would also have moved a live deployment's startup view
 * as a side effect of adding a second profile.
 */

/** Regional map furniture. All of AQPI's are Northern California datasets. */
export const ALL_OVERLAYS = ['watersheds', 'reservoirs', 'stream_gauges'] as const;
export type Overlay = (typeof ALL_OVERLAYS)[number];

/** Geographic bounds for a raster overlay, in the shape MapView already uses. */
export interface Extent {
	west: number;
	east: number;
	south: number;
	north: number;
}

export interface Product {
	id: string;
	label: string;
}

export interface SiteMap {
	/** What this deployment calls itself, e.g. "AQPI Sentinel". */
	name: string;
	/** What the header names as the data source. */
	dataSource: string;
	/**
	 * Whether there is an HTTP display tier in front of the data. False means
	 * no radarca AND no radar-display, so controls fed by either — the tilt
	 * elevation picker — have nothing behind them and are hidden.
	 */
	hasRadarca: boolean;
	/**
	 * The products this deployment publishes, when its ids are not the ones
	 * the map's own picker table knows. Null keeps that table.
	 */
	products: Product[] | null;
	home: HomeView;
	/** Which overlay toggles to offer. Empty means offer none. */
	overlays: readonly Overlay[];
	/**
	 * One box applied to every composite this deployment publishes, replacing
	 * the per-product table. Null keeps that table, which is AQPI's.
	 */
	compExtent: Record<string, Extent> | null;
	/**
	 * Which of those extents are assumed rather than sourced, by product id.
	 * A placed overlay is pixel-for-pixel as convincing as a surveyed one, so
	 * the map says which is which rather than letting them look alike.
	 */
	compExtentProvisional: readonly string[];
}

export interface HomeView {
	/** [lon, lat] — MapLibre's order, not lat/lon. */
	center: [number, number];
	zoom: number;
}

const FALLBACK_NAME = 'AQPI Sentinel';
const FALLBACK_SOURCE = 'radarca.engr.colostate.edu';

function looksLikeExtent(v: unknown): v is Extent {
	if (!v || typeof v !== 'object') return false;
	const e = v as Extent;
	return (
		[e.west, e.east, e.south, e.north].every((n) => Number.isFinite(n)) &&
		// A reversed or zero-area box renders as an invisible or mirrored
		// overlay rather than erroring, so reject it here.
		e.west < e.east && e.south < e.north &&
		Math.abs(e.west) <= 180 && Math.abs(e.east) <= 180 &&
		Math.abs(e.south) <= 90 && Math.abs(e.north) <= 90
	);
}

function parseExtents(v: unknown): Record<string, Extent> | null {
	if (!v || typeof v !== 'object' || Array.isArray(v)) return null;
	const out: Record<string, Extent> = {};
	for (const [k, ext] of Object.entries(v as Record<string, unknown>)) {
		if (looksLikeExtent(ext)) out[k] = ext;
	}
	return Object.keys(out).length ? out : null;
}

function looksLikeHomeView(v: unknown): v is HomeView {
	if (!v || typeof v !== 'object') return false;
	const c = (v as HomeView).center;
	const z = (v as HomeView).zoom;
	return (
		Array.isArray(c) &&
		c.length === 2 &&
		// Reject a lat/lon swap rather than opening the map in the Indian
		// Ocean: no inhabited radar site has |lat| > 90, and a swapped pair
		// puts a longitude there.
		Number.isFinite(c[0]) && Math.abs(c[0]) <= 180 &&
		Number.isFinite(c[1]) && Math.abs(c[1]) <= 90 &&
		Number.isFinite(z) && z >= 0 && z <= 22
	);
}

/**
 * Resolve the home view, falling back to `fallback` on anything unexpected.
 *
 * Never throws and never returns a partial view: the map is constructed from
 * the result, so a failed fetch or a malformed payload must degrade to the
 * built-in view rather than leaving the map without a camera. A profile that
 * supplies no home view (every AQPI deployment) also lands on the fallback,
 * which is why that path is the common one rather than the error one.
 */
let _cached: Promise<SiteMap> | null = null;

/**
 * The deployment's identity and map configuration, fetched once.
 *
 * Memoised because four call sites want it — the navbar, the page title, the
 * desktop map and the mobile map — and they must not each issue a request.
 * The result never changes for the life of the page: a profile switch means a
 * restart.
 */
export function getSite(fallback: HomeView = { center: [-122.6, 37.95], zoom: 7.2 }): Promise<SiteMap> {
	if (!_cached) _cached = resolveHomeView(fallback);
	return _cached;
}

export async function resolveHomeView(
	fallback: HomeView,
	fetcher: typeof fetch = fetch,
	url = '/api/version'
): Promise<SiteMap> {
	const miss: SiteMap = {
		name: FALLBACK_NAME, dataSource: FALLBACK_SOURCE, hasRadarca: true,
		products: null, home: fallback, overlays: ALL_OVERLAYS,
		compExtent: null, compExtentProvisional: []
	};
	try {
		const resp = await fetcher(url);
		if (!resp.ok) return miss;
		const body = await resp.json();
		const hv = body?.home_view;
		const ov = body?.map_overlays;
		return {
			home: looksLikeHomeView(hv)
				? { center: [hv.center[0], hv.center[1]], zoom: hv.zoom }
				: fallback,
			// An absent key means an older backend that predates the field, so
			// keep every overlay — the AQPI behaviour. An empty ARRAY is a
			// profile saying it has none, and must be honoured.
			overlays: Array.isArray(ov)
				? ov.filter((o): o is Overlay => (ALL_OVERLAYS as readonly string[]).includes(o))
				: ALL_OVERLAYS,
			// Per product id. Anything that does not parse as a usable box is
			// dropped, so a bad entry costs that one product its overlay
			// rather than the whole table.
			compExtent: parseExtents(body?.comp_extent),
			compExtentProvisional: Array.isArray(body?.comp_extent_provisional)
				? body.comp_extent_provisional.filter((x: unknown) => typeof x === 'string')
				: [],
			name: typeof body?.site_name === 'string' && body.site_name
				? body.site_name : FALLBACK_NAME,
			dataSource: typeof body?.data_source === 'string' && body.data_source
				? body.data_source : FALLBACK_SOURCE,
			hasRadarca: body?.has_radarca !== false,
			products: Array.isArray(body?.products)
				? body.products
						.filter((p: unknown): p is Product =>
							!!p && typeof (p as Product).id === 'string'
							&& typeof (p as Product).label === 'string')
				: null
		};
	} catch {
		return miss;
	}
}
