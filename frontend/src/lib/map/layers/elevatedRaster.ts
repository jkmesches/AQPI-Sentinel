/**
 * Radar imagery drawn in the air, rather than painted onto the ground.
 *
 * MapLibre drapes a raster layer onto the terrain mesh, so with 3D
 * terrain on, a reflectivity composite follows the hillsides. That is
 * the wrong reading of the data. A composite is a measurement made
 * aloft and shown in plan; the rain it reports was never on the slope
 * it appears to be lying on, and a reader looking at a pitched view
 * sees precipitation draped over Mount Diablo.
 *
 * So the imagery is drawn as a flat sheet at an altitude instead. The
 * sheet is nominal and says so: a composite has no single height,
 * being a column maximum or a lowest unblocked tilt depending on the
 * product, and this module does not pretend otherwise. What it fixes
 * is the claim the draped version makes without meaning to, that the
 * measurement belongs to the ground surface.
 *
 * The geometry here is pure. The projector is passed in, because
 * MapLibre's MercatorCoordinate needs a browser and the corner
 * arithmetic is what goes wrong.
 */

/** One corner of an image overlay. */
export interface Corner {
	lng: number;
	lat: number;
}

/**
 * The four corners of an image source, in MapLibre's order.
 *
 * Top left, top right, bottom right, bottom left, as `coordinates` on
 * an image source takes them.
 */
export type Corners = readonly [Corner, Corner, Corner, Corner];

/**
 * Turns a ground point at an altitude into mercator space.
 *
 * Under a 3D custom layer the z axis is conformal, so a meter of
 * altitude is the same length as a meter along the ground at that
 * latitude. MapLibre's MercatorCoordinate.fromLngLat does this; the
 * type is here so the geometry can be tested without it.
 */
export type Project = (
	lng: number, lat: number, altitudeM: number
) => [number, number, number];

/**
 * The lowest and highest sheet the control offers, in meters.
 *
 * Zero is the ground, which is the draped behavior this exists to
 * replace, so the floor sits above it. The ceiling is 12 km, above the
 * echo tops of anything this network sees.
 */
export const MIN_ALTITUDE_M = 250;
export const MAX_ALTITUDE_M = 12_000;

/**
 * Where the sheet sits unless the reader moves it.
 *
 * 2 km. A nominal figure, not a measurement: the X-band radars here
 * sample their lowest tilts between roughly half a kilometer and three
 * depending on range, and a composite mixes those. It is chosen to sit
 * clearly above the terrain, which tops out near 1.2 km in this
 * network's view, so the sheet reads as being in the air at any pitch.
 */
export const DEFAULT_ALTITUDE_M = 2000;

/**
 * Hold an altitude inside what the layer will draw.
 *
 * @param meters The altitude asked for.
 * @returns One between the floor and the ceiling.
 */
export function clampAltitude(meters: number): number {
	if (!Number.isFinite(meters)) return DEFAULT_ALTITUDE_M;
	return Math.min(MAX_ALTITUDE_M, Math.max(MIN_ALTITUDE_M, meters));
}

/**
 * Whether four corners describe a quad with area.
 *
 * A degenerate quad draws nothing and costs a GL call per frame, and
 * an overlay whose extent arrived malformed is better skipped than
 * drawn as a line.
 *
 * @param corners The four corners.
 * @returns True when every corner is on the globe and the quad is not
 *     collapsed to a line or a point.
 */
export function isDrawable(corners: Corners): boolean {
	for (const c of corners) {
		if (!Number.isFinite(c.lng) || !Number.isFinite(c.lat)) return false;
		if (Math.abs(c.lat) > 90 || Math.abs(c.lng) > 180) return false;
	}
	const lngs = corners.map((c) => c.lng);
	const lats = corners.map((c) => c.lat);
	return Math.max(...lngs) > Math.min(...lngs)
		&& Math.max(...lats) > Math.min(...lats);
}

/**
 * The two triangles of the sheet, as positions and texture coordinates.
 *
 * Two triangles rather than a strip, because the corners arrive in the
 * ring order an image source uses and a strip would need them
 * reordered. Six vertices is nothing.
 *
 * @param corners The overlay's four corners.
 * @param altitudeM How high to put the sheet, in meters.
 * @param project Ground and altitude to mercator space.
 * @returns Interleaved x, y, z, u, v per vertex, six vertices.
 */
export function sheetVertices(
	corners: Corners, altitudeM: number, project: Project
): Float32Array {
	const high = clampAltitude(altitudeM);
	// The texture's own corners, matching the image source's ring: the
	// image's top left is v = 0 because a decoded image runs downward.
	const uv: [number, number][] = [[0, 0], [1, 0], [1, 1], [0, 1]];
	const at = corners.map(
		(c, i) => [...project(c.lng, c.lat, high), ...uv[i]] as const);
	const out = new Float32Array(6 * 5);
	// Two triangles over the ring: 0-1-2 and 0-2-3.
	const order = [0, 1, 2, 0, 2, 3];
	for (const [slot, corner] of order.entries()) {
		out.set(at[corner], slot * 5);
	}
	return out;
}

/**
 * How far the sheet sits above the highest ground under it.
 *
 * Reported so the console can say when the sheet has been pushed below
 * the terrain, where it would be hidden by the hills it is meant to
 * float over and the reader would think the layer had failed.
 *
 * @param altitudeM The sheet's altitude.
 * @param terrainM The highest ground in view, in meters.
 * @returns The clearance in meters, negative when the sheet is buried.
 */
export function clearance(altitudeM: number, terrainM: number): number {
	if (!Number.isFinite(terrainM)) return clampAltitude(altitudeM);
	return clampAltitude(altitudeM) - terrainM;
}
