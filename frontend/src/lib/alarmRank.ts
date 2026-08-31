/**
 * Front-page alarm ordering and filtering.
 *
 * The dashboard is a glance surface: it shows the top few alarms, most urgent
 * first, and hides ones somebody has already acknowledged. Three things about
 * that are easy to get subtly wrong, and none of them fail loudly:
 *
 *   - Ordering by arrival instead of severity buries a critical under warns.
 *   - Hiding acknowledged alarms is fine; hiding them from the COUNT is not.
 *     The header must always state how many are really open, or the dashboard
 *     under-reports the state of the system, which is the one thing it exists
 *     not to do.
 *   - Capping the list is fine; capping it silently is not. Whatever does not
 *     fit has to be reachable and counted.
 *
 * So this returns both what to draw and what was withheld, and the caller is
 * expected to render the second part too.
 */
import type { Alarm } from './api';

/** Higher sorts first. Unknown severities rank last but are never dropped. */
export const SEV_RANK: Record<string, number> = { critical: 3, warn: 2, info: 1 };

export interface RankedAlarms {
	/** The rows to draw, already ordered and capped. */
	shown: Alarm[];
	/** How many ranked alarms did not fit in the cap. */
	overflow: number;
	/** How many open alarms are acknowledged, shown or not. Drives the toggle. */
	acked: number;
	/** Every open alarm, ordered — for callers that want the full ranking. */
	ranked: Alarm[];
}

/**
 * Order by severity, then oldest first within a severity: an unresolved
 * critical from this morning outranks one that opened a minute ago.
 */
export function compareAlarms(x: Alarm, y: Alarm): number {
	const d = (SEV_RANK[y.severity] ?? 0) - (SEV_RANK[x.severity] ?? 0);
	if (d) return d;
	const tx = new Date(x.opened_at).getTime();
	const ty = new Date(y.opened_at).getTime();
	// Unparseable dates must not reorder anything relative to valid ones.
	if (Number.isNaN(tx) || Number.isNaN(ty)) return 0;
	return tx - ty;
}

export function rankAlarms(
	alarms: readonly Alarm[] | null | undefined,
	opts: { showAcked?: boolean; limit?: number } = {}
): RankedAlarms {
	const all = alarms ?? [];
	const limit = opts.limit ?? 5;
	const acked = all.filter((a) => !!a.ack).length;
	const visible = opts.showAcked ? all : all.filter((a) => !a.ack);
	// Copy before sorting: the store's array is reactive state and sorting in
	// place would mutate it under Svelte.
	const ranked = [...visible].sort(compareAlarms);
	return {
		shown: limit >= 0 ? ranked.slice(0, limit) : ranked,
		overflow: Math.max(0, ranked.length - Math.max(limit, 0)),
		acked,
		ranked
	};
}
