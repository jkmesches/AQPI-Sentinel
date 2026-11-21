/**
 * Which moments a deployment offers, and what the published tree calls them.
 *
 * The console used to carry AQPI's five moment labels as a constant. XQPI
 * names two of them differently ("DifferentialReflectivity" against
 * "Differential Reflectivity") and has no RhoHV, so three of the five buttons
 * sent a label `moment_to_prefix()` does not know and the scan route answered
 * 400. The vocabulary now comes from `/api/version`, which resolves it with
 * the backend's own table, exception included.
 *
 * The prefix matters beyond the request. The color ramps in `./ramps` were
 * measured against colorbars that sit beside the frames, so they are keyed by
 * the published prefix rather than by the console's label.
 */

import type { MomentPrefixes } from '$lib/site';

export type { MomentPrefixes };

/** One entry in the moment picker. */
export interface MomentChoice {
	/** The label the API takes. */
	key: string;
	/** For a compact button. */
	short: string;
	/** For the readout line. */
	full: string;
}

/**
 * Display names for the labels both profiles use.
 *
 * A label absent here still appears, named after itself, so a profile that
 * adds a moment gets a working button before this table is updated.
 */
const DISPLAY: Readonly<Record<string, { short: string; full: string }>> = {
	'Reflectivity': { short: 'Z', full: 'Reflectivity' },
	'Velocity': { short: 'V', full: 'Velocity' },
	'Differential Reflectivity': { short: 'Zdr', full: 'Differential Refl' },
	'DifferentialReflectivity': { short: 'Zdr', full: 'Differential Refl' },
	'PhiDP': { short: 'ΦDP', full: 'Differential Phase' },
	'DifferentialPhase': { short: 'ΦDP', full: 'Differential Phase' },
	'RhoHV': { short: 'ρhv', full: 'Correlation Coef' }
};

/**
 * The order moments are shown in, by their display name.
 *
 * Keyed on the full name so the two spellings of each polarimetric moment
 * land in the same place whichever profile is running.
 */
const ORDER = ['Reflectivity', 'Velocity', 'Differential Refl',
	'Differential Phase', 'Correlation Coef'];

/**
 * How one moment is shown.
 *
 * @param key The label the API takes.
 * @returns Its short and full names, defaulting to the label itself.
 */
export function momentDisplay(key: string): { short: string; full: string } {
	return DISPLAY[key] ?? { short: key, full: key };
}

/**
 * The moments a deployment offers, in display order.
 *
 * The union across radars rather than one radar's set, because the picker is
 * shown before a radar is chosen and the C-band carries a moment the X-bands
 * do not.
 *
 * @param prefixes The table from /api/version, or null before it arrives.
 * @param fallback What to offer meanwhile.
 * @returns The choices, deduplicated and ordered.
 */
export function momentChoices(
	prefixes: MomentPrefixes | null,
	fallback: readonly string[]
): MomentChoice[] {
	const keys = new Set<string>();
	if (prefixes) {
		for (const perRadar of Object.values(prefixes)) {
			for (const k of Object.keys(perRadar)) keys.add(k);
		}
	}
	const source = keys.size > 0 ? [...keys] : [...fallback];
	const out = source.map((key) => ({ key, ...momentDisplay(key) }));
	out.sort((a, b) => {
		const ai = ORDER.indexOf(a.full);
		const bi = ORDER.indexOf(b.full);
		if (ai !== bi) return (ai < 0 ? ORDER.length : ai) - (bi < 0 ? ORDER.length : bi);
		return a.key.localeCompare(b.key);
	});
	return out;
}

/**
 * The published prefix for one radar's moment, or null when it has none.
 *
 * Null for a moment that radar does not carry, which is the honest answer for
 * the X-bands' RhoHV and for anything on a radar the table does not list.
 *
 * @param prefixes The table from /api/version, or null.
 * @param radar The radar id.
 * @param moment The moment label.
 * @returns The prefix, or null.
 */
export function prefixFor(
	prefixes: MomentPrefixes | null,
	radar: string,
	moment: string
): string | null {
	return prefixes?.[radar]?.[moment] ?? null;
}

/**
 * The moment to fall back to when the current one is not offered.
 *
 * Reflectivity where it exists, since every profile publishes it, and
 * otherwise the first choice. Returns null only for an empty list.
 *
 * @param choices The offered moments.
 * @returns A key, or null.
 */
export function defaultMoment(choices: readonly MomentChoice[]): string | null {
	if (choices.length === 0) return null;
	const z = choices.find((c) => c.full === 'Reflectivity');
	return (z ?? choices[0]).key;
}
