/**
 * Finding a place on the map from what this deployment already knows.
 *
 * No geocoder. The CSU deployment's namespace has no public internet
 * route, so a search that called out to one would work on the laptop and
 * fail silently on the machine the operators use, which is the worst place
 * for a feature to differ. Everything searched here is a dataset the
 * console has already loaded or can load from its own backend.
 *
 * What that covers is the questions actually asked of this console: where
 * a radar is, where a named reservoir or stream gauge is, and which
 * watershed a name belongs to. It does not cover street addresses, and the
 * box says so rather than returning nothing and letting the reader guess
 * whether the place or the search is at fault.
 *
 * Matching is deliberately plain. A fuzzy matcher that scores "XSCR"
 * against "Santa Cruz" above the radar named XSCR is worse than no
 * ranking, and the names here are short, known and few.
 */

/** Where a result sits and what it is. */
export interface SearchEntry {
	/** Stable within a kind, used as the list key. */
	id: string;
	name: string;
	/** What kind of thing it is, shown beside the name. */
	kind: string;
	lng: number;
	lat: number;
	/** A second line, such as a radar's band or a gauge's river. */
	detail?: string;
	/** The zoom to fly to, where the thing has a natural scale. */
	zoom?: number;
	/**
	 * The area to fit, for a thing that has one.
	 *
	 * A watershed is a shape rather than a point, and flying to its center
	 * at a fixed zoom shows either a corner of it or half the state.
	 */
	bounds?: Bounds;
}

/** West, south, east and north, in degrees. */
export type Bounds = [number, number, number, number];

/** A result with the reason it matched. */
export interface SearchHit {
	entry: SearchEntry;
	/** Higher is better. See `scoreEntry`. */
	score: number;
}

/** An exact match on the whole name or id. */
export const SCORE_EXACT = 1000;

/** The name or id starts with the query. */
export const SCORE_PREFIX = 500;

/** A word inside the name starts with the query. */
export const SCORE_WORD = 250;

/** The query appears somewhere in the name. */
export const SCORE_SUBSTRING = 100;

/** How many results the box shows at once. */
export const MAX_HITS = 8;

/** Below this many characters, searching returns nothing. */
export const MIN_QUERY = 2;

/**
 * A name reduced to what matching compares.
 *
 * Accents are folded, because a reader typing "Peta" should find
 * "Petaluma" and one typing "Canada" should find "Cañada". Punctuation
 * becomes a space so "Mt. Barnabe" and "Mt Barnabe" are one name.
 *
 * @param text Whatever is being compared.
 * @returns The folded form, lower case, single-spaced and trimmed.
 */
export function fold(text: string): string {
	return text
		.normalize('NFD')
		.replace(/[̀-ͯ]/g, '')
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, ' ')
		.trim();
}

/**
 * How well one entry matches a query.
 *
 * The id is compared as well as the name, since a radar is as likely to be
 * asked for by its call sign as by where it is.
 *
 * @param entry The candidate.
 * @param query The folded query.
 * @returns The score, or 0 when it does not match at all.
 */
export function scoreEntry(entry: SearchEntry, query: string): number {
	if (query === '') return 0;
	const name = fold(entry.name);
	const id = fold(entry.id);
	if (name === query || id === query) return SCORE_EXACT;
	if (id.startsWith(query) || name.startsWith(query)) return SCORE_PREFIX;
	if (name.split(' ').some((w) => w.startsWith(query))) return SCORE_WORD;
	if (name.includes(query) || id.includes(query)) return SCORE_SUBSTRING;
	return 0;
}

/**
 * The best matches for a query.
 *
 * Ties break on the shorter name and then alphabetically, so the order is
 * the same every time the same query is typed. An unstable list reorders
 * under the reader's finger between keystrokes.
 *
 * @param entries Everything searchable.
 * @param raw What was typed.
 * @param limit How many to return.
 * @returns The hits, best first.
 */
