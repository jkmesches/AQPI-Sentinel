/**
 * Reading upstream timestamps, for the map's clocks and its gauge popups.
 *
 * Both readers here exist because the upstream publishes UTC without saying
 * so. `/api/upstream/product_steps` returns `2026-11-16T22:14:00` and the
 * per-COMID stream endpoints return column headers like `2026-11-16-2200`.
 * Neither carries a `Z` or an offset, and ECMAScript reads a date-time with
 * no offset as LOCAL time. Handing either straight to `Date.parse` therefore
 * slides the whole series by the viewer's own offset.
 *
 * That is not a cosmetic error. The map's staleness banner is derived from
 * the newest step's age, so west of UTC a stale product read as fresh and
 * more than three hours east of it every live product read as stale, putting
 * a "not current" warning over good data. Measured in a browser at UTC+5:30,
 * a six-minute-old composite reported as three hours old.
 *
 * These live outside MapView so they can be tested against a fixed clock in
 * several zones, which is the only way to catch this class of fault.
 */

/** A timestamp that already states its zone, by `Z` or by an offset. */
const TS_HAS_ZONE = /([Zz]|[+-]\d{2}:?\d{2})$/;

/** `YYYY-MM-DD-HHMM`, the column header the stream endpoints use. */
const GAUGE_HEADER = /^(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})$/;

/**
 * Parse a step timestamp, treating an unmarked one as UTC.
 *
 * @param ts An ISO 8601 timestamp, with or without a zone.
 * @returns Milliseconds since the epoch, or NaN if it does not parse.
 */
export function parseStepTs(ts: string): number {
	return Date.parse(TS_HAS_ZONE.test(ts) ? ts : `${ts}Z`);
}

/** One point of a gauge series: an instant and the flow reported for it. */
export interface GaugePoint {
	t: number;
	v: number;
}

/** The {headers, values} shape both stream endpoints return. */
export interface GaugeFetch {
	headers?: string[];
	values?: (string | number)[];
}

/**
 * Turn one stream-data reply into a time series.
 *
 * The first column echoes the COMID and is dropped. A column whose header or
 * value does not parse is skipped rather than failing the series, because a
 * single bad hour should cost that hour and not the chart.
 *
 * @param j The upstream reply, or null when the fetch failed.
 * @returns The points that parsed, oldest first.
 */
export function gaugeSeries(j: GaugeFetch | null): GaugePoint[] {
	if (!j?.headers || !j.values) return [];
	const out: GaugePoint[] = [];
	for (let i = 1; i < j.headers.length && i < j.values.length; i++) {
		const m = GAUGE_HEADER.exec(j.headers[i]);
		if (!m) continue;
		const v = Number(j.values[i]);
		// Number('') is 0, and an empty cell is a missing reading rather than
		// a measured zero. On a flow chart the two are opposite claims.
		if (!Number.isFinite(v) || String(j.values[i]).trim() === '') continue;
		out.push({ t: Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]), v });
	}
	return out.sort((a, b) => a.t - b.t);
}
