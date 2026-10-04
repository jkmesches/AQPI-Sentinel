/**
 * One rule for pairing a radarca reading with its backend counterpart.
 *
 * Three surfaces render the same idea -- the desktop timeline grid, the mobile
 * status cards and the mobile uptime grid -- and each one had grown its own
 * copy of "walk the primaries, pull the matching backend row in beside it,
 * then sweep up whatever backend rows had no mate". Shared here for the same
 * reason $lib/timelineFill is shared: the surfaces must not drift into
 * disagreeing about which rows belong together.
 *
 * Keyed on TARGET, never on stage presence. Deciding paired-vs-flat by asking
 * "does this deployment have backend checks at all" is too coarse -- one stray
 * check with no counterpart (the xband-fleet correlation in L2, the NWM stream
 * feed in L1) flips an entire rail into the paired layout and prints a
 * placeholder on every row that has nothing to pair with.
 */

/** `primary` leads a pair; `secondary` is the backend reading beneath it. */
export type PairRole = 'solo' | 'primary' | 'secondary';

export interface Paired<T> {
	item: T;
	role: PairRole;
}

/**
 * Interleave `backend` into `primary`, matching on `keyOf`.
 *
 * A primary with a mate yields two entries (`primary` then `secondary`), in
 * that order, so a caller can rely on a secondary always being immediately
 * preceded by its own primary -- which is what lets a group separator be
 * `role !== 'primary'` rather than needing a lookahead.
 *
 * Everything unmatched, from either side, comes back as `solo`. An empty
 * `backend` therefore yields an all-`solo` list identical in order to
 * `primary`, which is what keeps a deployment with no backend mount rendering
 * exactly as it did before pairing existed.
 */
export function pairByTarget<T>(
	primary: readonly T[],
	backend: readonly T[],
	keyOf: (item: T) => string
): Paired<T>[] {
	if (backend.length === 0) {
		return primary.map((item) => ({ item, role: 'solo' as PairRole }));
	}
	// Last writer wins on a duplicate key. Two backend checks for one target is
	// not a shape the backend produces; if it ever does, pairing one of them is
	// better than dropping the row or rendering it twice.
	const bk = new Map<string, T>();
	for (const b of backend) bk.set(keyOf(b), b);

	const out: Paired<T>[] = [];
	for (const item of primary) {
		const mate = bk.get(keyOf(item));
		if (mate !== undefined) {
			out.push({ item, role: 'primary' });
			out.push({ item: mate, role: 'secondary' });
			bk.delete(keyOf(item));
		} else {
			out.push({ item, role: 'solo' });
		}
	}
	// A backend reading whose primary is missing still deserves a row. Not an
	// edge case: the XQPI profile registers LB1 and LB2 with no L1 or L2 at
	// all, so on that deployment EVERY row arrives through this loop.
	for (const orphan of bk.values()) out.push({ item: orphan, role: 'solo' });
	return out;
}

/**
 * Does this entry close a pair group? True for everything but a `primary`,
 * which is always followed by its own `secondary`.
 *
 * Exported so the separator rule itself is shared, not just the ordering --
 * the timeline and the mobile cards draw the same 3px rule, and reimplementing
 * the predicate is how one of them ends up drawing it in the wrong place.
 */
export function endsPairGroup<T>(p: Paired<T>): boolean {
	return p.role !== 'primary';
}
