/**
 * Where each frame sits on the time strip, and where the data is missing.
 *
 * The strip used to space steps evenly, one slot per frame. A product that
 * publishes every two minutes and then stops for three hours drew that gap
 * as one slot the same width as every other, so the strip said the record
 * was continuous when it was not. Reading an hours-old frame as current is
 * the failure this whole console keeps running into, and the control that
 * picks the frame was asserting it.
 *
 * Placing each frame at its own time fixes that, and introduces a second
 * question the even strip never had: how wide is a frame. A bar drawn from
 * one frame to the next would make a three-hour gap a three-hour-wide bar
 * at the last frame's activity, which claims the weather held for three
 * hours rather than that nothing was published. So a bar covers its own
 * interval only up to the normal cadence, and whatever is left over is
 * drawn as a gap.
 *
 * Everything here falls back to even spacing rather than failing. The times
 * arrive from an upstream that has served '', nulls and non-ISO strings, and
 * a time strip that cannot be drawn is worse than one drawn the old way.
 */

/** How many times the normal cadence an interval must be to count as a gap. */
export const GAP_FACTOR = 1.5;

/** The narrowest a bar may be drawn, as a fraction of the whole axis. */
export const MIN_BAR_FRACTION = 0.002;

/** Where one frame sits and how much of the axis its bar covers. */
export interface Bar {
	/** Index into the step list. */
	index: number;
	/** Left edge, as a fraction of the axis. */
	start: number;
	/** Width, as a fraction of the axis. */
	width: number;
}

/** A stretch of the axis with nothing published in it. */
export interface Gap {
	/** The step before the gap. */
	after: number;
	/** Left edge, as a fraction of the axis. */
	start: number;
	/** Width, as a fraction of the axis. */
	width: number;
	/** How long nothing was published, in milliseconds. */
	ms: number;
}

/** A whole axis: where the frames are, where the holes are. */
export interface Axis {
	bars: Bar[];
	gaps: Gap[];
	/** Each step's position, as a fraction of the axis. */
	positions: number[];
	/** The usual interval between frames, in milliseconds, or 0. */
	cadenceMs: number;
	/**
	 * True when the times could not be used and the frames are spaced
	 * evenly. The strip says so, rather than showing a regular axis that
	 * looks measured.
	 */
	even: boolean;
}

/**
 * Epoch milliseconds for each timestamp, or null where it cannot be read.
 *
 * @param times Timestamps as the step list carries them.
 * @returns One entry per input.
 */
export function parseTimes(
	times: readonly (string | null | undefined)[]
): (number | null)[] {
	return times.map((t) => {
		if (typeof t !== 'string' || t.trim() === '') return null;
		const ms = Date.parse(t);
		return Number.isFinite(ms) ? ms : null;
	});
}

/**
 * The usual interval between frames.
 *
 * The median rather than the mean, because the gaps this exists to find
 * would drag a mean toward themselves and then stop counting as gaps.
 *
 * @param stamps Epoch milliseconds, in order.
 * @returns The median positive interval, or 0 when there is none.
 */
export function medianInterval(stamps: readonly number[]): number {
	const gaps: number[] = [];
	for (let i = 1; i < stamps.length; i++) {
		const d = stamps[i] - stamps[i - 1];
		if (d > 0) gaps.push(d);
	}
	if (gaps.length === 0) return 0;
	gaps.sort((a, b) => a - b);
	const mid = Math.floor(gaps.length / 2);
	return gaps.length % 2 === 1
		? gaps[mid]
		: (gaps[mid - 1] + gaps[mid]) / 2;
}

/**
 * Evenly spaced positions, the fallback the old strip always used.
 *
 * @param count How many steps.
 * @returns One fraction per step.
 */
function evenPositions(count: number): number[] {
	if (count <= 1) return count === 1 ? [0] : [];
	return Array.from({ length: count }, (_, i) => i / (count - 1));
}

/**
 * An evenly spaced axis, for when the times cannot be used.
 *
 * @param count How many steps.
 * @returns An axis with no gaps.
 */
function evenAxis(count: number): Axis {
	const positions = evenPositions(count);
	const width = count > 1 ? 1 / count : 1;
	return {
		positions,
		bars: positions.map((start, index) => ({
			index,
			start: Math.min(start, 1 - width),
			width
		})),
		gaps: [],
		cadenceMs: 0,
		even: true
	};
}

/**
 * Where every frame sits, and where nothing was published.
 *
 * Falls back to even spacing when the times are missing, unreadable, all
 * the same, or out of order. Out of order matters: a list that is not
 * sorted produces negative widths, and a negative width on an absolutely
 * positioned bar draws nothing at all, so the strip would simply lose
 * frames with no sign that anything was wrong.
 *
 * @param times Timestamps as the step list carries them.
 * @param factor How many times the cadence counts as a gap.
 * @returns The axis to draw.
 */
