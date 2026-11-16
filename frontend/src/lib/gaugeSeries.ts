/**
 * Reading upstream timestamps, which are UTC without saying so.
 *
 * `/api/upstream/product_steps` returns `2026-11-16T22:14:00`, carrying
 * neither a `Z` nor an offset, and ECMAScript reads a date-time with no
 * offset as LOCAL time. Handing one straight to `Date.parse` therefore
 * slides the whole series by the viewer's own offset.
 *
 * That is not a cosmetic error. The map's staleness banner is derived from
 * the newest step's age, so west of UTC a stale product read as fresh and
 * more than three hours east of it every live product read as stale, putting
 * a "not current" warning over good data. Measured in a browser at UTC+5:30,
 * a six-minute-old composite reported as three hours old.
 *
 * This lives outside MapView so it can be tested against a fixed clock in
 * several zones, which is the only way to catch this class of fault.
 */

/** A timestamp that already states its zone, by `Z` or by an offset. */
const TS_HAS_ZONE = /([Zz]|[+-]\d{2}:?\d{2})$/;

/**
 * Parse a step timestamp, treating an unmarked one as UTC.
 *
 * @param ts An ISO 8601 timestamp, with or without a zone.
 * @returns Milliseconds since the epoch, or NaN if it does not parse.
 */
export function parseStepTs(ts: string): number {
	return Date.parse(TS_HAS_ZONE.test(ts) ? ts : `${ts}Z`);
}
