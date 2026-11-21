/**
 * Turning an overlay pixel back into the value it was painted from.
 *
 * Pure, so a runner can reach it. A readout that is wrong reads exactly like
 * one that is right, which is the whole hazard: a number beside a cursor
 * carries more authority than the picture under it, and nobody checks a
 * number against a color by eye.
 *
 * The ramps in `./data` are read out of the colorbar PNGs the producers
 * write beside their frames, so they are the painting code's own table
 * rather than a transcription of a web page. `validation_tests/visual/
 * ramp_coverage.py` measures each one against real frames before it ships.
 */

/** One color in a ramp, and the values it stands for. */
export interface RampStop {
	/** Lowercase `#rrggbb`, as the colorbar painted it. */
	color: string;
	/** The middle of this stop's range. */
	value: number;
	/** The bottom of its range. */
	lo: number;
	/** The top of its range. */
	hi: number;
	/**
	 * Where this color starts along the bar, 0 at the bottom.
	 *
	 * Carried by a segmented ramp, whose colors are reported as the
	 * band between two printed numbers, so several of them share one
	 * value range and only their measured positions tell them apart on
	 * the legend. Absent elsewhere, where the value places the block.
	 */
	at?: number;
}

/** One moment's color-to-value table. */
export interface Ramp {
	moment: string;
	unit: string;
	/** The colorbar's own axis label, e.g. "Z [dBZ]". */
	label: string;
	min: number;
	max: number;
	/**
	 * How the bar's axis maps position to value.
	 *
	 * Absent means linear, which is every radar moment. The composite
	 * accumulation bars are logarithmic, 0.01 to 20 inches, and placing
	 * their stops linearly would draw the whole gradient in the bottom
	 * tenth of the legend. Inversion does not consult this, since each
	 * stop carries its own bounds.
	 */
	scale?: 'linear' | 'log' | 'segmented';
	/**
	 * Where each number printed on the bar sits along it, 0 at the
	 * bottom. Present for a segmented axis, whose shape between two
	 * printed numbers is not recoverable from the picture, so the
	 * legend places them from the measurement rather than from a rule.
	 */
	ticks?: { value: number; fraction: number }[];
	/** Where the colorbar was read from. */
	source: string;
	/** The date it was read, ISO 8601. */
	read_on: string;
	/** Ascending by value. */
	stops: RampStop[];
}

/**
 * How far apart two colors may be and still count as the same stop.
 *
 * Summed over the three channels. Measured against 8,278 opaque pixels
 * across five frames for each of FLOW's four moments, read on 2026-11-21.
 *
 * The floor is what real frames need. Reflectivity is a discrete ramp and
 * every painted color matched a stop at distance 0. The three continuous
 * ramps are sampled into 56 to 61 stops from a smooth gradient, so a real
 * pixel can land between two of them, and the worst observed distance was
 * 9.
 *
 * The ceiling is the closest pair of distinct stops in any ramp, which is
 * 13. A tolerance at or above that could match a color to a stop it does
 * not belong to, and the readout would print a confident wrong number.
 *
 * 12 sits between the two with headroom on each side. A first guess of 24
 * cleared the floor and broke the ceiling, which is the kind of constant
 * that works on every frame tried and fails on the one that matters.
 */
export const MATCH_TOLERANCE = 12;

/** Pixels at or below this alpha are outside the radar's coverage. */
export const TRANSPARENT_ALPHA = 8;

/** An RGB triple, 0 to 255. */
export type Rgb = [number, number, number];

/**
 * Parse `#rrggbb` into channels.
 *
 * @param hex A six-digit hex color, with or without the hash.
 * @returns The channels.
 * @raises RangeError If the string is not six hex digits.
 */
export function parseHex(hex: string): Rgb {
	const s = hex.startsWith('#') ? hex.slice(1) : hex;
	if (!/^[0-9a-fA-F]{6}$/.test(s)) {
		throw new RangeError(`not a six-digit hex color: ${hex}`);
	}
	return [
		parseInt(s.slice(0, 2), 16),
		parseInt(s.slice(2, 4), 16),
		parseInt(s.slice(4, 6), 16)
	];
}

