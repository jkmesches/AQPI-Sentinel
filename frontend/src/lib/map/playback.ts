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
 * The next step to show, skipping blank frames where the data allows.
 *
 * Skipping needs an activity reading per step, so the list is used only
 * when it is exactly as long as the step list. A partial or stale reading
 * would skip against the wrong frames, which looks like playback jumping
 * at random.
 *
 * @param from The step currently shown.
 * @param total How many steps there are.
 * @param activity Painted-pixel fraction per step, or an empty list.
 * @returns The next index, wrapping at the end.
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
	return i;
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
