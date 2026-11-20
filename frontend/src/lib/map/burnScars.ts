/**
 * Burn-scar perimeters, filtered by how long ago each one burned.
 *
 * Pure, so a runner can reach it. The age window is a MapLibre expression
 * rather than a JavaScript predicate, because the slider has to move without
 * refetching 315 polygons, and `setFilter` on a live layer is cheap. An
 * expression is also data, and a runner can assert it without a map.
 *
 * The caller owns the layers. This module answers which features belong and
 * what the panel should say about them.
 */
// Type-only, so esbuild erases it and the runner needs no bundle step.
import type { ExpressionSpecification, FilterSpecification } from 'maplibre-gl';

/** Source and layer ids this component gives the perimeter layers. */
export const BURN_SCAR_SOURCE = 'burn-scars';
export const BURN_SCAR_FILL = 'burn-scar-fill';
export const BURN_SCAR_LINE = 'burn-scar-line';

/**
 * A Julian year in milliseconds.
 *
 * 365.25 days, so the window does not drift against the calendar over the
 * 30-year span the slider offers.
 */
export const YEAR_MS = 365.25 * 24 * 3600 * 1000;

/** The property CAL FIRE puts the ignition date in, as epoch milliseconds. */
export const ALARM_DATE = 'ALARM_DATE';

/**
 * Each feature's age in years, as an expression the style evaluates.
 *
 * `now` is baked in at build time rather than read per frame, since MapLibre
 * expressions have no clock. The layer is rebuilt on every slider move and
 * every theme swap, so the value stays fresh enough for a window measured in
 * years.
 *
 * @param now Milliseconds since the epoch. Injectable for tests.
 * @returns An expression yielding years since the fire.
 */
export function burnScarAge(now: number = Date.now()): ExpressionSpecification {
	return ['/', ['-', now, ['get', ALARM_DATE]], YEAR_MS];
}

/**
 * Features inside the slider's window, skipping any with no date.
 *
 * The `has` test comes first and is not decoration. A feature with no
 * `ALARM_DATE` evaluates `['get', ALARM_DATE]` to null, the subtraction
 * yields null, and the comparison lets it through, so every undated
 * perimeter in the file would draw at every slider position.
 *
 * @param years The oldest scar to draw, in years.
 * @param now Milliseconds since the epoch. Injectable for tests.
 * @returns A filter expression for `setFilter`.
 */
export function burnScarFilter(
	years: number,
	now: number = Date.now()
): FilterSpecification {
	return ['all', ['has', ALARM_DATE], ['<=', burnScarAge(now), years]];
}

/**
 * The month and year of the most recent perimeter in a collection.
 *
 * The panel prints this under the slider, so a reader can tell a layer that
 * is current from one whose source file was generated two years ago. Both
 * draw the same way otherwise.
 *
 * @param data The perimeter collection.
 * @returns A string like "Dec 2025", or empty when nothing carries a date.
 */
export function newestBurnScar(data: GeoJSON.FeatureCollection): string {
	let newest = 0;
	for (const f of data.features) {
		const alarm = f.properties?.[ALARM_DATE];
		if (typeof alarm === 'number' && alarm > newest) newest = alarm;
	}
	if (newest <= 0) return '';
	return new Date(newest).toLocaleDateString('en-US', {
		month: 'short', year: 'numeric', timeZone: 'UTC'
	});
}
