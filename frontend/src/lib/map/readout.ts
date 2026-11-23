/**
 * Turning a cursor position over a raster overlay into the value beneath it.
 *
 * Two steps, kept apart because only the first is geometry. `overlayPixel`
 * says which pixel of the overlay image the cursor is over, and `invert` in
 * `./ramps/invert` says what color that pixel was painted from. The caller
 * does the sampling, since reading a canvas is not a pure operation.
 *
 * MapLibre places an image source by its four corners and interpolates
 * between them in Web Mercator, so the inverse is linear in Mercator and not
 * in latitude. Interpolating in latitude instead is wrong by kilometers at
 * the top of a Bay Area overlay, which is enough to read a cell's value off
 * the clear air beside it.
 */

import { lon2x, lat2y } from './nexrad';
import { TRANSPARENT_ALPHA, type Band, type Ramp, type Reading } from './ramps/invert';
import { rampKind } from './ramps/legend';

/** The four corners of a placed image, clockwise from the north-west. */
export type ImageCorners = readonly [
	readonly [number, number],
	readonly [number, number],
	readonly [number, number],
	readonly [number, number]
];

/** Where a cursor falls inside an overlay, as fractions of its size. */
export interface OverlayFraction {
	/** 0 at the west edge, 1 at the east. */
	u: number;
	/** 0 at the north edge, 1 at the south. */
	v: number;
}

/**
 * Latitudes outside this cannot be projected, so a cursor there reads
 * nothing. Web Mercator is undefined at the poles and the usual cutoff keeps
 * the projection square.
 */
export const MERCATOR_LIMIT_DEG = 85.051129;

/**
 * How far from a rectangle a quad's corners may sit and still be treated as
 * one, in Mercator meters.
 *
 * Every overlay this console places comes from `extentCorners`, which builds
 * an axis-aligned box, so in practice the deviation is zero. The check is
 * here because a rotated or sheared quad would need a projective inverse,
 * and reading one with the rectangular inverse gives a plausible wrong pixel
 * rather than an error. One meter is far below the size of an overlay pixel,
 * which is hundreds of meters across.
 */
export const RECTANGLE_TOLERANCE_M = 1;

/**
 * Where a longitude and latitude fall inside a placed overlay.
 *
 * Null when the point is outside the overlay, beyond the projection's
 * latitude limit, or not finite. Null is also the answer for a quad that is
 * not a rectangle in Mercator, since this inverse would otherwise report a
 * confident wrong pixel.
 *
 * @param corners The overlay's corners, clockwise from the north-west.
 * @param lon Degrees east.
 * @param lat Degrees north.
 * @returns The fractions, or null.
 * @raises RangeError If the corners cannot be projected or describe no area.
 */
export function overlayFraction(
	corners: ImageCorners,
	lon: number,
	lat: number
): OverlayFraction | null {
	if (!Number.isFinite(lon) || !Number.isFinite(lat)) return null;
	if (Math.abs(lat) > MERCATOR_LIMIT_DEG) return null;

	const [nw, ne, se, sw] = corners;
	for (const [cLon, cLat] of corners) {
		if (!Number.isFinite(cLon) || !Number.isFinite(cLat)
			|| Math.abs(cLat) > MERCATOR_LIMIT_DEG) {
			throw new RangeError(`corner ${cLon},${cLat} cannot be projected`);
		}
	}
	const west = lon2x(nw[0]);
	const east = lon2x(ne[0]);
	const north = lat2y(nw[1]);
	const south = lat2y(sw[1]);
	if (!(east > west) || !(north > south)) {
		throw new RangeError(
			`corners describe no area: ${west}..${east}, ${south}..${north}`);
	}
	// A sheared or rotated quad needs a projective inverse, which this is
	// not. Saying so beats answering with the wrong pixel.
	if (Math.abs(lon2x(sw[0]) - west) > RECTANGLE_TOLERANCE_M
		|| Math.abs(lon2x(se[0]) - east) > RECTANGLE_TOLERANCE_M
		|| Math.abs(lat2y(ne[1]) - north) > RECTANGLE_TOLERANCE_M
		|| Math.abs(lat2y(se[1]) - south) > RECTANGLE_TOLERANCE_M) {
		return null;
	}

	const u = (lon2x(lon) - west) / (east - west);
	const v = (north - lat2y(lat)) / (north - south);
	if (u < 0 || u > 1 || v < 0 || v > 1) return null;
	return { u, v };
}

/** A pixel of the overlay image. */
export interface OverlayPixel {
	x: number;
	y: number;
}

/**
 * Which pixel of the overlay image a fraction lands on.
 *
 * The last row and column are reachable, since a fraction of exactly 1 is
 * the overlay's own edge rather than one pixel past it.
 *
 * @param f Where the cursor falls.
 * @param width The image's width in pixels.
 * @param height Its height.
 * @returns The pixel.
 * @raises RangeError If the size is not a positive whole number of pixels.
 */
export function overlayPixel(
	f: OverlayFraction,
	width: number,
	height: number
): OverlayPixel {
	if (!Number.isInteger(width) || width <= 0
		|| !Number.isInteger(height) || height <= 0) {
		throw new RangeError(`image is ${width} by ${height}`);
	}
	return {
		x: Math.min(width - 1, Math.floor(f.u * width)),
		y: Math.min(height - 1, Math.floor(f.v * height))
	};
}

