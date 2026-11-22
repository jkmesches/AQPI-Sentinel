/**
 * Range rings drawn at the height of the imagery they bound.
 *
 * A ring says how far a radar reaches. It was a GeoJSON fill and line,
 * which MapLibre drapes onto the terrain mesh, so with 3D terrain on it
 * followed the hillsides while the scan it bounds floated above them.
 * The ring then crossed its own imagery and read as a separate thing
 * lying on the ground, which is the fault the imagery itself was moved
 * off the ground to fix.
 *
 * Every point of a ring is the same distance from the dish, so the beam
 * is at one height all the way round and the ring is a horizontal
 * circle. That makes this much simpler than the beam surface: one
 * height for the whole ring, and the only real work is the arc length
 * the dashes are cut from.
 *
 * The geometry is pure. The projector is passed in, because MapLibre's
 * MercatorCoordinate needs a browser and the arithmetic is what goes
 * wrong.
 */
import { beamHeight, type Project } from './elevatedRaster';

/** Mean earth radius, for turning meters into degrees. */
const EARTH_RADIUS_M = 6_371_000;

/**
 * How many segments a ring is drawn with.
 *
 * 180 puts a vertex every two degrees. At a 40 km radius each chord is
 * then 1.4 km long and sags 6 m inside the true circle, which is under
 * a tenth of a pixel at any zoom this console reaches.
 */
export const RING_SEGMENTS = 180;

/** One radar's ring, and what it is drawn at. */
export interface Ring {
	/** The dish. */
	lng: number;
	lat: number;
	/** How far the ring sits from it, in meters. */
	radiusM: number;
	/** The radar's own height above sea level, in meters. */
	siteM: number;
	/**
	 * The tilt's elevation angle in degrees, when one is engaged.
	 *
	 * Null for a composite, which has no beam of its own, and the ring
	 * then goes at the nominal sheet altitude like the imagery does.
	 */
	elevationDeg: number | null;
}

/**
 * How high a ring floats, in meters above sea level.
 *
 * @param ring The radar and its radius.
 * @param sheetM The nominal sheet altitude, used when no tilt is on.
 * @returns The height to draw the whole circle at.
 */
export function ringHeight(ring: Ring, sheetM: number): number {
	if (ring.elevationDeg === null) return sheetM;
	return beamHeight(ring.radiusM, ring.elevationDeg, ring.siteM);
}

/**
 * The ring's points on the ground, in degrees, closing the circle.
 *
 * A small-angle conversion rather than a proper geodesic. At 40 km and
 * this latitude the two differ by under a meter, which is far inside
 * the chord error the segment count already accepts, and the ring is a
 * reference mark rather than a measurement.
 *
 * @param ring The radar and its radius.
 * @param segments How many segments to draw.
 * @returns Longitude and latitude per point, first point repeated last.
 */
export function ringPoints(
	ring: Ring, segments: number = RING_SEGMENTS
): [number, number][] {
	const n = Math.max(8, Math.min(1024, Math.trunc(segments) || RING_SEGMENTS));
	const rad = Math.PI / 180;
	const dLat = (ring.radiusM / EARTH_RADIUS_M) / rad;
	const dLng = dLat / Math.max(0.01, Math.cos(ring.lat * rad));
	const out: [number, number][] = [];
	// Bounded by n, which is clamped above.
	for (let i = 0; i <= n; i++) {
		const a = (2 * Math.PI * i) / n;
		out.push([ring.lng + dLng * Math.sin(a), ring.lat + dLat * Math.cos(a)]);
	}
	return out;
}

/**
 * The ring as a line strip, with the arc length the dashes are cut from.
 *
 * @param ring The radar and its radius.
 * @param sheetM The nominal sheet altitude, for a ring with no tilt.
 * @param project Ground and altitude to mercator space.
 * @param segments How many segments to draw.
 * @returns Interleaved x, y, z, s per vertex, where s is the distance
 *     travelled around the ring in meters.
 */
export function ringStrip(
	ring: Ring, sheetM: number, project: Project,
	segments: number = RING_SEGMENTS
): Float32Array {
	const pts = ringPoints(ring, segments);
	const high = ringHeight(ring, sheetM);
	const step = (2 * Math.PI * ring.radiusM) / (pts.length - 1);
	const out = new Float32Array(pts.length * 4);
	// Bounded by the point count, which ringPoints clamps.
	for (const [i, [lng, lat]] of pts.entries()) {
		const [x, y, z] = project(lng, lat, high);
		out[i * 4] = x;
		out[i * 4 + 1] = y;
		out[i * 4 + 2] = z;
		out[i * 4 + 3] = i * step;
	}
	return out;
}

/**
 * The ring's interior as a triangle fan, for the faint fill.
 *
 * The hub sits at the rim's height, since the whole ring is one circle
 * in a horizontal plane. The fill is therefore the flat disc the ring
 * bounds. The draped version drew that same disc. A beam's own surface
 * is a cone whose interior is not flat, and a fill following it would
 * be a different drawing.
 *
 * @param ring The radar and its radius.
 * @param sheetM The nominal sheet altitude, for a ring with no tilt.
 * @param project Ground and altitude to mercator space.
 * @param segments How many segments to draw.
 * @returns Interleaved x, y, z per vertex, hub first.
 */
export function ringFan(
	ring: Ring, sheetM: number, project: Project,
	segments: number = RING_SEGMENTS
): Float32Array {
	const pts = ringPoints(ring, segments);
	const high = ringHeight(ring, sheetM);
	const out = new Float32Array((pts.length + 1) * 3);
	const [hx, hy, hz] = project(ring.lng, ring.lat, high);
	out[0] = hx;
	out[1] = hy;
	out[2] = hz;
	// Bounded by the point count, which ringPoints clamps.
	for (const [i, [lng, lat]] of pts.entries()) {
		const [x, y, z] = project(lng, lat, high);
		out[(i + 1) * 3] = x;
		out[(i + 1) * 3 + 1] = y;
		out[(i + 1) * 3 + 2] = z;
	}
	return out;
}
