/**
 * Where the map opens, and where Reset returns to.
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

export interface SiteMap {
	home: HomeView;
	/** Which overlay toggles to offer. Empty means offer none. */
	overlays: readonly Overlay[];
}

export interface HomeView {
	/** [lon, lat] — MapLibre's order, not lat/lon. */
	center: [number, number];
	zoom: number;
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
export async function resolveHomeView(
	fallback: HomeView,
	fetcher: typeof fetch = fetch,
	url = '/api/version'
): Promise<SiteMap> {
	const miss: SiteMap = { home: fallback, overlays: ALL_OVERLAYS };
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
				: ALL_OVERLAYS
		};
	} catch {
		return miss;
	}
}