/**
 * How many decimals a band's bounds need.
 *
 * A band narrower than 10 keeps a decimal, since bins measured off a bar,
 * such as FLOW's reflectivity, sit on bounds like -25.2. Rounding those to
 * whole numbers would print a tidier band than the measurement supports.
 *
 * @param width The band's width in its own unit.
 * @returns A count from 0 to 2.
 */
export function bandDecimals(width: number): number {
	if (!Number.isFinite(width) || width <= 0) return 1;
	if (width >= 10) return 0;
	if (width >= 1) return 1;
	return 2;
}

/**
 * What to print beside the cursor.
 *
 * A discrete ramp reports the band, since that is all the overlay carries. A
 * pixel in the 20 to 25 dBZ block says nothing finer than the block. A
 * sampled gradient reports the single value its stop stands for, because its
 * bands are an artifact of the sampling rather than the producer's own bins.
 *
 * @param ramp The table the reading came from.
 * @param reading What `invert` returned, or null.
 * @returns The text, or null when there is nothing to say.
 */
export const OFF_SCALE = 'not on this scale';

/**
 * What a cursor found at one pixel.
 *
 * Three outcomes. A transparent pixel is outside the product's
 * coverage and the console says nothing, which is right. An opaque
 * pixel that matches no stop is a different case, where the frame is
 * painted there and the paint is not on this table. The composite PNGs carry
 * their own basemap linework and a near-black background at full alpha,
 * so most of a frame's opaque area is that rather than data.
 *
 * Telling the two apart is what stops the readout looking broken.
 * Reporting nothing for both made the coverage read as spotty, and a
 * reader who cannot tell "no data here" from "I cannot read this"
 * blames their own pointing.
 *
 * @param ramp The table to read through.
 * @param rgba The pixel, with its alpha.
 * @param read What `invert` made of it.
 * @returns The text, or null where there is genuinely nothing.
 */
export function readoutFor(
	ramp: Ramp, rgba: readonly [number, number, number, number],
	read: Reading | null
): string | null {
	if (rgba[3] <= TRANSPARENT_ALPHA) return null;
	return readoutText(ramp, read) ?? OFF_SCALE;
}

/**
 * Why a discrete reading names a band rather than a value.
 *
 * Shown where the reader meets the band, since that is where the
 * question comes up. The overlay is a picture, and the producer
 * quantized the field into these colors before the console ever saw
 * it, so a single number would be invented precision.
 *
 * @param ramp The table the reading came from.
 * @returns A sentence, or null for a scale that reports a value.
 */
export function readoutHint(ramp: Ramp): string | null {
	if (rampKind(ramp) === 'continuous') return null;
	return `This overlay is painted in ${ramp.stops.length} bands, so a `
		+ 'pixel says which band it is in. The producer quantized the '
		+ 'field into those colors before publishing it, and a single '
		+ 'number here would be precision the picture does not carry.';
}

/**
 * A number with its unit, or the bare number for a dimensionless one.
 *
 * The correlation coefficient has no unit, and appending an empty one
 * left a trailing space inside the readout chip.
 *
 * @param n The formatted number.
 * @param unit The ramp's unit, which may be empty.
 * @returns What the chip shows.
 */
function withUnit(n: string, unit: string): string {
	return unit ? `${n} ${unit}` : n;
}

export function readoutText(ramp: Ramp, reading: Reading | null): string | null {
	if (!reading) return null;
	if (rampKind(ramp) === 'continuous') {
		// The ramp's own resolution, which is its span over its stop count:
		// 1.07 m/s for velocity, 0.098 dB for ZDR, 5.9 degrees for PhiDP.
		// Printing finer than that claims precision the overlay does not
		// carry, and a velocity reported as -10.57 m/s off a bar that
		// resolves to about one is a number with two invented digits.
		const step = Math.abs(ramp.max - ramp.min) / ramp.stops.length;
		return withUnit(reading.value.toFixed(bandDecimals(step)), ramp.unit);
	}
	const d = bandDecimals(reading.hi - reading.lo);
	const own: Band = reading.open
		? { lo: reading.lo, hi: reading.hi, open: reading.open }
		: { lo: reading.lo, hi: reading.hi };
	const bands = [own, ...(reading.also ?? [])].sort((a, b) => a.lo - b.lo);
	return bands.map((b) => bandText(b, d, ramp.unit)).join(' or ');
}

/**
 * One band as the chip prints it.
 *
 * An open band names only its inner bound, because the renderer painted
 * everything past it in the same color and a closed range there would
 * claim the field stops where the scale does.
 *
 * @param band The band.
 * @param d Decimals for its bounds.
 * @param unit The ramp's unit, which may be empty.
 * @returns The text, e.g. "below -25.0 dBZ", "80.0 dBZ or more" or
 *     "5.0 to 10.0 dBZ".
 */
function bandText(band: Band, d: number, unit: string): string {
	if (band.open === 'below') return `below ${withUnit(band.hi.toFixed(d), unit)}`;
	if (band.open === 'above') return `${withUnit(band.lo.toFixed(d), unit)} or more`;
	return `${band.lo.toFixed(d)} to ${withUnit(band.hi.toFixed(d), unit)}`;
}
