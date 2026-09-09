/**
 * Timeline cell encoding: color = worst status, FILL HEIGHT = how much.
 *
 * A monitoring grid must never hide a failure, so worst-of-bunch still decides
 * a cell's color — any red at all means something failed in that window. But
 * worst-of alone gets more pessimistic as the grain coarsens. Measured over 7
 * days, inside cells drawn as bad:
 *
 *     5m   76.5% of runs really were bad
 *     1h   64.5%
 *     1d   27.6%   (worst case: 1 bad run in ~1000 painting a whole day)
 *
 * The first attempt encoded that share as opacity. It did not work: on a dark
 * canvas, lowering opacity blends toward black, so a sparse cell still reads as
 * a filled dark-red block, and the middle of the range came out muddy brown.
 * Opacity reads as "dim", not as "less".
 *
 * Proportional fill instead. The bad portion is a full-saturation band rising
 * from the bottom of the cell, sized by its share of the bucket; the remainder
 * is the pass color. The eye reads length as quantity, which is the thing
 * being encoded, and a color kept at full saturation never turns to mud.
 *
 * Lives here rather than inline in the route so the invariants below can be
 * tested — see validation_tests/js/test_timeline_fill.mjs.
 */
import type { TimelineCell } from './api';

export const STATUS_BG: Record<string, string> = {
	pass:    'var(--color-ok)',
	warn:    'var(--color-warn)',
	fail:    'var(--color-fail)',
	error:   'var(--color-error)',
	skip:    'var(--color-faint)',
	unknown: 'transparent'
};

export const STATUS_WORD: Record<string, string> = {
	pass: 'PASS', warn: 'WARN', fail: 'FAIL', error: 'ERROR', skip: 'SKIP', unknown: '—'
};

/** Statuses that are not a defect. These contribute no bad band of their own,
 *  but a cell at one of them is still drawn from its composition — a bucket of
 *  passes AND skips is not a flat green cell. */
const FLAT_STATUSES = new Set(['pass', 'skip', 'unknown']);

/**
 * === Load-bearing ===
 * The minimum height, in px, of the bad band whenever a bucket contains ANY
 * bad run. Without a floor, one bad run in a thousand rounds to 0px and a real
 * failure disappears from the grid entirely — the exact defect this whole
 * encoding exists to avoid. It must never reach zero; the test suite pins it.
 */
export const MIN_BAD_PX = 3;

/** The count matching a cell's own status, or undefined when the status is not
 *  a defect (nothing to count) or the server did not send the counts. */
function badCount(cell: TimelineCell): number | undefined {
	switch (cell.status) {
		case 'fail':  return cell.n_fail;
		case 'error': return cell.n_error;
		case 'warn':  return cell.n_warn;
		default:      return undefined;
	}
}

/**
 * Share of the bucket that was at the cell's own (worst) status, 0..1.
 *
 * Every ambiguous case resolves to 1 — a solid cell — because the failure mode
 * of guessing low is an invisible outage, and the failure mode of guessing
 * high is a cell that merely looks worse than it was.
 */
export function badFraction(cell: TimelineCell | undefined): number {
	if (!cell || !cell.n) return 0;          // no runs in this bucket at all
	const at = badCount(cell);
	if (at === undefined) return 1;          // not a defect, or pre-counts server
	if (at <= 0) return 1;                   // inconsistent payload — fail visible
	return Math.min(1, at / cell.n);         // clamp: at > n would overflow the cell
}

/**
 * Share of the bucket that was SKIPPED, 0..1.
 *
 * A skip is not a pass. The check ran and declined to return a verdict —
 * usually because an upstream dependency was unhealthy and the scheduler
 * demoted a real fail/error to skip so one fault would not paint every
 * downstream cell red. Drawing that as pass asserts we looked and found
 * nothing wrong, which is the opposite of what happened.
 *
 * Unlike badFraction, ambiguity here resolves to ZERO, not one. Guessing high
 * would gray out cells we have no evidence about, and gray is the color that
 * says "no data" — inventing it would be its own lie. The one exception is a
 * cell whose own status is `skip`: `pass` outranks `skip` when picking a
 * bucket's worst status, so a skip-status cell can only mean every run in it
 * skipped, even from a server too old to send the count.
 */
