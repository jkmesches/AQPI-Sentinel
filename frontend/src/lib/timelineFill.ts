/**
 * Timeline cell encoding: colour = worst status, FILL HEIGHT = how much.
 *
 * A monitoring grid must never hide a failure, so worst-of-bunch still decides
 * a cell's colour — any red at all means something failed in that window. But
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
 * is the pass colour. The eye reads length as quantity, which is the thing
 * being encoded, and a colour kept at full saturation never turns to mud.
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

/** Statuses that are not a defect, so the cell is drawn as one flat colour. */
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
 * CSS `background` for one cell: a full-saturation band of `st` sized by its
 * share of the bucket, over the pass colour. `h` is the drawn cell height in
 * px, which is what makes the MIN_BAD_PX floor meaningful.
 */
export function cellFill(cell: TimelineCell | undefined, st: string, h: number): string {
	const bad = STATUS_BG[st] ?? 'transparent';
	if (FLAT_STATUSES.has(st)) return bad;
	const frac = badFraction(cell);
	if (frac >= 1) return bad;
	// Floor the band so a rare failure stays visible at any row height, then
	// clamp — on a very short row the floor alone can exceed the cell.
	const px = Math.max(MIN_BAD_PX, Math.round(frac * h));
	const pct = Math.min(100, (px / Math.max(h, 1)) * 100);
	if (pct >= 100) return bad;
	return `linear-gradient(to top, ${bad} 0 ${pct}%, ${STATUS_BG['pass']} ${pct}% 100%)`;
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
