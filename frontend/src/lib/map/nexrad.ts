/**
 * NEXRAD mosaic frames from the Iowa Mesonet WMS-T service.
 *
 * Pure, so a runner can reach it. The projection and the TIME rule are the
 * two things here that look right while being wrong, and neither is visible
 * in a screenshot: a mosaic drawn with a linear latitude term still looks
 * like weather, and a frame with no TIME still returns 200 with a PNG.
 *
 * The caller owns the map. This module answers what to request and where it
 * belongs, and `layers/nexrad.ts` does the requesting.
 */

/** A geographic box, west/east/south/north in degrees. */
export interface Extent {
	west: number;
	east: number;
	south: number;
	north: number;
}

/** Single-image GetMap, which scrubs more smoothly than tiles. */
export const NEXRAD_WMS =
	'https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q-t.cgi';

/**
 * Requested pixel size.
 *
 * Resolution only. MapLibre places an image source by its four corner
 * coordinates, so these numbers decide how much detail the mosaic carries
 * inside that box and never where it sits or how it is shaped.
 */
export const NEXRAD_WIDTH = 1024;
export const NEXRAD_HEIGHT = 1100;

/** The service publishes on 5-minute marks. */
export const NEXRAD_STEP_MS = 5 * 60_000;

/** Half the Web Mercator world, in meters. */
const MERCATOR_HALF_M = 20037508.34;

/**
 * Longitude to Web Mercator x.
 *
 * @param lon Degrees east.
 * @returns Meters from the prime meridian in EPSG:3857.
 */
export function lon2x(lon: number): number {
	return (lon * MERCATOR_HALF_M) / 180;
}

/**
 * Latitude to Web Mercator y.
 *
 * Dropping the logarithm for a linear scale draws a mosaic that still looks
 * like weather while sitting tens of kilometers from where the rain fell.
 *
 * @param lat Degrees north.
 * @returns Meters from the equator in EPSG:3857.
 */
export function lat2y(lat: number): number {
	return (Math.log(Math.tan(((90 + lat) * Math.PI) / 360)) * MERCATOR_HALF_M) / Math.PI;
}

/**
 * The box containing every box given.
 *
 * NEXRAD is context under the composites, so it covers whatever they cover.
 * A profile that supplies no composite extents gets the fallback, since a
 * mosaic has to be placed somewhere and an empty union is not a box.
 *
 * @param boxes The extents to cover.
 * @param fallback The box to use when there are none.
 * @returns The union, or `fallback`.
 */
export function unionExtent(boxes: readonly Extent[], fallback: Extent): Extent {
	if (boxes.length === 0) return fallback;
	return {
		west: Math.min(...boxes.map((b) => b.west)),
		east: Math.max(...boxes.map((b) => b.east)),
		south: Math.min(...boxes.map((b) => b.south)),
		north: Math.max(...boxes.map((b) => b.north))
	};
}

/**
 * The newest slice the service indexes, snapped to a 5-minute mark.
 *
 * No lag is subtracted. The current mark is already served: measured at
 * 23:15Z, a request for 23:15Z returned the same 108,357 bytes as the
 * non-time endpoint's latest. Snapping also holds the URL still for five
 * minutes, which lets the blob cache and the browser cache keep the live
 * frame.
 *
 * @param now Milliseconds since the epoch. Injectable for tests.
 * @returns An ISO 8601 UTC timestamp on a 5-minute boundary.
 */
export function nexradLatestIso(now: number = Date.now()): string {
	const snapped = Math.floor(now / NEXRAD_STEP_MS) * NEXRAD_STEP_MS;
	return `${new Date(snapped).toISOString().slice(0, 19)}Z`;
}

/**
 * A GetMap URL for one mosaic frame.
 *
 * TIME is always sent, never omitted. This is the WMS-T endpoint
 * (`n0q-t.cgi`), and a GetMap with no TIME does not fall back to the newest
 * slice: it answers 200 with a 5 KB near-empty PNG. Measured against the
 * service at the same bbox and size, no TIME paints 0.05% of its pixels,
 * while every 5-minute mark from the current one back to 40 minutes paints
 * 7.7% to 13.8% and returns 67 to 111 KB. The live view is the default and
 * the one an operator compares against, and it showed an empty layer until
 * scrubbing back one step made it appear. That reads as "NEXRAD sees nothing
 * here", which is the one thing a ground-truth layer must never say wrongly.
 *
 * @param extent Where the frame will be placed.
 * @param timeIso The slice to request, or null for the newest.
 * @param now Milliseconds since the epoch, used only when `timeIso` is null.
 * @returns An absolute URL.
 */
export function nexradImageUrl(
	extent: Extent,
	timeIso: string | null,
	now: number = Date.now()
): string {
	const bbox = [
		lon2x(extent.west), lat2y(extent.south),
		lon2x(extent.east), lat2y(extent.north)
	].join(',');
	const params = new URLSearchParams({
		SERVICE: 'WMS',
		VERSION: '1.1.1',
		REQUEST: 'GetMap',
		LAYERS: 'nexrad-n0q-wmst',
		STYLES: '',
		FORMAT: 'image/png',
		TRANSPARENT: 'true',
		SRS: 'EPSG:3857',
		WIDTH: String(NEXRAD_WIDTH),
		HEIGHT: String(NEXRAD_HEIGHT),
		BBOX: bbox,
		TIME: timeIso ?? nexradLatestIso(now)
	});
	return `${NEXRAD_WMS}?${params.toString()}`;
}
