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

/** Defect statuses, most severe first. Order is the drawing order from the
 *  bottom of the cell, and it matches the server's worst_rank: fail outranks
 *  error ("the monitored thing is broken" is a stronger statement than "our
 *  probe could not determine its state"), which outranks warn. */
const DEFECT_ORDER = ['fail', 'error', 'warn'] as const;

/** Per-status run count from the bucket, or undefined if the server did not
 *  send it. */
function countOf(cell: TimelineCell, st: string): number | undefined {
	switch (st) {
		case 'fail':  return cell.n_fail;
		case 'error': return cell.n_error;
		case 'warn':  return cell.n_warn;
		case 'skip':  return cell.n_skip;
		default:      return undefined;
	}
}

/**
 * CSS `background` for one cell, drawn from the bucket's FULL composition.
 *
 * Bottom to top, in severity order: fail, error, warn, skip, and only then the
 * share that actually passed. `h` is the drawn cell height in px, which is what
 * makes the MIN_BAD_PX floor meaningful.
 *
 * Every band is drawn because the remainder is not knowable from the worst
 * status alone, and assuming it was cost us the same bug twice.
 *
 * The first time, until 2026-09-05, the remainder above the bad band was
 * hard-coded to the pass color and any cell whose worst status was `pass` was
 * drawn as one flat green block — so skips were painted green. Measured at the
 * time: 141 of 1,093 buckets held a skip, 59 drew as solid green.
 *
 * The second time is the same assumption surviving in the band above: a cell
 * whose worst status is `fail` drew its fail band, its skip band, and then
 * everything left over in green — including the runs that ERRORED. XEBY on
 * 2026-09-11 is the case in point: 673 fail, 23 error, 20 skip and **not one
 * pass** in 24 hours, yet its 22:00 bucket (12 fail, 2 error, 1 skip of 15)
 * drew about an eighth of its height green. A radar that has been down for
 * weeks showed green for the runs where the probe itself failed.
 *
 * So: no status is inferred from another's absence. A band is drawn only for
 * runs the server actually counted, and the green at the top is what is left
 * after every counted status has taken its share — which on XEBY is nothing.
 */
export function cellFill(cell: TimelineCell | undefined, st: string, h: number): string {
	const own = STATUS_BG[st] ?? 'transparent';
	if (st === 'unknown' || !cell || !cell.n) return own;

	// Ambiguity still resolves toward visibility: a server too old to send the
	// counts, or a payload that contradicts itself, fills the cell with its own
	// status rather than inventing a composition.
	const isDefect = !FLAT_STATUSES.has(st);
	if (isDefect && badFraction(cell) >= 1) return own;

	const span = Math.max(h, 1);

	// Every status that actually occurred gets a band. Collected first, sized
	// second: sizing as we go let the most severe band eat the cell and drop
	// the rest, which is how one error among 999 fails disappeared entirely —
	// the same "guess low and it vanishes" failure MIN_BAD_PX exists to stop,
	// just applied to the band above the floor instead of the floor itself.
	const parts: Array<{ color: string; share: number; min: number }> = [];
	let defectShare = 0;
	for (const d of DEFECT_ORDER) {
		const c = countOf(cell, d);
		if (!c || c <= 0) continue;
		const share = Math.min(1, c / cell.n);
		defectShare += share;
		parts.push({ color: STATUS_BG[d], share, min: MIN_BAD_PX });
	}
	const skipShare = skipFraction(cell);
	// 1px, not MIN_BAD_PX: grey means "we did not judge", which must stay
	// visible but must never crowd out a defect that did happen.
	if (skipShare > 0) parts.push({ color: STATUS_BG['skip'], share: skipShare, min: 1 });
	// Pass gets NO floor, unlike every band below it. A floor here would round
	// a 1.1% healthy share up to a visible slice and make an almost-entirely
	// broken cell read as less broken — over-reporting health, which is the one
	// direction this encoding must never fail in. Green appears only when there
	// is enough green to earn a pixel on its own.
	const passShare = Math.max(0, 1 - defectShare - skipShare);
	if (passShare > 0) parts.push({ color: STATUS_BG['pass'], share: passShare, min: 0 });

	if (!parts.length) return STATUS_BG[st] ?? STATUS_BG['pass'];
	if (parts.length === 1) return parts[0].color;

	// Too short to show every band honestly — fall back to the worst status
	// filling the cell. Over-reporting severity is the safe direction; the
	// alternative is silently dropping whichever band did not fit.
	const floor = parts.reduce((a, p) => a + p.min, 0);
	if (floor > span) return own;

	const px = parts.map((p) => Math.max(p.min, Math.round(p.share * span)));
	// Shave the largest band(s) until it fits, never below their floors, so
	// what gets squeezed is the share that has pixels to spare rather than the
	// rare defect that has none.
	let total = px.reduce((a, b) => a + b, 0);
	while (total > span) {
		let bi = -1;
		for (let i = 0; i < px.length; i++) {
			if (px[i] > parts[i].min && (bi === -1 || px[i] > px[bi])) bi = i;
		}
		if (bi === -1) break;
		px[bi] -= 1;
		total -= 1;
	}

	const stops: string[] = [];
	let cursor = 0;
	for (let i = 0; i < parts.length; i++) {
		if (px[i] <= 0) continue;
		const startPct = cursor;
		cursor += (px[i] / span) * 100;
		stops.push(`${parts[i].color} ${startPct === 0 ? '0' : `${startPct}%`} ${Math.min(100, cursor)}%`);
	}
	if (stops.length === 1) return parts.find((p) => stops[0].startsWith(p.color))?.color ?? own;
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
