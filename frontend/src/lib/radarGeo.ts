/**
 * Radar markers and range rings, as GeoJSON.
 *
 * Both maps build these from the same radar list and the same status
 * reducer, and each kept its own copy. That is how `radarStatus.ts` came to
 * exist: two copies of one rule, one of them fixed, and the desktop map
 * drawing green pins on no evidence for months. The geometry is the same
 * shape of hazard, quieter because a wrong ring looks like a right one.
 *
 * Pure and palette-free. Callers pass their own colors, because the map and
 * the dashboard locator resolve theme differently and neither should have to
 * adopt the other's. A runner can therefore reach every line here.
 *
 * `MapView` and `MobileStatusMap` still carry their own copies. This module
 * is where they go when the map is decomposed; the dashboard locator uses it
 * from the start rather than adding a third.
 */

/** The fields of a radar that place it on a map. */
export interface RadarSite {
	id: string;
	lat: number;
	lon: number;
	/** Range in meters, as /api/radars/meta reports it. */
	range_m: number;
	kind: string;
	name?: string;
	/**
	 * Half the side of the published frame in meters, for a radar whose
	 * frame is not its range box. Absent for the X-bands, whose frames span
	 * exactly their range.
	 */
	frame_half_m?: number;
	/**
	 * Whether this deployment can draw the radar's frames. Absent on a
	 * backend that predates the field, where the X-bands and the C-band were
	 * the radars that had frames.
	 */
	scan?: boolean;
}

/** Earth's mean radius in km, which turns ground distance into arc. */
const EARTH_RADIUS_KM = 6371.0;

/**
 * Whether a radar has frames the map can draw, so a click on it selects it.
 *
 * @param site The radar.
 * @returns The backend's answer, or the old kind rule where it gave none.
 */
export function isSelectable(site: RadarSite): boolean {
	return site.scan ?? (site.kind === 'xband' || site.kind === 'cband');
}

/** Colors for one radar, resolved by the caller from its own theme. */
export interface RadarPaint {
	/** Fill and stroke for the range ring, and the legacy single color. */
	verdict: string;
	/** The marker dot, which carries the worst-case verdict. */
	center: string;
	/** The marker halo, which carries the staleness mode. */
	halo: string;
}

/** How a caller turns one radar's status into colors. */
export type PaintFor = (status: string) => RadarPaint;

/**
 * Degrees of latitude and longitude spanned by a radar's range.
 *
 * Longitude is divided by cos(latitude) because a degree of longitude
 * narrows toward the poles. Dropping that term draws a ring too narrow by
 * 21% at 38N, which still looks like a ring.
 *
 * @param lat Latitude in degrees.
 * @param rangeM Range in meters.
 * @returns Span in degrees for each axis.
 */
export function rangeSpanDeg(lat: number, rangeM: number): { dLat: number; dLon: number } {
	const km = rangeM / 1000;
	return { dLat: km / 111, dLon: km / (111 * Math.cos((lat * Math.PI) / 180)) };
}

/**
 * A radar's position in Web Mercator and one distance at the scale there.
 *
 * @param site The radar.
 * @param distanceM A ground distance from the radar in meters.
 * @returns x and y in radians of the unit sphere, and the distance in the
 *     same units at the radar's latitude.
 */
function mercatorAt(site: RadarSite, distanceM: number): { x0: number; y0: number; r: number } {
	const phi = (site.lat * Math.PI) / 180;
	return {
		x0: (site.lon * Math.PI) / 180,
		y0: Math.log(Math.tan(Math.PI / 4 + phi / 2)),
		r: distanceM / 1000 / EARTH_RADIUS_KM / Math.cos(phi)
	};
}

/** Web Mercator y in radians back to latitude in degrees. */
function latOfY(y: number): number {
	return ((2 * Math.atan(Math.exp(y)) - Math.PI / 2) * 180) / Math.PI;
}

/** A box of half width `halfM` centered on a radar in Web Mercator. */
function mercatorBox(site: RadarSite, halfM: number): {
	west: number; east: number; south: number; north: number;
} {
	const { x0, y0, r } = mercatorAt(site, halfM);
	return {
		west: ((x0 - r) * 180) / Math.PI, east: ((x0 + r) * 180) / Math.PI,
		south: latOfY(y0 - r), north: latOfY(y0 + r)
	};
}

/**
 * A closed ring approximating one radar's coverage.
 *
 * A radar whose frame is placed in Web Mercator (one with `frame_half_m`)
 * gets a circle in Mercator too, so the ring follows the edge of the frame
 * as drawn. A ring drawn true to the ground would miss that edge by up to
 * 2.5 km at the north and south of a 200 km NEXRAD frame, because the frame
 * itself cannot be placed true to the ground at its edges (see frameExtent).
 *
 * @param site The radar.
 * @param segments How many segments to draw. Must be at least 3.
 * @returns [lon, lat] pairs, first point repeated last so the ring closes.
 * @raises RangeError If `segments` is below 3, which cannot enclose an area.
 */