export function searchEntries(
	entries: readonly SearchEntry[],
	raw: string,
	limit: number = MAX_HITS
): SearchHit[] {
	const query = fold(raw);
	if (query.length < MIN_QUERY) return [];
	const hits: SearchHit[] = [];
	for (const entry of entries) {
		const score = scoreEntry(entry, query);
		if (score > 0) hits.push({ entry, score });
	}
	hits.sort((a, b) => {
		if (b.score !== a.score) return b.score - a.score;
		if (a.entry.name.length !== b.entry.name.length) {
			return a.entry.name.length - b.entry.name.length;
		}
		return a.entry.name.localeCompare(b.entry.name);
	});
	return hits.slice(0, Math.max(0, limit));
}

/**
 * Whether an entry can be flown to.
 *
 * A dataset with a missing or null coordinate has shipped before, and a
 * result that moves the camera to the Gulf of Guinea looks like the map
 * broke rather than like the data did.
 *
 * @param entry The candidate.
 * @returns True when its coordinates are usable.
 */
export function isPlaceable(entry: SearchEntry): boolean {
	return Number.isFinite(entry.lng) && Number.isFinite(entry.lat)
		&& Math.abs(entry.lat) <= 90 && Math.abs(entry.lng) <= 180
		&& !(entry.lng === 0 && entry.lat === 0);
}

/**
 * Build the searchable list, dropping anything that cannot be flown to.
 *
 * @param entries The candidates, from any number of datasets.
 * @returns The usable ones, with duplicates of the same kind and id gone.
 */
export function buildIndex(
	entries: readonly SearchEntry[]
): SearchEntry[] {
	const seen = new Set<string>();
	const out: SearchEntry[] = [];
	for (const entry of entries) {
		if (!isPlaceable(entry)) continue;
		const key = `${entry.kind}:${entry.id}`;
		if (seen.has(key)) continue;
		seen.add(key);
		out.push(entry);
	}
	return out;
}

/**
 * The box around a GeoJSON geometry's coordinates.
 *
 * Walks whatever nesting the geometry has rather than assuming a shape,
 * because the watershed file holds both Polygon and MultiPolygon and a
 * reader of a MultiPolygon as a Polygon gets coordinates that are arrays.
 *
 * @param coordinates A coordinate, or any nesting of them.
 * @returns The bounds, or null when nothing usable was found.
 */
export function coordinateBounds(coordinates: unknown): Bounds | null {
	let west = Infinity;
	let south = Infinity;
	let east = -Infinity;
	let north = -Infinity;
	const walk = (node: unknown, depth: number): void => {
		// Bounded: GeoJSON nests at most four deep (MultiPolygon), and a
		// cyclic or absurdly deep value would otherwise hang the page.
		if (depth > 6 || !Array.isArray(node)) return;
		if (typeof node[0] === 'number' && typeof node[1] === 'number') {
			const [lng, lat] = node as number[];
			if (!Number.isFinite(lng) || !Number.isFinite(lat)) return;
			west = Math.min(west, lng);
			east = Math.max(east, lng);
			south = Math.min(south, lat);
			north = Math.max(north, lat);
			return;
		}
		for (const child of node) walk(child, depth + 1);
	};
	walk(coordinates, 0);
	if (!Number.isFinite(west) || !Number.isFinite(south)) return null;
	return [west, south, east, north];
}

/**
 * The middle of a box.
 *
 * @param bounds West, south, east and north.
 * @returns The center point.
 */
export function boundsCenter(bounds: Bounds): { lng: number; lat: number } {
	return {
		lng: (bounds[0] + bounds[2]) / 2,
		lat: (bounds[1] + bounds[3]) / 2
	};
}

/**
 * Which hit the arrow keys land on next.
 *
 * Clamped rather than wrapping. A list of eight that jumps from the last
 * result back to the first on one more press moves the selection away from
 * where the reader was looking.
 *
 * @param current The index selected now, or -1 for none.
 * @param count How many hits there are.
 * @param step 1 for down, -1 for up.
 * @returns The new index, or -1 when there is nothing to select.
 */
export function stepHit(current: number, count: number, step: number): number {
	if (count <= 0) return -1;
	if (current < 0) return step > 0 ? 0 : count - 1;
	return Math.min(count - 1, Math.max(0, current + Math.sign(step)));
}
