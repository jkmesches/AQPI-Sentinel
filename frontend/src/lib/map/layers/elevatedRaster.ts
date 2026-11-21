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

/**
 * The radius a radar beam is bent around, under the 4/3 earth model.
 *
 * A beam in a standard atmosphere refracts downward, so it climbs more
 * slowly than a straight line would. Propagation is modelled as a
 * straight ray over an earth of 4/3 the real radius, which is the
 * convention every radar meteorology text uses and the one the
 * publisher's own range rings assume.
 */
export const EFFECTIVE_EARTH_RADIUS_M = (4 / 3) * 6_371_000;

/**
 * How high a beam is at a given distance from the radar.
 *
 * Two terms. The ray climbs at the tilt angle, and the earth falls away
 * beneath it. At 50 km and half a degree the first gives 436 m and the
 * second 147 m, so dropping the curvature would put the beam a quarter
 * too low at the edge of a scan.
 *
 * @param groundM Distance from the radar along the ground, in meters.
 * @param elevationDeg The tilt angle in degrees.
 * @param siteM The radar's own height above sea level, in meters.
 * @returns The beam's height above sea level, in meters.
 */
export function beamHeight(
	groundM: number, elevationDeg: number, siteM: number
): number {
	if (!Number.isFinite(groundM) || groundM < 0) return siteM;
	if (!Number.isFinite(elevationDeg)) return siteM;
	const base = Number.isFinite(siteM) ? siteM : 0;
	const rise = groundM * Math.tan((elevationDeg * Math.PI) / 180);
	const curve = (groundM * groundM) / (2 * EFFECTIVE_EARTH_RADIUS_M);
	return base + rise + curve;
}

/** Mean earth radius, for ground distances. */
const EARTH_RADIUS_M = 6_371_000;

/**
 * Great-circle distance between two points, in meters.
 *
 * @param aLng First longitude in degrees.
 * @param aLat First latitude.
 * @param bLng Second longitude.
 * @param bLat Second latitude.
 * @returns The distance along the ground.
 */
export function groundDistance(
	aLng: number, aLat: number, bLng: number, bLat: number
): number {
	const rad = Math.PI / 180;
	const dLat = (bLat - aLat) * rad;
	const dLng = (bLng - aLng) * rad;
	const s = Math.sin(dLat / 2) ** 2
		+ Math.cos(aLat * rad) * Math.cos(bLat * rad) * Math.sin(dLng / 2) ** 2;
	return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(s)));
}

/** How a tilt's surface is placed in the air. */
export interface Beam {
	/** The radar's position. */
	lng: number;
	lat: number;
	/** The tilt angle in degrees. */
	elevationDeg: number;
	/** The radar's height above sea level, in meters. */
	siteM: number;
}

/**
 * How finely the beam surface is divided along each edge.
 *
 * 32 gives 2,048 triangles for one tilt. The surface is a smooth
 * paraboloid, so the error from the chords falls as the square of the
 * step: across a 120 km extent, 32 divisions put each chord 3.75 km
 * long and under a meter below the curve it is approximating.
 */
export const BEAM_DIVISIONS = 32;

/**
 * The beam's surface over an extent, as triangles at their own heights.
 *
 * A tilt sweeps a cone. Its height grows with distance from the radar,
 * so a flat quad at one altitude is right at a single range and wrong
 * at every other, by a kilometer at the edge of a 60 km scan at 2
 * degrees. Each vertex is placed at its own beam height instead.
 *
 * @param corners The tilt image's four corners.
 * @param beam Where the radar is and how it is pointed.
 * @param project Ground and altitude to mercator space.
 * @param divisions How many cells along each edge.
 * @returns Interleaved x, y, z, u, v per vertex, six per cell.
 */
export function beamVertices(
	corners: Corners, beam: Beam, project: Project,
	divisions: number = BEAM_DIVISIONS
): Float32Array {
	const n = Math.max(1, Math.min(128, Math.trunc(divisions) || 1));
	// The extent's own axes, read off the ring so a rotated or mirrored
	// extent still walks from its own top left.
	const [tl, tr, , bl] = corners;
	const out = new Float32Array(n * n * 6 * 5);
	let at = 0;
	const put = (u: number, v: number): void => {
		const lng = tl.lng + (tr.lng - tl.lng) * u + (bl.lng - tl.lng) * v;
		const lat = tl.lat + (tr.lat - tl.lat) * u + (bl.lat - tl.lat) * v;
		const d = groundDistance(beam.lng, beam.lat, lng, lat);
		const h = beamHeight(d, beam.elevationDeg, beam.siteM);
		const [x, y, z] = project(lng, lat, h);
		out[at] = x; out[at + 1] = y; out[at + 2] = z;
		out[at + 3] = u; out[at + 4] = v;
		at += 5;
	};
	// Bounded: n is clamped above, so this runs at most 128 x 128.
	for (let i = 0; i < n; i++) {
		for (let j = 0; j < n; j++) {
			const u0 = i / n;
			const u1 = (i + 1) / n;
			const v0 = j / n;
			const v1 = (j + 1) / n;
			put(u0, v0); put(u1, v0); put(u1, v1);
			put(u0, v0); put(u1, v1); put(u0, v1);
		}
	}
	return out;
}