export function rangeRingCoords(site: RadarSite, segments = 64): number[][] {
	if (!Number.isInteger(segments) || segments < 3) {
		throw new RangeError(`segments must be an integer of at least 3, got ${segments}`);
	}
	const coords: number[][] = [];
	if (site.frame_half_m !== undefined) {
		const { x0, y0, r } = mercatorAt(site, site.range_m);
		for (let i = 0; i < segments; i++) {
			const a = (i / segments) * 2 * Math.PI;
			coords.push([((x0 + r * Math.cos(a)) * 180) / Math.PI, latOfY(y0 + r * Math.sin(a))]);
		}
		// sin(2*pi) is not exactly zero, so the closing point is copied.
		coords.push([...coords[0]]);
		return coords;
	}
	const { dLat, dLon } = rangeSpanDeg(site.lat, site.range_m);
	for (let i = 0; i <= segments; i++) {
		const a = (i / segments) * 2 * Math.PI;
		coords.push([site.lon + dLon * Math.cos(a), site.lat + dLat * Math.sin(a)]);
	}
	return coords;
}

/**
 * The bounding box of one radar's range, for fitting a camera.
 *
 * @param site The radar.
 * @returns West, east, south and north in degrees, the box of its ring.
 */
export function radarExtent(site: RadarSite): {
	west: number; east: number; south: number; north: number;
} {
	if (site.frame_half_m !== undefined) return mercatorBox(site, site.range_m);
	const { dLat, dLon } = rangeSpanDeg(site.lat, site.range_m);
	return {
		west: site.lon - dLon, east: site.lon + dLon,
		south: site.lat - dLat, north: site.lat + dLat
	};
}

/**
 * The box a radar's frame is placed in.
 *
 * The X-band frames take their range box, as they always have. A frame that
 * names its own size (the NEXRAD plotter's 400 km square) is centered in Web
 * Mercator instead. MapLibre interpolates an image linearly in Mercator, and
 * Mercator stretches toward the pole, so a box centered in latitude puts
 * the frame's middle pixel north of the radar: by 2.5 km for a 200 km half
 * width at KDAX. Centered in Mercator, the middle pixel sits on the radar and
 * the scale there is the true one.
 *
 * Away from the middle, four corners cannot carry the plotter's azimuthal
 * equidistant grid exactly. A point d from the radar lands up to
 * tan(lat) * d^2 / (2R) from its true ground: 0.6 km at 100 km and 2.5 km at
 * the 200 km edge at KDAX, pointing a different way at each bearing. The
 * drift averages to zero around any circle, so centering is the best a
 * four-corner placement can do. Removing it needs the frame warped to
 * Mercator before it is drawn.
 *
 * @param site The radar.
 * @returns West, east, south and north in degrees.
 * @raises RangeError If `frame_half_m` is not a positive finite number.
 */
export function frameExtent(site: RadarSite): {
	west: number; east: number; south: number; north: number;
} {
	const half = site.frame_half_m;
	if (half === undefined) return radarExtent(site);
	if (!Number.isFinite(half) || half <= 0) {
		throw new RangeError(`frame_half_m must be positive, got ${half}`);
	}
	return mercatorBox(site, half);
}

/**
 * One point feature per radar, carrying its status and colors.
 *
 * @param radars The sites to draw.
 * @param statusFor One radar's collapsed status, by id.
 * @param paintFor Colors for a status.
 * @param isActive Whether a radar's overlay is on. Always false where there
 *     is no overlay to turn on, such as the dashboard locator.
 * @returns A FeatureCollection ready for a GeoJSON source.
 */
export function radarPointsGeo(
	radars: readonly RadarSite[],
	statusFor: (id: string) => string,
	paintFor: PaintFor,
	isActive: (id: string) => boolean = () => false
): GeoJSON.FeatureCollection {
	return {
		type: 'FeatureCollection',
		features: radars.map((r) => {
			const status = statusFor(r.id);
			const paint = paintFor(status);
			return {
				type: 'Feature' as const,
				geometry: { type: 'Point' as const, coordinates: [r.lon, r.lat] },
				properties: {
					id: r.id,
					name: r.name ?? r.id,
					kind: r.kind,
					status,
					active: isActive(r.id),
					color: paint.verdict,
					center_color: paint.center,
					halo_color: paint.halo,
					// A NEXRAD site has an overlay only where the deployment
					// reads its frames off the mount, and a click on one
					// without frames has nothing to show.
					clickable: isSelectable(r)
				}
			};
		})
	};
}

/**
 * One polygon feature per radar's range ring.
 *
 * @param radars The sites to draw.
 * @param statusFor One radar's collapsed status, by id.
 * @param paintFor Colors for a status.
 * @param isActive Whether a radar's overlay is on.
 * @returns A FeatureCollection ready for a GeoJSON source.
 */
export function radarRangesGeo(
	radars: readonly RadarSite[],
	statusFor: (id: string) => string,
	paintFor: PaintFor,
	isActive: (id: string) => boolean = () => false
): GeoJSON.FeatureCollection {
	return {
		type: 'FeatureCollection',
		features: radars.map((r) => ({
			type: 'Feature' as const,
			geometry: { type: 'Polygon' as const, coordinates: [rangeRingCoords(r)] },
			properties: {
				id: r.id,
				color: paintFor(statusFor(r.id)).verdict,
				active: isActive(r.id)
			}
		}))
	};
}
