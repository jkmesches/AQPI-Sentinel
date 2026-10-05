/**
 * Group every reading of one target onto a single expandable row.
 *
 * `pairByTarget` answers a two-source question: a radarca reading and its
 * backend counterpart, side by side. That shape ran out when LB3 arrived —
 * a radar now has up to THREE readings (L2 radarca, LB2 arrival, LB3 composite
 * participation) and nothing says it stops at three. A two-column layout
 * cannot grow a third column without rewriting every rail that uses it, and a
 * flat list of three rows per radar makes an 18-row rail out of six radars.
 *
 * So rows group by target and collapse to their worst reading, which is the
 * one an operator needs first: if any view of XEBY is unhappy, the row is
 * unhappy, and expanding says which view and why. `pairByTarget` is untouched
 * and still used where the two-source comparison IS the point.
 */

/**
 * Must match `backend/checks/helpers._STATUS_RANK` exactly.
 *
 * `skip` ranks BELOW `pass` deliberately, same as the backend: a check that
 * declined to judge must not outrank one that looked and found nothing wrong,
 * or a single skipped sub-check would gray out a healthy row. See the comment
 * above the backend copy.
 */
export const STATUS_RANK: Record<string, number> = {
	skip: 0,
	pass: 1,
	warn: 2,
	fail: 3,
	error: 4
};

/** The most severe of the given statuses. Empty input is `skip`, not `pass`. */
export function worstOf(statuses: readonly string[]): string {
	// Empty means "nothing reported this target", which is the absence of an
	// observation rather than a good one. `pass` here would paint a row green
	// on no evidence — the same mistake the fleet check's `skip` floor avoids.
	if (statuses.length === 0) return 'skip';
	let worst = statuses[0];
	for (const s of statuses) {
		if ((STATUS_RANK[s] ?? -1) > (STATUS_RANK[worst] ?? -1)) worst = s;
	}
	return worst;
}

/** True for a status an operator should look at. */
export function isAlerting(status: string): boolean {
	return status !== 'pass' && status !== 'skip';
}

export interface TargetGroup<T> {
	target: string;
	/** Every reading of this target, in the order the stages were given. */
	rows: T[];
	/** Worst status across `rows`. */
	worst: string;
	/** True when `worst` is something to act on. */
	alerting: boolean;
}

/**
 * Bucket rows by target, preserving the caller's stage order within each group.
 *
 * Stage order is the caller's to decide and is NOT sorted here: the rails want
 * arrival before participation before the radarca view, because that is the
 * causal order — a radar that is not arriving explains a missing composite
 * contribution, and reading the consequence first wastes the operator's
 * attention.
 */
export function groupByTarget<T extends { target: string; status: string }>(
	rows: readonly T[]
): TargetGroup<T>[] {
	const byTarget = new Map<string, T[]>();
	for (const r of rows) {
		const list = byTarget.get(r.target);
		if (list) list.push(r);
		else byTarget.set(r.target, [r]);
	}
	return [...byTarget.entries()]
		.map(([target, group]) => {
			const worst = worstOf(group.map((r) => r.status));
			return { target, rows: group, worst, alerting: isAlerting(worst) };
		})
		.sort((a, b) => a.target.localeCompare(b.target));
}

/**
 * Which groups start expanded.
 *
 * Only the ones that are alerting. A rail that opens everything is the flat
 * list we were trying to get away from, and a rail that opens nothing hides
 * the thing the operator came to read. Collapsing is a convenience for the
 * healthy majority, not a reason to make a fault take an extra click.
 */
export function initiallyOpen<T>(groups: readonly TargetGroup<T>[]): Set<string> {
	return new Set(groups.filter((g) => g.alerting).map((g) => g.target));
}
