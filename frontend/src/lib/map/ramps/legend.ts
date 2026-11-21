/**
 * Geometry for the legend bar, kept apart from the SVG that draws it.
 *
 * Positions come out as fractions of the bar from 0 at the minimum to 1 at
 * the maximum, so the same numbers serve a vertical bar on a desktop and a
 * horizontal one at phone width. The component decides which axis they land
 * on and how long the bar is.
 *
 * Tick spacing is the caller's to supply, in pixels, because only the
 * component knows its font. Nothing here embeds a guess about label height.
 */

import type { Ramp } from './invert';
import { parseHex, colorDistance } from './invert';

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
 * The most ticks one bar may ask for.
 *
 * A 4K display is 3840 px wide and a label needs several pixels per
 * character, so a thousand ticks is already two orders of magnitude past any
 * bar that can be drawn. A step computed from a bad measurement therefore
 * fails with its numbers in the message rather than filling memory. A step
 * of 1e-6 across differential phase did the latter once.
 */
export const MAX_TICKS = 1000;

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
 * How a ramp should be drawn.
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

/**
 * Where a value sits along the bar.
 *
 * Clamped, so a value past either end draws at that end rather than off the
 * bar. The caller gets no signal that it clamped, which is correct here: the
 * legend's job is to show the ramp, and a value outside it has no place on
 * the bar at all.
 *
 * @param ramp The table.
 * @param value The value to place.
 * @returns A fraction from 0 at the minimum to 1 at the maximum.
 * @raises RangeError If the ramp's bounds are not a usable span, or the value
 *     is not finite.
 */
export function fractionFor(ramp: Ramp, value: number): number {
	const span = ramp.max - ramp.min;
	if (!Number.isFinite(span) || span <= 0) {
		throw new RangeError(`ramp spans ${ramp.min} to ${ramp.max}`);
	}
	if (!Number.isFinite(value)) {
		throw new RangeError(`value is ${value}`);
	}
	return Math.min(1, Math.max(0, (value - ramp.min) / span));
}

/** One painted block of a discrete bar. */
export interface LegendBlock {
	color: string;
	/** Fraction of the bar where the block starts. */
	from: number;
	/** Fraction where it ends. */
	to: number;
	lo: number;
	hi: number;
}

/**
 * The blocks of a discrete bar, in bar order.
 *
 * Drawn at the bounds the colorbar was read at rather than at rounded ones,
 * so the picture matches the frames. The tick labels round instead.
 *
 * Interior edges land midway between one block's `hi` and the next block's
 * `lo`, because those two numbers are the centers of adjacent rows in the
 * colorbar and the boundary between the blocks runs between them. Drawing
 * each block at its measured bounds instead leaves one row of background
 * showing at every edge, which on reflectivity is 0.4 dBZ and comes to 1.5 px
 * of hairline on a 400 px bar.
 *
 * @param ramp The table.
 * @returns One block per stop, ascending and edge to edge.
 * @raises RangeError If the ramp's bounds are not a usable span.
 */
export function legendBlocks(ramp: Ramp): LegendBlock[] {
	return ramp.stops.map((s, i) => {
		const prev = ramp.stops[i - 1];
		const next = ramp.stops[i + 1];
		return {
			color: s.color,
			from: i === 0 ? 0 : fractionFor(ramp, (prev.hi + s.lo) / 2),
			to: i === ramp.stops.length - 1
				? 1
				: fractionFor(ramp, (s.hi + next.lo) / 2),
			lo: s.lo,
			hi: s.hi
		};
	});
}

/** One stop of an SVG gradient. */
export interface GradientStop {
	color: string;
	/** Fraction of the bar, 0 to 1. */
	offset: number;
}

/**
 * The gradient stops of a continuous bar.
 *
 * Placed at each stop's own center, so a bar whose sampling was uneven still
 * draws its colors where they were read.
 *
 * @param ramp The table.
 * @returns One gradient stop per ramp stop, ascending by offset.
 * @raises RangeError If the ramp's bounds are not a usable span.
 */
export function legendGradient(ramp: Ramp): GradientStop[] {
	return ramp.stops.map((s) => ({
		color: s.color,
		offset: fractionFor(ramp, s.value)
	}));
}

/**
 * The smallest 1, 2 or 5 times a power of ten that is at least a raw step.
 *
 * @param raw The step the available room implies.
 * @returns A step from the 1-2-5 sequence.
 * @raises RangeError If the raw step is not a positive finite number.
 */
export function niceStep(raw: number): number {
	if (!Number.isFinite(raw) || raw <= 0) {
		throw new RangeError(`step is ${raw}`);
	}
	const power = Math.pow(10, Math.floor(Math.log10(raw)));
	for (const m of [1, 2, 5]) {
		if (m * power >= raw) return m * power;
	}
	return 10 * power;
}

/**
 * How many decimals a step needs for its labels to stay distinct.
 *
 * @param step The tick step.
 * @returns A count from 0 to 6.
 * @raises RangeError If the step is not a positive finite number.
 */
