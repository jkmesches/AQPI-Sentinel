/**
 * How see-through each raster layer is, one setting per layer.
 *
 * One slider used to move the radar overlays and the tilt overlay together,
 * while the composite sat at a hard-coded 0.7 and NEXRAD at 0.6 with no way
 * to touch either. The three stack on the same ground, so the question a
 * reader actually has is which of them to fade against the others, and a
 * single slider cannot answer it.
 *
 * The defaults are the values that shipped, so turning three constants into
 * three settings changes nothing until someone moves one.
 *
 * A stored value reaches here from preferences written by an older build,
 * so nothing is trusted: anything out of range, missing or not a number
 * becomes the default rather than being handed to MapLibre, where an
 * opacity of NaN paints nothing and reads as a layer that failed to load.
 */

/** The layers that carry their own opacity. */
export const OPACITY_LAYERS = ['composite', 'radars', 'nexrad'] as const;

/** One of the layers above. */
export type OpacityLayer = (typeof OPACITY_LAYERS)[number];

/** A full set of opacities, one per layer. */
export type Opacities = Record<OpacityLayer, number>;

/**
 * What each layer shipped at before it was adjustable.
 *
 * `radars` also carries the tilt overlay, which is the same imagery from a
 * single radar and has always moved with them.
 */
export const DEFAULT_OPACITIES: Opacities = {
	composite: 0.7,
	radars: 0.8,
	nexrad: 0.6
};

/** What each slider is called. */
export const OPACITY_LABELS: Readonly<Record<OpacityLayer, string>> = {
	composite: 'Composite',
	radars: 'Radars',
	nexrad: 'NEXRAD'
};

/**
 * A finite number, or null when the value is not one.
 *
 * The empty string needs the explicit guard. `Number('')` is 0, and 0 is a
 * legal opacity, so a blank or whitespace value would pass validation and
 * hide a layer outright rather than falling back.
 *
 * @param raw Whatever arrived.
 * @returns The number, or null.
 */
function asNumber(raw: unknown): number | null {
	if (typeof raw === 'number') return Number.isFinite(raw) ? raw : null;
	if (typeof raw !== 'string') return null;
	const text = raw.trim();
	if (text === '') return null;
	const n = Number(text);
	return Number.isFinite(n) ? n : null;
}

/**
 * One opacity, clamped into range.
 *
 * @param raw Whatever arrived.
 * @param fallback The value to use when `raw` is unusable.
 * @returns A number in [0, 1].
 */
export function normalizeOpacity(raw: unknown, fallback: number): number {
	const n = asNumber(raw);
	if (n === null) return fallback;
	return Math.min(1, Math.max(0, n));
}

/**
 * A full set of opacities from whatever was stored.
 *
 * A `legacy` number is the single slider older builds saved. It applies to
 * the radars alone, because that is the only layer it ever moved, and it is
 * used only where the new per-layer value is absent, so one upgrade does
 * not keep overwriting a setting made after it.
 *
 * @param raw The stored object, or anything at all.
 * @param legacy The old single `opacity` value, if there was one.
 * @returns One usable number per layer.
 */
export function normalizeOpacities(raw: unknown, legacy?: unknown): Opacities {
	const obj = (raw && typeof raw === 'object' ? raw : {}) as
		Record<string, unknown>;
	const out = { ...DEFAULT_OPACITIES };
	for (const layer of OPACITY_LAYERS) {
		const stored = obj[layer];
		if (stored === undefined && layer === 'radars' && legacy !== undefined) {
			out[layer] = normalizeOpacity(legacy, DEFAULT_OPACITIES[layer]);
			continue;
		}
		out[layer] = normalizeOpacity(stored, DEFAULT_OPACITIES[layer]);
	}
	return out;
}

/**
 * An opacity as a whole percent, for a slider and its readout.
 *
 * @param value An opacity in [0, 1].
 * @returns A whole number in [0, 100].
 */
export function opacityPercent(value: number): number {
	return Math.round(normalizeOpacity(value, 0) * 100);
}

/**
 * A slider's percent back as an opacity.
 *
 * @param percent Whatever the slider reported.
 * @param fallback The value to keep when the slider reports nonsense.
 * @returns A number in [0, 1].
 */
export function opacityFromPercent(
	percent: unknown,
	fallback: number
): number {
	const n = asNumber(percent);
	if (n === null) return fallback;
	return Math.min(1, Math.max(0, n / 100));
}

/**
 * Whether a layer is fully transparent.
 *
 * A layer at zero is still in the style and still fetches its imagery, so
 * the panel says it is hidden rather than leaving the reader to wonder why
 * a layer that is switched on draws nothing.
 *
 * @param value An opacity.
 * @returns True when nothing of the layer will show.
 */
export function isHidden(value: number): boolean {
	return opacityPercent(value) === 0;
}