export function buildAxis(
	times: readonly (string | null | undefined)[],
	factor: number = GAP_FACTOR
): Axis {
	const count = times.length;
	if (count === 0) return evenAxis(0);
	const parsed = parseTimes(times);
	if (parsed.some((t) => t === null)) return evenAxis(count);
	const stamps = parsed as number[];
	for (let i = 1; i < count; i++) {
		if (stamps[i] < stamps[i - 1]) return evenAxis(count);
	}
	const span = stamps[count - 1] - stamps[0];
	if (span <= 0) return evenAxis(count);
	const cadenceMs = medianInterval(stamps);
	if (cadenceMs <= 0) return evenAxis(count);

	const positions = stamps.map((t) => (t - stamps[0]) / span);
	const bars: Bar[] = [];
	const gaps: Gap[] = [];
	const limit = Number.isFinite(factor) && factor > 0 ? factor : GAP_FACTOR;
	for (let i = 0; i < count; i++) {
		// The last frame has no following interval, so it is drawn at the
		// cadence like any other and ends the axis.
		const interval = i < count - 1
			? stamps[i + 1] - stamps[i]
			: cadenceMs;
		const covered = Math.min(interval, cadenceMs);
		bars.push({
			index: i,
			start: positions[i],
			width: Math.max(MIN_BAR_FRACTION, covered / span)
		});
		if (i < count - 1 && interval > cadenceMs * limit) {
			const start = positions[i] + covered / span;
			gaps.push({
				after: i,
				start,
				width: Math.max(0, positions[i + 1] - start),
				ms: interval - cadenceMs
			});
		}
	}
	return { bars, gaps, positions, cadenceMs, even: false };
}

/**
 * The step nearest a point on the axis.
 *
 * By position rather than by index, so clicking inside a gap lands on the
 * frame next to it instead of on whatever index that fraction would be.
 *
 * @param positions Each step's position, as `buildAxis` reports them.
 * @param fraction Where on the axis, in [0, 1].
 * @returns The step index, or -1 when there are no steps.
 */
export function nearestStep(
	positions: readonly number[],
	fraction: number
): number {
	if (positions.length === 0) return -1;
	const f = Number.isFinite(fraction) ? fraction : 0;
	let best = 0;
	let bestDist = Infinity;
	for (let i = 0; i < positions.length; i++) {
		const d = Math.abs(positions[i] - f);
		if (d < bestDist) {
			bestDist = d;
			best = i;
		}
	}
	return best;
}

/**
 * How long a gap was, in words.
 *
 * @param ms A duration in milliseconds.
 * @returns A short label, "2h 15m" or "40m".
 */
export function gapLabel(ms: number): string {
	if (!Number.isFinite(ms) || ms <= 0) return '';
	const minutes = Math.round(ms / 60_000);
	if (minutes < 60) return `${minutes}m`;
	const hours = Math.floor(minutes / 60);
	const rest = minutes % 60;
	if (hours < 24) return rest ? `${hours}h ${rest}m` : `${hours}h`;
	const days = Math.floor(hours / 24);
	return `${days}d ${hours % 24}h`;
}

/**
 * Half of gapLabel's rounding step, below which it would print "0m".
 *
 * Not a tolerance for how far apart two panes may sit. It is the point
 * where a label stops saying anything, and a mark reading "0m behind" is
 * worse than no mark.
 */
export const OFFSET_FLOOR_MS = 30_000;

/**
 * How far a pane's frame sits from the time the panes share.
 *
 * A follower pane lands on the nearest frame its own product publishes,
 * within an hour, and two products' manifests are not always equally
 * fresh. So the panes can be minutes apart while one time strip sits under
 * both of them. An unmarked gap is read as the same minute, which is the
 * comparison this mode exists to prevent.
 *
 * @param at The frame this pane is on, ISO, or null when unknown.
 * @param shared The time both panes are following, ISO, or null.
 * @returns "8m behind", "4m ahead", or null when they agree or either
 *   time is missing or unparsable.
 */
export function frameOffsetLabel(
	at: string | null | undefined,
	shared: string | null | undefined
): string | null {
	if (!at || !shared) return null;
	const mine = Date.parse(at);
	const theirs = Date.parse(shared);
	if (!Number.isFinite(mine) || !Number.isFinite(theirs)) return null;
	const delta = mine - theirs;
	if (Math.abs(delta) < OFFSET_FLOOR_MS) return null;
	const words = gapLabel(Math.abs(delta));
	return words ? `${words} ${delta < 0 ? 'behind' : 'ahead'}` : null;
}
