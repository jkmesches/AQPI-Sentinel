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
	/**
	 * Which side past its range this stop also stands for.
	 *
	 * A renderer that clips paints every value below its minimum in the
	 * first color and every value above its maximum in the last, so those
	 * two stops cover an open band. Absent where the producer's behavior
	 * past the ends was not read from its code.
	 */
	open?: 'below' | 'above';
}

/** One value range a color stands for. */
export interface Band {
	lo: number;
	hi: number;
	open?: 'below' | 'above';
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
	 * The colormap definition this table was generated from, when it was
	 * generated rather than read off a published colorbar.
	 *
	 * Its presence selects the per-stop matching rule in `toleranceAt`. A
	 * table quantised from a continuous colormap has neighbors one channel
	 * step apart, and a listed one may reuse a color for two bands, as the
	 * radar-display reflectivity table does. A single closest-pair
	 * tolerance fails both, the first by admitting nothing and the second
	 * by measuring zero, while a tolerance bounded by value handles each.
	 * Whether the scale reads as bands or as a value is `rampKind`'s call,
	 * from the stop spacing.
	 */
	built_from?: {
		kind: string;
		anchors: string[];
		levels: number;
		vmin: number;
		vmax: number;
	};
	/**
	 * Where each number printed on the bar sits along it, 0 at the
	 * bottom. Present for a segmented axis, whose shape between two
	 * printed numbers is not recoverable from the picture, so the
	 * legend places them from the measurement rather than from a rule.
	 */
	ticks?: { value: number; fraction: number }[];
	/**
	 * How the producer stores a partly transparent pixel.
	 *
	 * "premultiplied" means it wrote the color already multiplied by its
	 * alpha, though PNG holds straight alpha, so an edge pixel reads back
	 * darkened and matches no stop until the alpha is divided out. Set from
	 * a measurement of real frames, recorded in `edges_measured`. Absent
	 * means a pixel is read as stored.
	 */
	edges?: 'premultiplied';
	/** The frames and numbers `edges` was set from. */
	edges_measured?: string;
	/**
	 * Whether the producer resamples after painting, so that where two
	 * bands meet a pixel mixes their colors.
	 *
	 * "resampled" lets `invert` read such a pixel as the span of the two
	 * bands. Set from a measurement of real frames, recorded in
	 * `blends_measured`. Absent means a color off every stop is off the
	 * scale: a producer that paints each band in one exact color, as the
	 * radar renderers do, leaves nothing between them but linework.
	 */
	blends?: 'resampled';
	/** The frames and numbers `blends` was set from. */
	blends_measured?: string;
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

/** Whether a ramp paints in blocks or as a gradient. */
export type RampKind = 'discrete' | 'continuous';

/**
 * Median adjacent-stop distance above which a ramp is painted in blocks.
 *
 * Measured over FLOW's four colorbars on 2026-11-21. The three continuous
 * bars are sampled from a gradient and step by 13 to 17 between neighbors,
 * medians 14 to 15, because extract_ramp.py starts a new stop once the color
 * moves past its BLOCK_TOLERANCE of 12. Reflectivity is painted in blocks and
 * its median step is 148, with no single step under 24.
 *
 * 20 sits above the continuous maximum of 17 and below the smallest discrete
 * step of 24. The median rather than the minimum is the test, since a
 * discrete bar may put two similar blocks side by side, as reflectivity does
 * at #d30000 and #bb0000.
 */
export const DISCRETE_MEDIAN_STEP = 20;

/**
 * Typical color distance between neighboring stops.
 *
 * @param ramp The table to measure.
 * @returns The median summed channel distance, or 0 for a single stop.
 * @raises RangeError If the ramp carries no stops.
 */
export function medianAdjacentStep(ramp: Ramp): number {
	if (ramp.stops.length === 0) throw new RangeError('ramp carries no stops');
	if (ramp.stops.length === 1) return 0;
	const steps: number[] = [];
	for (let i = 1; i < ramp.stops.length; i++) {
		steps.push(colorDistance(
			parseHex(ramp.stops[i - 1].color), parseHex(ramp.stops[i].color)));
	}
	steps.sort((a, b) => a - b);
	const mid = Math.floor(steps.length / 2);
	return steps.length % 2 === 1
		? steps[mid]
		: (steps[mid - 1] + steps[mid]) / 2;
}

/**
 * How a ramp should be drawn, and whether its stops are bands.
 *
 * @param ramp The table to classify.
 * @returns 'discrete' for a bar painted in blocks, 'continuous' otherwise.
 * @raises RangeError If the ramp carries no stops.
 */
export function rampKind(ramp: Ramp): RampKind {
	return medianAdjacentStep(ramp) >= DISCRETE_MEDIAN_STEP
		? 'discrete'
		: 'continuous';
}

/** A value read back from a pixel, with how well the color matched. */
export interface Reading {
	value: number;
	/** The range the stop covers, for a readout that shows a band. */
	lo: number;
	hi: number;
	/** Summed channel distance to the nearest stop. */
	distance: number;
	/** Set when the matched stop is open past one end of the scale. */
	open?: 'below' | 'above';
	/**
	 * Set when the pixel mixes two stops' colors, and `lo` to `hi` then
	 * spans both of their bands.
	 */
	blend?: true;
	/**
	 * The other bands painted in exactly the matched color, ascending.
	 *
	 * Present only where a ramp reuses a color. The radar-display
	 * reflectivity table paints both below -25 dBZ and 5 to 10 dBZ in
	 * #646464, and naming the first band alone would put a confident
	 * wrong number on half of those pixels.
	 */
	also?: Band[];
}

/**
 * The value a pixel was painted from, or null when nothing matches.
 *
 * Null for a pixel that is off the ramp, which is the answer for the map
 * background, a basemap label showing through, or anti-aliasing at the edge
 * of the radar's coverage. A readout that guessed instead would put a
 * confident dBZ on a road name.
 *
 * On a ramp whose producer resamples, a pixel that matches no stop may
 * mix two neighboring bands, and reads as the span of both when nothing
 * else fits (see `blendReading`). On a ramp whose producer premultiplies
 * its edges, a partly transparent pixel read exactly is divided by its
 * alpha first.
 *
 * @param ramp The moment's table.
 * @param rgb The pixel.
 * @param alpha The pixel's alpha, where available. Below TRANSPARENT_ALPHA
 *     the pixel is outside coverage and reads as null.
 * @param options Whether the pixel holds the file's own bytes.
 * @returns The reading, or null.
 */
export function invert(
	ramp: Ramp,
	rgb: Rgb,
	alpha: number = 255,
	options: InvertOptions = {}
): Reading | null {
	if (alpha <= TRANSPARENT_ALPHA) return null;
	const recovered = ramp.edges === 'premultiplied' && alpha < 255;
	// Read as stored, a premultiplied edge is darker than its band and can
	// sit on a darker band's color: the composite's 20 to 25 dBZ green at
	// alpha 200 lands within 3 of its 25 to 30 green. A canvas readback rounds
	// it past any stated bound, so without the file's bytes it reads as
	// nothing.
	if (recovered && options.exact !== true) return null;
	const color = recovered ? unpremultiply(rgb, alpha) : rgb;
	const spread = summedError(channelError(alpha, recovered));
	// A solid pixel keeps the stop test it has always had, whose tolerance
	// already absorbs one write. Only a recovered color carries more.
	const margin = recovered ? spread : 0;
	return stopReading(ramp, color, margin)
		?? blendReading(ramp, color, spread, margin);
}

/**
 * The most a producer's 8-bit write moves one channel, in channel units.
 *
 * One rather than a half, because the composite producer truncates. Of
 * 250,107 pixels in six comp_ref frames of 2026-11-24 that sat between two
 * stops, 75.2% lay within the round-to-nearest bound of the nearest
 * two-stop mix and 97.6% within the bound this sets.
 */
export const WRITE_ERROR = 1;

/** How `invert` should treat the pixel it is given. */
export interface InvertOptions {
	/**
	 * Whether the pixel holds the file's own bytes.
	 *
	 * A 2D canvas stores premultiplied color and rounds a partly
	 * transparent pixel on the way back out, by up to 75 summed channel
	 * units at the faintest edges of one comp_ref frame in Firefox. A
	 * premultiplied edge is recovered only from exact bytes, where the
	 * producer's own writes are the only error left to bound.
	 */
	exact?: boolean;
}

/**
 * How far a pixel's color may sit from the color its producer meant, per
 * channel.
 *
 * A solid pixel carries one write. A color recovered from a premultiplied
 * edge carries two, the color and the alpha, and dividing by the alpha
 * scales both by 255 over it: the color's error directly, and the alpha's
 * through the channel it divides, which is at most 255.
 *
 * @param alpha The pixel's alpha, above TRANSPARENT_ALPHA.
 * @param recovered Whether the color was divided by its alpha.
 * @returns The bound in channel units.
 */
export function channelError(alpha: number, recovered: boolean): number {
	return recovered ? 2 * WRITE_ERROR * (255 / alpha) : WRITE_ERROR;
}

/**
 * The summed channel distance a per-channel error allows.
 *
 * It bounds how far a recovered color sits from the one its producer meant.
 * It also bounds how far a mixed pixel sits from the segment between its two
 * stops: an error of e per channel moves a color at most e√3 in straight
 * line, so the segment's nearest point is no further, and the summed
 * distance to that point is at most √3 times that.
 *
 * @param perChannel The bound per channel.
 * @returns Three times it.
 */
export function summedError(perChannel: number): number {
	return 3 * perChannel;
}

/**
 * The color a premultiplied pixel was painted in, before its alpha.
 *
 * Left unrounded, since the error bound is stated in these units.
 *
 * @param rgb The stored channels.
 * @param alpha Its alpha, above TRANSPARENT_ALPHA.
 * @returns The channels divided by alpha, at most 255.
 */
function unpremultiply(rgb: Rgb, alpha: number): Rgb {
	const k = 255 / alpha;
	return [
		Math.min(255, rgb[0] * k),
		Math.min(255, rgb[1] * k),
		Math.min(255, rgb[2] * k)
	];
}

/** Each ramp's stop colors, parsed once per table. */
const STOP_COLORS = new WeakMap<Ramp, Rgb[]>();

/**
 * A ramp's stop colors as channels.
 *
 * @param ramp The table.
 * @returns One triple per stop, in table order.
 */
function stopColors(ramp: Ramp): Rgb[] {
	let out = STOP_COLORS.get(ramp);
	if (out === undefined) {
		out = ramp.stops.map((s) => parseHex(s.color));
		STOP_COLORS.set(ramp, out);
	}
	return out;
}

/**
 * The stop a color is, when it is one.
 *
 * The nearest stop within its tolerance, as the readout has always read.
 * A margin widens that for a color carrying more error than a solid
 * pixel: every color the margin allows must fall inside the stop's
 * tolerance and read the same, or the answer would turn on an error the
 * pixel cannot show.
 *
 * @param ramp The table.
 * @param color The pixel, recovered where its edge was premultiplied.
 * @param margin The summed error beyond a solid pixel's, 0 for one.
 * @returns The reading, or null.
 */
function stopReading(ramp: Ramp, color: Rgb, margin: number): Reading | null {
	const colors = stopColors(ramp);
	let best = -1;
	let bestDistance = Infinity;
	for (let i = 0; i < colors.length; i++) {
		const d = colorDistance(color, colors[i]);
		if (d < bestDistance) {
			bestDistance = d;
			best = i;
		}
		if (d === 0) break;
	}
	if (best < 0 || bestDistance + margin > toleranceAt(ramp, best)) return null;
	if (margin > 0) {
		// A color within the margin of this one has its nearest stop within
		// twice the margin of the nearest here.
		const reach = bestDistance + 2 * margin;
		for (let i = 0; i < colors.length; i++) {
			if (colorDistance(color, colors[i]) <= reach && !sameReading(ramp, best, i)) {
				return null;
			}
		}
	}
	const stop = ramp.stops[best];
	const reading: Reading = {
		value: stop.value, lo: stop.lo, hi: stop.hi, distance: bestDistance
	};
	if (stop.open) reading.open = stop.open;
	const also = sharedBands(ramp, best);
	if (also.length) reading.also = also;
	return reading;
}

/** Each ramp's discreteness, computed once per table. */
const KINDS = new WeakMap<Ramp, RampKind>();

/**
 * Whether two stops say the same thing to a reader.
 *
 * The same color always does. On a continuous ramp two values within the
 * snap budget do, since that is how far a snapped match may be wrong. On a
 * discrete ramp only the same band does.
 *
 * @param ramp The table.
 * @param i One stop.
 * @param j The other.
 * @returns Whether a reading of either would print the same.
 */
function sameReading(ramp: Ramp, i: number, j: number): boolean {
	const a = ramp.stops[i];
	const b = ramp.stops[j];
	if (a.color.toLowerCase() === b.color.toLowerCase()) return true;
	if (cachedKind(ramp) === 'continuous') {
		return Math.abs(a.value - b.value)
			<= Math.abs(ramp.max - ramp.min) * SNAP_BUDGET;
	}
	return a.lo === b.lo && a.hi === b.hi && a.open === b.open;
}

/**
 * `rampKind`, remembered per table.
 *
 * @param ramp The table.
 * @returns Its kind.
 */
function cachedKind(ramp: Ramp): RampKind {
	let kind = KINDS.get(ramp);
	if (kind === undefined) {
		kind = rampKind(ramp);
		KINDS.set(ramp, kind);
	}
	return kind;
}

/** Two stops whose colors a pixel may mix, and what the mix stands for. */
interface MixPair {
	/** The first stop's color. */
	from: Rgb;
	/** The second stop's color less the first's. */
	step: Rgb;
	/** The step's squared length. */
	length2: number;
	/** Both stops' bands as one, or null when it is open at both ends. */
	span: Band | null;
	/** Whether the two stops are neighbors on the scale. */
	adjacent: boolean;
}

/** Each discrete ramp's pairs, built once per table. */
const MIX_PAIRS = new WeakMap<Ramp, MixPair[]>();

/**
 * Every pair of differently colored stops on a ramp.
 *
 * @param ramp The table.
 * @returns The pairs, each once.
 */
function mixPairs(ramp: Ramp): MixPair[] {
	const cached = MIX_PAIRS.get(ramp);
	if (cached !== undefined) return cached;
	const colors = stopColors(ramp);
	const out: MixPair[] = [];
	for (let i = 0; i < colors.length; i++) {
		for (let j = i + 1; j < colors.length; j++) {
			const step: Rgb = [
				colors[j][0] - colors[i][0],
				colors[j][1] - colors[i][1],
				colors[j][2] - colors[i][2]
			];
			const length2 = step[0] ** 2 + step[1] ** 2 + step[2] ** 2;
			if (length2 === 0) continue;
			out.push({ from: colors[i], step, length2,
			           span: spanOf(ramp.stops[i], ramp.stops[j]), adjacent: j === i + 1 });
		}
	}
	MIX_PAIRS.set(ramp, out);
	return out;
}

/**
 * Two stops' bands as one range.
 *
 * @param s One stop.
 * @param t The other.
 * @returns The range from the lower bottom to the higher top, open where
 *     the stop at that end is, or null when it would be open at both ends
 *     and no band could say it.
 */
function spanOf(s: RampStop, t: RampStop): Band | null {
	const low = s.lo <= t.lo ? s : t;
	const high = s.hi >= t.hi ? s : t;
	const below = low.open === 'below';
	const above = high.open === 'above';
	if (below && above) return null;
	const band: Band = { lo: low.lo, hi: high.hi };
	if (below) band.open = 'below';
	if (above) band.open = 'above';
	return band;
}

/**
 * Summed channel distance from a color to the nearest mix of a pair.
 *
 * The nearest mix in straight-line distance, which is the point
 * `summedError` bounds.
 *
 * @param color The pixel.
 * @param pair The two stops.
 * @returns The distance.
 */
function distanceToMix(color: Rgb, pair: MixPair): number {
	const { from, step } = pair;
	const along = ((color[0] - from[0]) * step[0] + (color[1] - from[1]) * step[1]
		+ (color[2] - from[2]) * step[2]) / pair.length2;
	const t = Math.min(1, Math.max(0, along));
	return Math.abs(color[0] - (from[0] + t * step[0]))
		+ Math.abs(color[1] - (from[1] + t * step[1]))
		+ Math.abs(color[2] - (from[2] + t * step[2]));
}

/**
 * The span of two neighboring bands a pixel mixes, when nothing else fits.
 *
 * The composite producer paints in bands and then resamples the image,
 * so where two bands meet a pixel takes a weighted mix of both colors.
 * Its value lies somewhere across the two bands. The weight says where
 * the pixel sat between two cells, not what either cell held, so the
 * reading is the whole span and never a value interpolated inside it.
 *
 * Only on a ramp whose producer was measured to resample (`blends`), and
 * only a discrete one, whose stops are bands. A continuous ramp's
 * neighbors are already within its tolerance, and a pixel far from all of
 * them is off the ramp rather than between two of its hundreds of stops.
 *
 * Only neighboring bands. A pixel near the mix of two bands further apart
 * is as likely three or more bands mixed, or linework, and the mix of a
 * blue and a red passes within 3 of the composite's purples: read as a
 * span it printed "10 to 65 dBZ" over a -15 to -5 dBZ color.
 *
 * Every pair whose mix comes within the radius could have made the pixel,
 * neighbors or not. Exactly one may, and no stop may be within its own
 * tolerance plus the margin, or the readout would be choosing among
 * answers the pixel cannot tell apart.
 *
 * @param ramp The table.
 * @param color The pixel, recovered where its edge was premultiplied.
 * @param radius The summed error a mix may carry.
 * @param margin The summed error beyond a solid pixel's, 0 for one.
 * @returns The reading, or null.
 */
function blendReading(
	ramp: Ramp, color: Rgb, radius: number, margin: number
): Reading | null {
	if (ramp.blends !== 'resampled' || cachedKind(ramp) !== 'discrete') return null;
	const colors = stopColors(ramp);
	for (let i = 0; i < colors.length; i++) {
		if (colorDistance(color, colors[i]) <= toleranceAt(ramp, i) + margin) return null;
	}
	let found: Band | null = null;
	let nearest = Infinity;
	for (const pair of mixPairs(ramp)) {
		const d = distanceToMix(color, pair);
		if (d > radius) continue;
		if (!pair.adjacent || pair.span === null) return null;
		if (found !== null && !sameBand(found, pair.span)) return null;
		found = pair.span;
		nearest = Math.min(nearest, d);
	}
	if (found === null) return null;
	const reading: Reading = {
		value: (found.lo + found.hi) / 2, lo: found.lo, hi: found.hi,
		distance: nearest, blend: true
	};
	if (found.open) reading.open = found.open;
	return reading;
}

/**
 * Whether two bands are the same range.
 *
 * @param a One band.
 * @param b The other.
 * @returns True when both bounds and the open side agree.
 */
function sameBand(a: Band, b: Band): boolean {
	return a.lo === b.lo && a.hi === b.hi && a.open === b.open;
}

/**
 * The bands of every other stop painted in the same color as one stop.
 *
 * @param ramp The table.
 * @param index The stop that matched.
 * @returns Those stops' bands in table order, empty for a color used once.
 */
function sharedBands(ramp: Ramp, index: number): Band[] {
	const color = ramp.stops[index].color.toLowerCase();
	const out: Band[] = [];
	for (const [i, stop] of ramp.stops.entries()) {
		if (i === index || stop.color.toLowerCase() !== color) continue;
		out.push(stop.open
			? { lo: stop.lo, hi: stop.hi, open: stop.open }
			: { lo: stop.lo, hi: stop.hi });
	}
	return out;
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
	const out = ramp.built_from
		? Math.max(0, ...budgetedTolerances(ramp, colors))
		: Number.isFinite(closest)
			? Math.max(0, Math.min(MATCH_TOLERANCE, closest - 1))
			: MATCH_TOLERANCE;
	TOLERANCES.set(ramp, out);
	return out;
}

/**
 * How much of a ramp's range a snapped match may be wrong by.
 *
 * One percent. A table quantised from a continuous colormap puts its
 * neighbors one channel step apart, so the closest-pair rule above
 * returns zero for it and only an exact pixel reads back. On the water
 * level frames that left 18% of the painted area readable, because the
 * renderer resamples with a Hanning window and most pixels land between
 * two entries rather than on one.
 *
 * Snapping to the nearer entry is safe there in a way it is not for a
 * block ramp, because the two candidates are neighbors on one continuous
 * scale and the answer differs by a band width. This budget says how
 * wide a band width is allowed to be before that stops being true.
 */
export const SNAP_BUDGET = 0.01;

/**
 * The largest tolerance whose worst wrong answer stays inside the budget.
 *
 * Derived from the ramp rather than set: it walks every pair of stops
 * within each candidate tolerance and takes the largest one where no
 * pair differs in value by more than SNAP_BUDGET of the range. A ramp
 * that turns back on itself, so that one color sits near two distant
 * values, gets a small tolerance from this for the same reason a block
 * ramp does.
 *
 * @param ramp The table.
 * @param colors Its stop colors, already parsed.
 * @returns The tolerance in summed channel distance.
 */
function budgetedTolerances(ramp: Ramp, colors: Rgb[]): number[] {
	const span = Math.abs(ramp.max - ramp.min);
	const out = new Array<number>(colors.length).fill(0);
	if (!(span > 0)) return out;
	const allowed = span * SNAP_BUDGET;
	// Per stop, because a colormap can be injective over most of its
	// range and turn back on itself over a little of it. The water level
	// map does: its purples below zero repeat to within one channel
	// step, while its greens and reds, where this data actually sits, do
	// not. One number for the whole table takes the strictest region's
	// answer and applies it everywhere. That is why the water level
	// ramp read back 18% of its pixels while 95% of them sat within
	// distance 5 of a stop.
	//
	// Each stop's distance to the nearest stop outside its budget.
	// Everything closer than that is near enough in value to be safe.
	const limit = new Array<number>(colors.length).fill(MATCH_TOLERANCE + 1);
	for (let i = 0; i < colors.length; i++) {
		for (let j = i + 1; j < colors.length; j++) {
			const d = colorDistance(colors[i], colors[j]);
			if (d > MATCH_TOLERANCE) continue;
			if (Math.abs(ramp.stops[i].value - ramp.stops[j].value) <= allowed) {
				continue;
			}
			if (d < limit[i]) limit[i] = d;
			if (d < limit[j]) limit[j] = d;
		}
	}
	for (let i = 0; i < colors.length; i++) {
		out[i] = Math.max(0, Math.min(MATCH_TOLERANCE, limit[i] - 1));
	}
	return out;
}

/** Each generated ramp's per-stop tolerances, computed once per table. */
const STOP_TOLERANCES = new WeakMap<Ramp, number[]>();

/**
 * How far a pixel may sit from one stop of this ramp and still be it.
 *
 * A table read off a published block bar gets one answer for every
 * stop, since its stops are meant to be told apart and the closest pair
 * bounds them all. A table generated from a renderer's colormap gets an
 * answer per stop. A quantised continuous scale needs that because its
 * neighbors are one channel step apart by construction, and a listed one
 * that reuses a color needs it so that color matches only exactly.
 *
 * @param ramp The table.
 * @param index Which stop matched.
 * @returns The tolerance in summed channel distance.
 */
export function toleranceAt(ramp: Ramp, index: number): number {
	if (!ramp.built_from) return toleranceFor(ramp);
	let per = STOP_TOLERANCES.get(ramp);
	if (per === undefined) {
		per = budgetedTolerances(ramp, ramp.stops.map((s) => parseHex(s.color)));
		STOP_TOLERANCES.set(ramp, per);
	}
	return index >= 0 && index < per.length ? per[index] : 0;
}

/**
 * The fraction of a ramp's own colors that round-trip back to themselves.
 *
 * A ramp whose stops sit closer together than the tolerance cannot tell them
 * apart, and the readout would report whichever came first. Measured per
 * ramp rather than assumed, since the continuous ones are sampled from a
 * gradient and their neighbors are close by construction.
 *
 * A stop that shares its exact color with another counts when the reading
 * names its band among the others, since that is the honest answer for a
 * color the producer reused and the readout prints every band it names.
 *
 * @param ramp The table to check.
 * @returns How many stops invert to their own value, and the total.
 */
export function selfCoverage(ramp: Ramp): { exact: number; total: number } {
	let exact = 0;
	for (const stop of ramp.stops) {
		const back = invert(ramp, parseHex(stop.color));
		if (!back) continue;
		if (back.value === stop.value
			|| (back.also ?? []).some((b) => b.lo === stop.lo && b.hi === stop.hi)) {
			exact++;
		}
	}
	return { exact, total: ramp.stops.length };
}
