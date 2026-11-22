/**
 * Putting a published step list into one order.
 *
 * A time strip places frames by time and a scrubber walks them by
 * position, so the two agree only while the list is strictly
 * increasing. A manifest that repeats a time breaks both: the repeats
 * stack on each other in the strip, and matching a held frame by
 * timestamp finds whichever copy comes first.
 *
 * Pure, so it can be tested in node. The console's own Step type
 * carries more than this needs, and the structural type below is the
 * part that matters.
 */

/** The part of a step this module reads. */
export interface Timed {
	/** Its position in the list. */
	i: number;
	/** When the frame is for, or null when the name did not parse. */
	ts: string | null;
}

/** An ordered list and what putting it in order cost. */
export interface Ordered<T extends Timed> {
	steps: T[];
	/** How many steps repeated a time another already held. */
	dropped: number;
}

/**
 * When a published timestamp is for, in milliseconds.
 *
 * A time with no zone designator is UTC. The publisher means UTC and
 * the backend formats the strip's own day, date and time from it that
 * way, while `Date.parse` reads a bare string as the reader's local
 * time, which moves a forecast hour by the browser's offset. Only a
 * list mixing the two spellings can be sorted wrongly by that, which
 * one manifest does not do, so this removes a hazard rather than a
 * fault.
 *
 * @param ts The timestamp as published, or null.
 * @returns The instant, or NaN when there is nothing to read.
 */
export function instantOf(ts: string | null): number {
	if (ts === null) return Number.NaN;
	const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/.test(ts) ? ts : `${ts}Z`;
	const ms = Date.parse(zoned);
	return Number.isFinite(ms) ? ms : Number.NaN;
}

/**
 * Whether a list is already one frame per time, oldest first.
 *
 * A step whose time cannot be read makes the question unanswerable
 * rather than false. It has no place in a sort, and a tilt frame whose
 * name did not parse is that case.
 *
 * `orderSteps` sorts on the instant and this compares on it, so the
 * two cannot disagree about one list.
 *
 * @param list The steps as the source gave them.
 * @returns True when the list is strictly increasing, or holds a step
 *     whose time cannot be read and so must be left alone.
 */
export function inOrder<T extends Timed>(list: readonly T[]): boolean {
	// Bounded by the list, and it stops at the first fault.
	for (let k = 1; k < list.length; k++) {
		const a = instantOf(list[k - 1].ts);
		const b = instantOf(list[k].ts);
		if (Number.isNaN(a) || Number.isNaN(b)) return true;
		if (a >= b) return false;
	}
	return true;
}

/**
 * One frame per timestamp, oldest first.
 *
 * A list that is already sound is returned untouched, so the ordinary
 * case costs one pass and no allocation.
 *
 * fcst_temp is why this exists. Its manifest carried 131 steps holding
 * 75 distinct times on 2026-11-22: one run out to the 27th, then a
 * jump back 4.4 days and a second run over the same ground.
 *
 * The later entry wins a tie, because a publisher appending to a
 * manifest puts the newer run last.
 *
 * @param list The steps as the source gave them.
 * @returns The ordered steps, renumbered, and how many were dropped.
 */
export function orderSteps<T extends Timed>(list: readonly T[]): Ordered<T> {
	if (inOrder(list)) return { steps: [...list], dropped: 0 };
	const byTs = new Map<string, T>();
	for (const s of list) {
		if (s.ts !== null) byTs.set(s.ts, s);
	}
	const out = [...byTs.values()]
		.sort((p, q) => instantOf(p.ts) - instantOf(q.ts))
		.map((s, k) => ({ ...s, i: k }));
	return { steps: out, dropped: list.length - out.length };
}
