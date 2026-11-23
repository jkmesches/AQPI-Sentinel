/**
 * Which checks a page's source filters hide.
 *
 * The dashboard can hide every check whose readings come from RadarCA, and
 * every check that watches a NEXRAD radar. The backend decides both per
 * check (`reads_radarca`, `nexrad` on /api/checks), because L4 reads the
 * mount on one deployment and RadarCA on another, and because the NEXRAD set
 * is the one that also keeps those alarms from notifying. A second copy of
 * either rule here would drift from the backend's.
 *
 * Pure, so a runner can reach it. Rows and alarms are hidden by check id,
 * which both carry.
 */

/** The fields of a check's catalog entry that the filters read. */
export interface FilterableCheck {
	id: string;
	reads_radarca?: boolean;
	nexrad?: boolean;
}

/** Which sources a page is hiding. */
export interface SourceFilter {
	hideRadarca: boolean;
	hideNexrad: boolean;
}

/**
 * The ids of the checks a filter hides.
 *
 * A check the backend has not classified is never hidden. A filter that
 * guesses would hide a check nobody chose to hide.
 *
 * @param checks The check catalog.
 * @param filter Which sources are hidden.
 * @returns The hidden check ids.
 */
export function hiddenCheckIds(
	checks: readonly FilterableCheck[], filter: SourceFilter
): Set<string> {
	const out = new Set<string>();
	for (const c of checks) {
		if ((filter.hideRadarca && c.reads_radarca === true)
			|| (filter.hideNexrad && c.nexrad === true)) {
			out.add(c.id);
		}
	}
	return out;
}

/**
 * How many checks each filter could hide, for labels and for whether to
 * offer the filter at all.
 *
 * @param checks The check catalog.
 * @returns The count per source.
 */
export function sourceCounts(checks: readonly FilterableCheck[]): {
	radarca: number; nexrad: number;
} {
	return {
		radarca: checks.filter((c) => c.reads_radarca === true).length,
		nexrad: checks.filter((c) => c.nexrad === true).length
	};
}