export function stepDecimals(step: number): number {
	if (!Number.isFinite(step) || step <= 0) {
		throw new RangeError(`step is ${step}`);
	}
	return Math.min(6, Math.max(0, -Math.floor(Math.log10(step))));
}

/** One labeled tick on the bar. */
export interface LegendTick {
	value: number;
	/** Fraction of the bar, 0 to 1. */
	at: number;
	label: string;
}

/**
 * The ticks to label on a bar of a given length.
 *
 * Both ends of the ramp are always labeled, because a bar whose range the
 * reader has to infer from interior ticks is a bar that can be misread by a
 * factor of ten. Interior ticks fall on round values and are dropped where
 * they would crowd an end label.
 *
 * @param ramp The table.
 * @param lengthPx The bar's length along its own axis.
 * @param minSpacingPx The least room two labels may have between their
 *     centers. The caller takes this from its own font.
 * @returns Ticks ascending by value, at least the two ends.
 * @raises RangeError If either measurement is not a positive finite number,
 *     or the ramp's bounds are not a usable span.
 */
export function legendTicks(
	ramp: Ramp,
	lengthPx: number,
	minSpacingPx: number
): LegendTick[] {
	if (!Number.isFinite(lengthPx) || lengthPx <= 0) {
		throw new RangeError(`bar length is ${lengthPx}`);
	}
	if (!Number.isFinite(minSpacingPx) || minSpacingPx <= 0) {
		throw new RangeError(`label spacing is ${minSpacingPx}`);
	}
	const span = ramp.max - ramp.min;
	if (!Number.isFinite(span) || span <= 0) {
		throw new RangeError(`ramp spans ${ramp.min} to ${ramp.max}`);
	}

	const room = Math.max(1, Math.floor(lengthPx / minSpacingPx));
	return ticksAtStep(ramp, niceStep(span / room), minSpacingPx / lengthPx);
}

/**
 * The ticks one step produces, with both ends.
 *
 * @param ramp The table.
 * @param step The spacing between interior ticks, in value units.
 * @param gap The least fraction of the bar an interior tick must keep clear
 *     of either end.
 * @returns Ticks ascending by value, at least the two ends.
 * @raises RangeError If the step is not a positive finite number, or the
 *     ramp's bounds are not a usable span.
 */
export function ticksAtStep(
	ramp: Ramp,
	step: number,
	gap: number
): LegendTick[] {
	if (!Number.isFinite(step) || step <= 0) {
		throw new RangeError(`step is ${step}`);
	}
	const span = ramp.max - ramp.min;
	if (span / step > MAX_TICKS) {
		throw new RangeError(
			`a step of ${step} across ${span} wants more than ${MAX_TICKS} ticks`);
	}
	const decimals = stepDecimals(step);
	const fmt = (v: number): string => v.toFixed(decimals);
	const out: LegendTick[] = [
		{ value: ramp.min, at: fractionFor(ramp, ramp.min), label: fmt(ramp.min) }
	];
	// Interior ticks only, so a round value landing on an end does not label
	// it twice. Stepped by index rather than by accumulation, because a step
	// of 0.1 added 58 times lands on 3.9000000000000004, and that value
	// reaches the readout's band labels as well as these.
	for (let i = Math.ceil(ramp.min / step); i * step < ramp.max; i++) {
		const v = i * step;
		if (v <= ramp.min) continue;
		const at = fractionFor(ramp, v);
		if (at < gap || at > 1 - gap) continue;
		out.push({ value: v, at, label: fmt(v) });
	}
	out.push({ value: ramp.max, at: 1, label: fmt(ramp.max) });
	return out;
}

/**
 * The finest ticking whose own labels do not touch.
 *
 * Deciding the step from a pixel spacing and then formatting the labels is
 * circular, since a finer step carries more decimals and so wider labels.
 * This walks the 1-2-5 steps from finest to coarsest and returns the first
 * whose drawn labels clear each other, which answers the question by
 * measurement rather than by an estimate of label width.
 *
 * Widths come from one measured character, which is exact here because the
 * console sets `--font-mono` on these labels. The caller measures it in the
 * font the browser actually resolved, so a fallback face cannot silently
 * widen the labels past their room.
 *
 * @param ramp The table.
 * @param lengthPx The bar's length along its own axis.
 * @param charPx The advance width of one character in the label font.
 * @param gapChars How many characters of clear space to keep between two
 *     labels.
 * @returns Ticks ascending by value, down to the two ends when nothing fits.
 * @raises RangeError If a measurement is not a positive finite number, the
 *     gap is negative, or the ramp's bounds are not a usable span.
 */
