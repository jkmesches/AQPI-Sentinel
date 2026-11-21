/**
 * Which units a product can be shown in, and which of them are current.
 *
 * Six of AQPI's products publish a second unit as its own subtree. Three of
 * those subtrees stopped being written on 2026-09-30 while the canonical
 * one kept updating every two minutes, so a plain toggle would hand the
 * reader rainfall seven weeks old looking exactly like this hour's. The
 * backend therefore sends what the mount held for each unit, and this
 * module keeps that next to the choice rather than behind it.
 *
 * These notes are the only warning there is. The stale-composite banner
 * reads the backend's L1 verdict for the product, and the L1 checks read
 * the canonical unit by design, so it stays quiet while a stale alternate
 * unit fills the screen. MapView therefore puts a note on the map as well
 * as on the control, since the layers panel collapses and the map is what
 * the reader is looking at.
 */

/** What `/api/version` sends per product. */
export interface ProductUnits {
	/** The labels, canonical first. */
	units: readonly string[];
	/** Label to "live" or "stale:YYYY-MM-DD", as the mount was last read. */
	state: Readonly<Record<string, string>>;
}

/** The whole table, by product id. */
export type UnitTable = Readonly<Record<string, ProductUnits>>;

/** One entry in the unit control. */
export interface UnitChoice {
	/** The label the API takes. */
	key: string;
	/** Whether the published subtree was current when it was last read. */
	live: boolean;
	/** The date it stopped, for a unit that is not live. */
	since: string | null;
}

/**
 * The units one product offers.
 *
 * A product with fewer than two is not a choice, so it comes back empty and
 * the caller shows no control at all. Showing a single-option toggle
 * suggests there is something to pick.
 *
 * @param table The table from /api/version, or null before it arrives.
 * @param product The product id.
 * @returns The choices, canonical first, or an empty list.
 */
export function unitChoices(
	table: UnitTable | null,
	product: string
): UnitChoice[] {
	const entry = table?.[product];
	if (!entry || entry.units.length < 2) return [];
	return entry.units.map((key) => {
		const state = entry.state[key] ?? '';
		return {
			key,
			live: state === 'live',
			since: state.startsWith('stale:') ? state.slice('stale:'.length) : null
		};
	});
}

/**
 * The unit to show a product in when nothing has been chosen.
 *
 * The canonical one, which is the first and is what the L1 checks read.
 * Null for a product with no choice, which leaves the parameter off the
 * request entirely.
 *
 * @param choices What the product offers.
 * @returns A label, or null.
 */
export function defaultUnit(choices: readonly UnitChoice[]): string | null {
	return choices.length > 0 ? choices[0].key : null;
}

/**
 * What to say beside a unit that is not current, or null when it is.
 *
 * @param choices What the product offers.
 * @param unit The selected label, or null.
 * @returns A short note, or null when there is nothing to warn about.
 */
export function unitNote(
	choices: readonly UnitChoice[],
	unit: string | null
): string | null {
	if (!unit) return null;
	const choice = choices.find((c) => c.key === unit);
	if (!choice || choice.live) return null;
	return choice.since
		? `not published since ${choice.since}`
		: 'not published';
}

/**
 * Whether a stored unit still applies to a product.
 *
 * A unit restored from prefs after the product changed would be sent to a
 * product that does not have it, and the backend answers 400.
 *
 * @param choices What the product offers.
 * @param unit The stored label, or null.
 * @returns The unit when it still applies, otherwise the default.
 */
export function resolveUnit(
	choices: readonly UnitChoice[],
	unit: string | null
): string | null {
	if (unit && choices.some((c) => c.key === unit)) return unit;
	return defaultUnit(choices);
}
