/**
 * Which step playback shows next, and how long it dwells there.
 *
 * Pure, so a runner can reach it. A loop that skips wrongly still animates,
 * and the two failures available here are both invisible from outside: a
 * run of blank frames played at full dwell reads as a stalled map, and a
 * skip rule with no stop condition spins the loop over a wholly blank list
 * without ever painting.
 */

/** Below this fraction of painted pixels, a frame is blank. */
export const ACTIVITY_FLOOR = 0.005;

/**
 * How many blank frames one tick may skip over.
 *
 * A bound rather than a free scan. Without it a list that is blank
 * everywhere spins the whole length before painting, and the loop stops
 * being a loop the reader can interrupt.
 */
export const MAX_SKIP_PER_TICK = 6;

/**
 * How long each frame holds, in milliseconds.
 *
 * A dwell rather than a budget. The loop waits for the frame to load and
 * then waits out whatever of this is left, so the real interval is whichever
 * is longer. Measured before that change, six radars ran at 1.88 s a frame
 * against 0.98 s at one radar, because the loop was guessing at the load
 * instead of waiting for it.
 */
export const FRAME_DWELL_MS = 900;

/**
 * The speeds the control offers, as multipliers of the dwell above.
 *
 * Four, spaced by doubling. A continuous slider reads as finer control than
 * there is, since the dwell is a floor and not a rate: the loop waits for
 * the frame to load and then waits out whatever of the dwell is left, so
 * above about 2x on six radars the load is what sets the interval.
 */
export const SPEEDS: readonly number[] = [0.5, 1, 2, 4];

/** The speed a console starts at, and falls back to. */
export const DEFAULT_SPEED = 1;

/**
 * The shortest dwell any speed may ask for, in milliseconds.
 *
 * A loop with no floor is not a loop a reader can interrupt, and a frame
 * that paints for 30 ms is a flicker rather than a frame.
 */
export const MIN_DWELL_MS = 150;

/**
 * A stored or typed speed, reduced to one the control offers.
 *
 * Anything unrecognized becomes the default rather than being honored. The
 * value reaches here from saved preferences and from a link, so it can be a
 * string, a negative, a zero or a NaN, and each of those would otherwise
 * turn into a dwell that spins the loop or stops it.
 *
 * @param raw Whatever arrived.
 * @returns One of `SPEEDS`.
 */
export function normalizeSpeed(raw: unknown): number {
	const n = typeof raw === 'string' ? Number(raw) : raw;
	if (typeof n !== 'number' || !Number.isFinite(n)) return DEFAULT_SPEED;
	return SPEEDS.includes(n) ? n : DEFAULT_SPEED;
}

/**
 * How long a frame holds at a given speed.
 *
 * @param speed A speed multiplier, or anything at all.
 * @returns The dwell in milliseconds, never below `MIN_DWELL_MS`.
 */
export function dwellFor(speed: unknown): number {
	return Math.max(MIN_DWELL_MS, FRAME_DWELL_MS / normalizeSpeed(speed));
}

/**
 * The next speed when the control is stepped.
 *
 * Clamps at both ends rather than wrapping. Wrapping from 4x back to 0.5x
 * on one more press of the same control is a jump in the direction the
 * reader did not ask for.
 *
 * @param speed The current speed.
 * @param step How many notches, 1 for faster and -1 for slower.
 * @returns The new speed, one of `SPEEDS`.
 */
export function stepSpeed(speed: unknown, step: number): number {
	const at = SPEEDS.indexOf(normalizeSpeed(speed));
	const by = Number.isFinite(step) ? Math.trunc(step) : 0;
	const next = Math.min(SPEEDS.length - 1, Math.max(0, at + by));
	return SPEEDS[next];
}

/**
 * How a speed reads on the control.
 *
 * @param speed The speed.
 * @returns A short label, "1x" rather than "1.0x".
 */
export function speedLabel(speed: unknown): string {
	const n = normalizeSpeed(speed);
	return `${Number.isInteger(n) ? n : n.toFixed(1)}x`;
}

/**
 * The next step to show, skipping blank frames where the data allows.
 *
 * Skipping needs an activity reading per step, so the list is used only
 * when it is exactly as long as the step list. A partial or stale reading
 * would skip against the wrong frames, which looks like playback jumping
 * at random.
 *
 * Never returns the frame it started from. A scan over a list where every
 * frame is quiet walks the whole way round and lands back on `from`, and
 * returning that parks playback: the control still reads as playing and
 * the time axis stops. Seven tilt frames on a clear day are exactly that
 * shape, since the skip bound is six. Falling back to the plain next step
 * keeps the loop running at its normal cadence over an empty hour.
 *
 * @param from The step currently shown.
 * @param total How many steps there are.
 * @param activity Painted-pixel fraction per step, or an empty list.
 * @returns The next index, wrapping at the end, and never `from` unless
 *     there is only one step.
 */
export function nextInterestingStep(
	from: number,
	total: number,
	activity: readonly number[]
): number {
	if (total === 0) return 0;
	let i = (from + 1) % total;
	let skipped = 0;
	while (
		skipped < MAX_SKIP_PER_TICK
		&& activity.length === total
		&& activity[i] !== undefined
		&& activity[i] < ACTIVITY_FLOOR
		&& i !== from
	) {
		i = (i + 1) % total;
		skipped++;
	}
	return i === from ? (from + 1) % total : i;
}

/**
 * How long to wait before the next frame.
 *
 * Measured from when the frame's work started, so a slow fetch eats its own
 * dwell rather than adding to it. Playback then runs at a steady cadence
 * until the work exceeds the dwell, at which point it runs as fast as it
 * can without queueing.
 *
 * @param startedAt When this frame's work began, from a monotonic clock.
 * @param now The same clock, read after the work.
 * @param dwellMs The intended time per frame.
 * @returns Milliseconds to wait, never negative.
 */
export function remainingDwell(
	startedAt: number,
	now: number,
	dwellMs: number = FRAME_DWELL_MS
): number {
	return Math.max(0, dwellMs - (now - startedAt));
}

/**
 * The step a button moves to.
 *
 * Wrapping, because the scrubber is a loop and a reader holding the next
 * button expects to come round rather than stick at the end.
 *
 * @param current The step shown now.
 * @param total How many steps there are.
 * @param move Which way, or which end.
 * @returns The new index, or -1 when there are no steps.
 */
export function stepTo(
	current: number,
	total: number,
	move: 'first' | 'last' | 'prev' | 'next'
): number {
	if (total === 0) return -1;
	if (move === 'first') return 0;
	if (move === 'last') return total - 1;
	const delta = move === 'next' ? 1 : -1;
	return (current + delta + total) % total;
}
