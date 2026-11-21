/**
 * How large the console draws the things laid over the map.
 *
 * The legend, the compass, the cursor readout and the pane label are all
 * sized for a desk monitor at arm's length. A wall display across a room
 * and a laptop on a plane want different sizes, and the right answer is
 * the reader's rather than a measurement anyone can take for them.
 *
 * One number drives all of them, because a console where each overlay
 * has its own size control is one where they never agree.
 */

/** The sizes offered, as multipliers of the drawn-for-a-desk default. */
export const UI_SCALES = [0.85, 1, 1.25, 1.5, 2] as const;

/** Every overlay is drawn at this size, so the console opens at it. */
export const DEFAULT_UI_SCALE = 1;

/** What each size is called, for a control that has to label them. */
const NAMES: Record<string, string> = {
	'0.85': 'XS',
	'1': 'S',
	'1.25': 'M',
	'1.5': 'L',
	'2': 'XL'
};

/**
 * The nearest offered size to whatever was stored.
 *
 * A stored preference outlives the list it was chosen from, so a value
 * that is no longer offered snaps to the closest one rather than
 * dropping the reader back to the default they had moved away from.
 *
 * @param raw Whatever came out of storage or a URL.
 * @returns One of UI_SCALES.
 */
export function normalizeScale(raw: unknown): number {
	const want = typeof raw === 'number' ? raw : Number(raw);
	if (!Number.isFinite(want) || want <= 0) return DEFAULT_UI_SCALE;
	let best: number = DEFAULT_UI_SCALE;
	let gap = Infinity;
	for (const s of UI_SCALES) {
		const d = Math.abs(s - want);
		if (d < gap) {
			gap = d;
			best = s;
		}
	}
	return best;
}

/**
 * Move one step along the offered sizes.
 *
 * @param from The current size.
 * @param step How many places to move, positive for larger.
 * @returns The size that many places along, clamped at both ends.
 */
export function stepScale(from: number, step: number): number {
	const at = UI_SCALES.indexOf(normalizeScale(from) as typeof UI_SCALES[number]);
	const want = at + (Number.isFinite(step) ? Math.trunc(step) : 0);
	const i = Math.min(UI_SCALES.length - 1, Math.max(0, want));
	return UI_SCALES[i];
}

/**
 * What to call one size.
 *
 * @param scale One of the offered sizes.
 * @returns A short name, or the multiplier when it is not one of them.
 */
export function scaleName(scale: number): string {
	const at = normalizeScale(scale);
	return NAMES[String(at)] ?? `${at}x`;
}

/**
 * Round a length to whole pixels at one scale.
 *
 * Whole pixels because a half-pixel border on an overlay draws as a
 * grey smear on a display that is not retina, and these sit over
 * imagery a reader is judging colors against.
 *
 * @param px The length at the default size.
 * @param scale The size in force.
 * @returns The scaled length, at least one pixel.
 */
export function scaled(px: number, scale: number): number {
	if (!Number.isFinite(px)) return 1;
	return Math.max(1, Math.round(px * normalizeScale(scale)));
}

/**
 * Where the choice is kept.
 *
 * Its own key rather than a field of the map preferences, because those
 * belong to one pane and are written by the pane that owns the strip.
 * This one is the console's.
 */
const STORE_KEY = 'sentinel-ui-scale';

/**
 * The size kept from the last visit.
 *
 * Storage throws rather than returning null in a private window with
 * site data blocked, so the read is guarded and a failure reads as a
 * first visit.
 *
 * @returns The stored size, or the default when there is none.
 */
export function readScale(): number {
	if (typeof localStorage === 'undefined') return DEFAULT_UI_SCALE;
	try {
		return normalizeScale(localStorage.getItem(STORE_KEY));
	} catch {
		return DEFAULT_UI_SCALE;
	}
}

/**
 * Keep a size for the next visit.
 *
 * @param scale The size to store.
 */
export function writeScale(scale: number): void {
	if (typeof localStorage === 'undefined') return;
	try {
		localStorage.setItem(STORE_KEY, String(normalizeScale(scale)));
	} catch {
		// A browser that refuses to store it still has to run the
		// console, and the reader can set the size again next visit.
	}
}