export function skipFraction(cell: TimelineCell | undefined): number {
	if (!cell || !cell.n) return 0;
	const s = cell.n_skip;
	if (s === undefined) return cell.status === 'skip' ? 1 : 0;
	if (s <= 0) return 0;
	return Math.min(1, s / cell.n);
}

/**
 * CSS `background` for one cell, drawn from the bucket's composition.
 *
 * Bottom to top: the bad band (the cell's own defect status), then skips, then
 * passes. `h` is the drawn cell height in px, which is what makes the
 * MIN_BAD_PX floor meaningful.
 *
 * The skip band is why this is not simply "bad over pass". Until 2026-09-05
 * the remainder above the bad band was hard-coded to the pass color, and any
 * cell whose worst status was `pass` was drawn as one flat green block. Both
 * cases painted skips green. Measured over 24h at 1h grain: 141 of 1,093
 * buckets contained a skip, 59 of them rendered as solid green and 2 more as a
 * green remainder — so a bucket where we had stopped being able to see
 * anything looked exactly like one we had checked and found healthy. That is
 * the same mistake the alarm engine was making by closing alarms on demoted
 * skips, and it is worse on the timeline, because the grid is what an operator
 * scans to decide whether to look closer at all.
 */
export function cellFill(cell: TimelineCell | undefined, st: string, h: number): string {
	const bad = STATUS_BG[st] ?? 'transparent';
	if (st === 'unknown') return bad;

	const isDefect = !FLAT_STATUSES.has(st);
	const badFrac = isDefect ? badFraction(cell) : 0;
	if (badFrac >= 1) return bad;

	const skipFrac = skipFraction(cell);
	if (!isDefect && skipFrac >= 1) return STATUS_BG['skip'];
	if (badFrac <= 0 && skipFrac <= 0) return STATUS_BG[st] ?? STATUS_BG['pass'];

	// Floor the bad band so a rare failure stays visible at any row height,
	// then clamp — on a very short row the floor alone can exceed the cell.
	let badPx = badFrac > 0 ? Math.max(MIN_BAD_PX, Math.round(badFrac * h)) : 0;
	if (badPx > h) badPx = h;
	// Skips yield to the bad band, never the other way round: an outage must
	// stay visible even in a bucket that is mostly skipped.
	let skipPx = Math.round(skipFrac * h);
	if (badPx + skipPx > h) skipPx = Math.max(0, h - badPx);

	const span = Math.max(h, 1);
	const badPct = (badPx / span) * 100;
	const skipPct = (skipPx / span) * 100;
	if (badPct >= 100) return bad;

	const stops: string[] = [];
	let cursor = 0;
	if (badPx > 0) {
		stops.push(`${bad} 0 ${badPct}%`);
		cursor = badPct;
	}
	if (skipPx > 0) {
		const start = cursor === 0 ? '0' : `${cursor}%`;
		cursor += skipPct;
		stops.push(`${STATUS_BG['skip']} ${start} ${cursor}%`);
	}
	if (cursor < 100) stops.push(`${STATUS_BG['pass']} ${cursor}% 100%`);
	return `linear-gradient(to top, ${stops.join(', ')})`;
}

/** Tooltip text carrying the same figures the fill encodes. */
export function cellRatioText(cell: TimelineCell | undefined): string {
	if (!cell || !cell.n) return '';
	const plural = cell.n === 1 ? '' : 's';
	const at = badCount(cell);
	if (at === undefined) return `${cell.n} run${plural}`;
	const pct = Math.round((at / cell.n) * 100);
	return `${at} of ${cell.n} run${plural} ${STATUS_WORD[cell.status] ?? cell.status} (${pct}%)`;
}
