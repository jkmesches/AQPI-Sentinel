/**
 * Shared timebase for multi-radar overlays.
 *
 * Both maps used to take the FIRST active radar's frame list as the timeline
 * and let every other radar snap to the nearest frame. With one radar that is
 * exactly right. With several it is subtly wrong: radars scan on independent
 * cadences and phases, so the scrubber advanced on one radar's schedule and
 * the others were permanently approximated — including at the newest step,
 * where a radar that had just published sat one frame behind for no visible
 * reason.
 *
 * This builds a timeline from the union of every active radar's timestamps,
 * so each radar has at least one step where it is exact, and merges instants
 * that are close enough to be the same moment.
 */

export interface TimelineStep {
	i: number;
	ts: string | null;
	imageName: string;
	day: string;
	date: string;
	time: string;
}

/**
 * Instants within this many ms are treated as one step.
 *
 * Five radars on a ~120s cadence with independent phase would otherwise
 * produce five steps per cycle — a scrubber five times denser than the data,
 * which on a phone is unusable. 60s keeps one step per cycle while staying
 * well under the cadence, so genuinely distinct sweeps never merge.
 */
const MERGE_WINDOW_MS = 60_000;

function fmt(d: Date): { day: string; date: string; time: string } {
	const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
	const mons = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
	              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
	const p = (n: number) => String(n).padStart(2, '0');
	return {
		day: days[d.getUTCDay()],
		date: `${p(d.getUTCDate())}-${mons[d.getUTCMonth()]}-${d.getUTCFullYear()}`,
		time: `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`
	};
}

/**
 * Merge timestamps from several radars into one ordered step list.
 * Exported for testing; `buildSharedTimeline` is the normal entry point.
 */
export function mergeTimelines(tsLists: (string | null)[][]): TimelineStep[] {
	const epochs: number[] = [];
	for (const list of tsLists) {
		for (const ts of list) {
			if (!ts) continue;
			const n = Date.parse(ts);
			if (!Number.isNaN(n)) epochs.push(n);
		}
	}
	if (epochs.length === 0) return [];
	epochs.sort((a, b) => a - b);

	// Collapse runs of near-simultaneous instants to their midpoint, so the
	// merged step sits between the radars that produced it rather than
	// favoring whichever happened to scan first.
	const merged: number[] = [];
	let bucket: number[] = [epochs[0]];
	for (let i = 1; i < epochs.length; i++) {
		if (epochs[i] - bucket[0] <= MERGE_WINDOW_MS) {
			bucket.push(epochs[i]);
		} else {
			merged.push(Math.round(bucket.reduce((a, b) => a + b, 0) / bucket.length));
			bucket = [epochs[i]];
		}
	}
	merged.push(Math.round(bucket.reduce((a, b) => a + b, 0) / bucket.length));

	return merged.map((ms, i) => {
		const d = new Date(ms);
		const f = fmt(d);
		return { i, ts: d.toISOString(), imageName: '', day: f.day, date: f.date, time: f.time };
	});
}

/**
 * Fetch each radar's frame list and merge them into a shared timeline.
 * Returns null when no active radar has any frames.
 */
export async function buildSharedTimeline(
	radarIds: string[],
	moment: string,
	fetchFn: typeof fetch = fetch
): Promise<{ steps: TimelineStep[]; idx: number } | null> {
	if (radarIds.length === 0) return null;
	const lists = await Promise.all(
		radarIds.map(async (id) => {
			try {
				const r = await fetchFn(
					`/api/upstream/radar_steps?radar=${encodeURIComponent(id)}` +
					`&moment=${encodeURIComponent(moment)}`
				);
				if (!r.ok) return [];
				const j = await r.json();
				return ((j.steps ?? []) as { ts: string | null }[]).map((s) => s.ts);
			} catch {
				return [];          // one radar failing must not blank the timeline
			}
		})
	);
	const steps = mergeTimelines(lists);
	if (steps.length === 0) return null;
	return { steps, idx: steps.length - 1 };
}