/**
 * Summed channel distance between two colors.
 *
 * Channel distance rather than a perceptual metric, because the question is
 * whether these are the same painted color, not whether a reader could tell
 * them apart.
 *
 * @param a One color.
 * @param b The other.
 * @returns The sum of the three absolute differences, 0 to 765.
 */
export function colorDistance(a: Rgb, b: Rgb): number {
	return Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]);
}

/** A value read back from a pixel, with how well the color matched. */
export interface Reading {
	value: number;
	/** The range the stop covers, for a readout that shows a band. */
	lo: number;
	hi: number;
	/** Summed channel distance to the nearest stop. */
	distance: number;
}

/**
 * The value a pixel was painted from, or null when nothing matches.
 *
 * Null for a pixel that is off the ramp, which is the answer for the map
 * background, a basemap label showing through, or anti-aliasing at the edge
 * of the radar's coverage. A readout that guessed instead would put a
 * confident dBZ on a road name.
 *
 * @param ramp The moment's table.
 * @param rgb The pixel.
 * @param alpha The pixel's alpha, where available. Below TRANSPARENT_ALPHA
 *     the pixel is outside coverage and reads as null.
 * @returns The reading, or null.
 */
export function invert(
	ramp: Ramp,
	rgb: Rgb,
	alpha: number = 255
): Reading | null {
	if (alpha <= TRANSPARENT_ALPHA) return null;
	let best: RampStop | null = null;
	let bestDistance = Infinity;
	for (const stop of ramp.stops) {
		const d = colorDistance(rgb, parseHex(stop.color));
		if (d < bestDistance) {
			bestDistance = d;
			best = stop;
		}
		if (d === 0) break;
	}
	if (!best || bestDistance > toleranceFor(ramp)) return null;
	return { value: best.value, lo: best.lo, hi: best.hi, distance: bestDistance };
}

/** Each ramp's own tolerance, computed once per table. */
const TOLERANCES = new WeakMap<Ramp, number>();

/**
 * How far a pixel may sit from a stop of this ramp and still be it.
 *
 * MATCH_TOLERANCE was set against the radar ramps, whose closest pair of
 * distinct stops is 13. The product ramps are not all that loose: the
 * forecast rate bars put two stops 7 apart, and a flat 12 there would let
 * a pixel halfway between them match either one and print a confident
 * wrong rate. Reading the spacing off the table keeps the rule the
 * comment above states, a tolerance under the closest pair, without a
 * second constant to maintain beside the data.
 *
 * @param ramp The table.
 * @returns The tolerance in summed channel distance, at most
 *   MATCH_TOLERANCE and always under this ramp's closest distinct pair.
 */
export function toleranceFor(ramp: Ramp): number {
	const cached = TOLERANCES.get(ramp);
	if (cached !== undefined) return cached;
	const colors = ramp.stops.map((s) => parseHex(s.color));
	let closest = Infinity;
	for (let i = 0; i < colors.length; i++) {
		for (let j = i + 1; j < colors.length; j++) {
			const d = colorDistance(colors[i], colors[j]);
			if (d > 0 && d < closest) closest = d;
		}
	}
	const out = Number.isFinite(closest)
		? Math.max(0, Math.min(MATCH_TOLERANCE, closest - 1))
		: MATCH_TOLERANCE;
	TOLERANCES.set(ramp, out);
	return out;
}

/**
 * The fraction of a ramp's own colors that round-trip back to themselves.
 *
 * A ramp whose stops sit closer together than the tolerance cannot tell them
 * apart, and the readout would report whichever came first. Measured per
 * ramp rather than assumed, since the continuous ones are sampled from a
 * gradient and their neighbors are close by construction.
 *
 * @param ramp The table to check.
 * @returns How many stops invert to their own value, and the total.
 */
export function selfCoverage(ramp: Ramp): { exact: number; total: number } {
	let exact = 0;
	for (const stop of ramp.stops) {
		const back = invert(ramp, parseHex(stop.color));
		if (back && back.value === stop.value) exact++;
	}
	return { exact, total: ramp.stops.length };
}