export function fitTicks(
	ramp: Ramp,
	lengthPx: number,
	charPx: number,
	gapChars: number
): LegendTick[] {
	if (!Number.isFinite(lengthPx) || lengthPx <= 0) {
		throw new RangeError(`bar length is ${lengthPx}`);
	}
	if (!Number.isFinite(charPx) || charPx <= 0) {
		throw new RangeError(`character width is ${charPx}`);
	}
	if (!Number.isFinite(gapChars) || gapChars < 0) {
		throw new RangeError(`label gap is ${gapChars}`);
	}
	const span = ramp.max - ramp.min;
	if (!Number.isFinite(span) || span <= 0) {
		throw new RangeError(`ramp spans ${ramp.min} to ${ramp.max}`);
	}

	// A label is at least one character wide, so a bar of lengthPx holds at
	// most lengthPx/charPx of them, and any step finer than this cannot fit
	// whatever its labels say. The walk starts here to stay bounded. Starting
	// at the finest candidate instead, a step of 1e-6 across differential
	// phase builds 360 million ticks before the first pair rejects it.
	// MAX_TICKS bounds it as well, because the character bound alone assumes
	// one character per label. A 3840 px bar measuring 1 px characters admits
	// a step of 0.1 on that reading, which is 3,600 ticks of six characters
	// each, rejected only after being built.
	const minStep = Math.max(span * charPx / lengthPx, span / MAX_TICKS);
	const steps: number[] = [];
	for (let power = -6; power <= 9; power++) {
		for (const m of [1, 2, 5]) {
			const s = m * Math.pow(10, power);
			if (s >= minStep && s <= span) steps.push(s);
		}
	}
	steps.sort((a, b) => a - b);

	let coarsest = ticksAtStep(ramp, span, 0);
	for (const step of steps) {
		const t = ticksAtStep(ramp, step, 0);
		coarsest = t;
		if (fitsLabels(t, lengthPx, charPx, gapChars)) return t;
	}
	// Nothing finer fit, so only the ends remain. They are never dropped: a
	// bar whose range the reader has to infer from interior ticks can be
	// misread by a factor of ten.
	return coarsest.length <= 2
		? coarsest
		: [coarsest[0], coarsest[coarsest.length - 1]];
}

/**
 * Whether a tick set's labels clear each other at one bar length.
 *
 * Every label is centered on its tick, the ends included, so the two end
 * labels hang half their width past the bar. `barLayout` reserves that room.
 * Tucking the ends inward instead costs real ticks: on a 182 px velocity bar
 * the inward "-30" pushed the first interior label 1.8 px too close, and the
 * scale fell back from seven labels to three.
 *
 * @param ticks The ticks, ascending.
 * @param lengthPx The bar's length.
 * @param charPx The advance width of one character.
 * @param gapChars Clear space between labels, in characters.
 * @returns True when no two labels overlap.
 */
export function fitsLabels(
	ticks: LegendTick[],
	lengthPx: number,
	charPx: number,
	gapChars: number
): boolean {
	const gap = gapChars * charPx;
	const box = (t: LegendTick): [number, number] => {
		const half = (t.label.length * charPx) / 2;
		const center = t.at * lengthPx;
		return [center - half, center + half];
	};
	for (let i = 1; i < ticks.length; i++) {
		if (box(ticks[i])[0] - box(ticks[i - 1])[1] < gap) return false;
	}
	return true;
}

/** A bar's drawn geometry, with room for its end labels. */
export interface BarLayout {
	/** Room on each side for half an end label. */
	padPx: number;
	/** The bar itself, which is the available width less both pads. */
	barPx: number;
	ticks: LegendTick[];
}

/**
 * The bar and its ticks, sized to fit the labels inside the available width.
 *
 * Two passes, because the side padding depends on the end labels and those
 * depend on the step the remaining width allows. The second pass can only
 * coarsen the step, so its labels are no wider than the first's and its
 * padding no larger, which makes the result safe rather than merely close.
 *
 * @param ramp The table.
 * @param widthPx Everything the legend has to draw in.
 * @param charPx The advance width of one character in the label font.
 * @param gapChars Clear space between two labels, in characters.
 * @returns Where to draw the bar and what to tick it with.
 * @raises RangeError If a measurement is not usable, or the width leaves no
 *     room for a bar.
 */
export function barLayout(
	ramp: Ramp,
	widthPx: number,
	charPx: number,
	gapChars: number
): BarLayout {
	if (!Number.isFinite(widthPx) || widthPx <= 0) {
		throw new RangeError(`available width is ${widthPx}`);
	}
	const padFor = (ticks: LegendTick[]): number => Math.max(
		ticks[0].label.length,
		ticks[ticks.length - 1].label.length) * charPx / 2;

	let pad = 0;
	let bar = widthPx;
	let ticks = fitTicks(ramp, bar, charPx, gapChars);
	for (let pass = 0; pass < 2; pass++) {
		pad = padFor(ticks);
		bar = widthPx - 2 * pad;
		if (bar <= charPx) {
			throw new RangeError(
				`${widthPx} px leaves no bar once ${pad} px of label room is ` +
				`taken from each end`);
		}
		ticks = fitTicks(ramp, bar, charPx, gapChars);
	}
	return { padPx: pad, barPx: bar, ticks };
}
