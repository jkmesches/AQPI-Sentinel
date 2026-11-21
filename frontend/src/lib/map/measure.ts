/**
 * Distance and bearing between points a reader clicks on the map.
 *
 * Great-circle rather than planar. The console covers the Bay Area at zoom
 * 7 and the X-band network spans about 150 km, where a planar measurement
 * on Web Mercator coordinates is wrong by the secant of the latitude, which
 * is 27% at 38 degrees north. A tool that reports a radar's range 27% long
 * is worse than no tool, because the number looks plausible.
 *
 * Bearings are true. Declination in the Bay Area is about 13 degrees east
 * and changes with time and place, so a magnetic bearing would need a model
 * this console has no reason to carry, and one silently off by 13 degrees
 * is the kind of error that survives review. Every label says "true" so
 * nobody has to guess which it is.
 */

/** A point on the map. */
export interface LngLat {
	lng: number;
	lat: number;
}

/** One leg of a measurement. */
export interface Segment {
	from: LngLat;
	to: LngLat;
	km: number;
	/** Initial bearing in degrees true, or null when the points coincide. */
	bearing: number | null;
}

/** Mean Earth radius, in kilometers (IUGG). */
export const EARTH_RADIUS_KM = 6371.0088;

/** Kilometers in a statute mile. */
export const KM_PER_MILE = 1.609344;

/** Kilometers in a nautical mile. */
export const KM_PER_NM = 1.852;

/** Below this, two points are the same click and have no bearing. */
export const COINCIDENT_KM = 1e-6;

/** The 16 compass points, from north, clockwise. */
export const COMPASS = [
	'N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
	'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'
] as const;

const DEG = Math.PI / 180;

/**
 * Whether a point can be measured from.
 *
 * MapLibre hands back whatever the pointer was over, and a click outside
 * the projected world, or on a map that has just lost its context, has
 * produced NaN here before.
 *
 * @param p The point.
 * @returns True when both coordinates are usable.
 */
export function isUsable(p: LngLat | null | undefined): boolean {
	if (!p) return false;
	return Number.isFinite(p.lng) && Number.isFinite(p.lat)
		&& Math.abs(p.lat) <= 90;
}

/**
 * Great-circle distance between two points.
 *
 * Haversine rather than the spherical law of cosines, which loses precision
 * on short legs: at the 20 m scale a reader can click twice, the cosine
 * form returns zero or noise in double precision.
 *
 * @param a One point.
 * @param b The other.
 * @returns The distance in kilometers, or NaN when either point is unusable.
 */
export function haversineKm(a: LngLat, b: LngLat): number {
	if (!isUsable(a) || !isUsable(b)) return NaN;
	const dLat = (b.lat - a.lat) * DEG;
	const dLng = (b.lng - a.lng) * DEG;
	const lat1 = a.lat * DEG;
	const lat2 = b.lat * DEG;
	const h = Math.sin(dLat / 2) ** 2
		+ Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
	return 2 * EARTH_RADIUS_KM * Math.asin(Math.min(1, Math.sqrt(h)));
}

/**
 * Initial bearing from one point to another.
 *
 * The heading where the leg starts. A great circle changes heading along
 * its length, so there is no single bearing for a leg, and the number a
 * reader wants beside a line is the direction it leaves the first point.
 *
 * @param a The point measured from.
 * @param b The point measured to.
 * @returns Degrees true in [0, 360), or null when the points coincide.
 */
export function initialBearingDeg(a: LngLat, b: LngLat): number | null {
	if (!isUsable(a) || !isUsable(b)) return null;
	if (haversineKm(a, b) < COINCIDENT_KM) return null;
	const lat1 = a.lat * DEG;
	const lat2 = b.lat * DEG;
	const dLng = (b.lng - a.lng) * DEG;
	const y = Math.sin(dLng) * Math.cos(lat2);
	const x = Math.cos(lat1) * Math.sin(lat2)
		- Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLng);
	const deg = Math.atan2(y, x) / DEG;
	return (deg + 360) % 360;
}

/**
 * The compass point a bearing falls in.
 *
 * @param deg Degrees true, or null.
 * @returns One of `COMPASS`, or '' when there is no bearing.
 */
export function compassPoint(deg: number | null): string {
	if (deg === null || !Number.isFinite(deg)) return '';
	const i = Math.round((((deg % 360) + 360) % 360) / 22.5) % 16;
	return COMPASS[i];
}

/**
 * Each leg of a measurement, in order.
 *
 * Unusable points are dropped rather than producing a NaN leg, because one
 * bad click would otherwise make every later total NaN and the tool would
 * read as broken with no way to recover but starting over.
 *
 * @param points The points clicked, in order.
 * @returns One segment per consecutive pair.
 */
export function segments(points: readonly LngLat[]): Segment[] {
	const good = points.filter(isUsable);
	const out: Segment[] = [];
	for (let i = 1; i < good.length; i++) {
		const from = good[i - 1];
		const to = good[i];
		out.push({
			from, to,
			km: haversineKm(from, to),
			bearing: initialBearingDeg(from, to)
		});
	}
	return out;
}

/**
 * The whole path's length.
 *
 * @param points The points clicked, in order.
 * @returns Kilometers, 0 for fewer than two usable points.
 */
export function totalKm(points: readonly LngLat[]): number {
	return segments(points).reduce((sum, s) => sum + s.km, 0);
}

/**
 * A distance in the units a radar operator reads.
 *
 * Kilometers lead, because every range ring, every product extent and every
 * scan strategy in this network is in kilometers. Miles and nautical miles
 * follow, since the operators are in the United States and the aviation
 * users ask in nautical miles.
 *
 * The precision follows the magnitude: 10 m under a kilometer, 100 m under
 * a hundred, and a kilometer above that. Three decimals on a 140 km leg
 * claims a meter of accuracy that a click on a zoom-7 map does not have,
 * where one screen pixel is about 600 m.
 *
 * @param km A distance in kilometers.
 * @returns A label, or '' when the distance is not a number.
 */
export function formatDistance(km: number): string {
	if (!Number.isFinite(km) || km < 0) return '';
	const places = km < 1 ? 2 : km < 100 ? 1 : 0;
	const mi = km / KM_PER_MILE;
	const nm = km / KM_PER_NM;
	return `${km.toFixed(places)} km · ${mi.toFixed(places)} mi `
		+ `· ${nm.toFixed(places)} nm`;
}

/**
 * A bearing as three digits and a compass point.
 *
 * Three digits because that is how a bearing is written everywhere this
 * console's readers come from, and 043 cannot be misread as 43 minutes or
 * 4.3 degrees.
 *
 * @param deg Degrees true, or null.
 * @returns A label, or '' when there is no bearing.
 */
export function formatBearing(deg: number | null): string {
	if (deg === null || !Number.isFinite(deg)) return '';
	const whole = Math.round(((deg % 360) + 360) % 360) % 360;
	return `${String(whole).padStart(3, '0')}° true ${compassPoint(whole)}`;
}

/**
 * The line to draw, as GeoJSON.
 *
 * @param points The points clicked, in order.
 * @returns A LineString feature, or null with fewer than two usable points.
 */
export function measureLine(
	points: readonly LngLat[]
): GeoJSON.Feature<GeoJSON.LineString> | null {
	const good = points.filter(isUsable);
	if (good.length < 2) return null;
	return {
		type: 'Feature',
		properties: {},
		geometry: {
			type: 'LineString',
			coordinates: good.map((p) => [p.lng, p.lat])
		}
	};
}

/**
 * The midpoint of a segment, for placing its label.
 *
 * Along the great circle rather than the average of the coordinates, so the
 * label sits on the line that is drawn rather than beside it. On a 150 km
 * leg the two differ by under a kilometer, but the console also measures
 * across the whole state.
 *
 * @param a One end.
 * @param b The other.
 * @returns The midpoint, or null when either end is unusable.
 */
export function midpoint(a: LngLat, b: LngLat): LngLat | null {
	if (!isUsable(a) || !isUsable(b)) return null;
	const lat1 = a.lat * DEG;
	const lat2 = b.lat * DEG;
	const dLng = (b.lng - a.lng) * DEG;
	const bx = Math.cos(lat2) * Math.cos(dLng);
	const by = Math.cos(lat2) * Math.sin(dLng);
	const lat = Math.atan2(
		Math.sin(lat1) + Math.sin(lat2),
		Math.sqrt((Math.cos(lat1) + bx) ** 2 + by ** 2)
	);
	const lng = a.lng * DEG + Math.atan2(by, Math.cos(lat1) + bx);
	return {
		lng: ((lng / DEG + 540) % 360) - 180,
		lat: lat / DEG
	};
}
