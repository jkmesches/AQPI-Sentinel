/**
 * The console's state, encoded into a URL and read back out.
 *
 * A link has to reopen the same view for someone who was not there when it
 * was made, and two things decide whether it does.
 *
 * Everything is validated against the vocabulary the deployment actually
 * publishes, the same way stored prefs are. A link made on AQPI and opened
 * on XQPI names products, radars and moments that do not exist there, and a
 * link from six months ago may name a decommissioned radar. An unvalidated
 * value reaches MapLibre and the request layer, where a bad product id is a
 * 404 on every frame and a bad radar is an overlay that never paints.
 *
 * The playback position travels as a timestamp and never as a step index.
 * Indices shift as the window rolls, so a link made at step 30 of 31 opens
 * on whatever is 30th an hour later, which is a different minute and no
 * longer the frame the sender was looking at.
 */

/** What the deployment publishes, for validating a link against it. */
export interface Vocabulary {
	products: readonly string[];
	radars: readonly string[];
	moments: readonly string[];
	overlays: readonly string[];
	/** Units by product, since each product has its own. */
	units: Readonly<Record<string, readonly string[]>>;
}

/** Where the map is looking. */
export interface Camera {
	lon: number;
	lat: number;
	zoom: number;
	bearing: number;
	pitch: number;
}

/** The part of the console a link carries. */
export interface LinkState {
	product: string | null;
	/** The frame's time, ISO 8601, never a step index. */
	ts: string | null;
	radars: string[];
	moment: string | null;
	unit: string | null;
	overlays: string[];
	camera: Camera | null;
}

/** Decimal places kept for longitude and latitude. */
export const CAMERA_PLACES = 5;
/** Decimal places kept for zoom, bearing and pitch. */
export const ZOOM_PLACES = 2;

/**
 * The widest camera a link may carry.
 *
 * Longitude and latitude are the projection's own limits. Zoom is
 * MapLibre's range, and a value outside it throws rather than clamping.
 */
export const CAMERA_LIMITS = {
	lon: 180,
	lat: 85.051129,
	zoomMin: 0,
	zoomMax: 24,
	pitchMax: 85
} as const;

/** The query parameter each field travels under. */
export const PARAMS = {
	product: 'p',
	ts: 't',
	radars: 'r',
	moment: 'm',
	unit: 'u',
	overlays: 'o',
	camera: 'c'
} as const;

/**
 * The overlay toggles a link names, and which profile overlay gates each.
 *
 * These are the console's toggles, not `ALL_OVERLAYS`, which lists only the
 * four the profile supplies data for and spells them with underscores. A
 * link validated against that list loses terrain, 3D terrain, NEXRAD and
 * both hyphenated names, so a link made with five overlays on reopened
 * with two.
 *
 * Null means the toggle is always available, since it needs no per-profile
 * data.
 */
export const LINK_OVERLAYS: Readonly<Record<string, string | null>> = {
	'watersheds': 'watersheds',
	'reservoirs': 'reservoirs',
	'stream-gauges': 'stream_gauges',
	'burn-scars': 'burn_scars',
	'terrain': null,
	'terrain-3d': null,
	'nexrad': null
};

/**
 * The overlay names a link may carry on this deployment.
 *
 * @param profileOverlays What the profile supplies data for, as
 *     `ALL_OVERLAYS` spells them.
 * @returns The toggle names that are available here.
 */
export function linkOverlayNames(
	profileOverlays: readonly string[]
): string[] {
	return Object.entries(LINK_OVERLAYS)
		.filter(([, gate]) => gate === null || profileOverlays.includes(gate))
		.map(([name]) => name);
}

/** An empty link, which leaves every field to the stored prefs. */
export function emptyLink(): LinkState {
	return {
		product: null, ts: null, radars: [], moment: null, unit: null,
		overlays: [], camera: null
	};
}

/**
 * Round a number for the URL without trailing zeros.
 *
 * @param v The value.
 * @param places How many decimals to keep.
 * @returns The shortest form that keeps those decimals.
 */
function trim(v: number, places: number): string {
	return String(Number(v.toFixed(places)));
}

/**
 * Whether a camera is one a map can be put at.
 *
 * @param c The camera.
 * @returns True when every field is finite and in range.
 */
export function cameraInRange(c: Camera): boolean {
	return Number.isFinite(c.lon) && Math.abs(c.lon) <= CAMERA_LIMITS.lon
		&& Number.isFinite(c.lat) && Math.abs(c.lat) <= CAMERA_LIMITS.lat
		&& Number.isFinite(c.zoom) && c.zoom >= CAMERA_LIMITS.zoomMin
		&& c.zoom <= CAMERA_LIMITS.zoomMax
		&& Number.isFinite(c.bearing)
		&& Number.isFinite(c.pitch) && c.pitch >= 0
		&& c.pitch <= CAMERA_LIMITS.pitchMax;
}

/**
 * Encode the console's state as a query string.
 *
 * Fields that are null or empty are left out, so a default view produces a
 * bare link rather than one listing every default. Keys go in a fixed
 * order, so the same view always produces the same string and the caller
 * can skip a `replaceState` that would change nothing.
 *
 * @param s What to encode.
 * @returns A query string without the leading question mark.
 */
export function encodeLink(s: LinkState): string {
	const q = new URLSearchParams();
	if (s.product) q.set(PARAMS.product, s.product);
	if (s.ts) q.set(PARAMS.ts, s.ts);
	if (s.radars.length > 0) q.set(PARAMS.radars, [...s.radars].join(','));
	if (s.moment) q.set(PARAMS.moment, s.moment);
	if (s.unit) q.set(PARAMS.unit, s.unit);
	if (s.overlays.length > 0) q.set(PARAMS.overlays, [...s.overlays].join(','));
	if (s.camera && cameraInRange(s.camera)) {
		const c = s.camera;
		const parts = [trim(c.lon, CAMERA_PLACES), trim(c.lat, CAMERA_PLACES),
			trim(c.zoom, ZOOM_PLACES)];
		// Bearing and pitch ride along only when the map is actually
		// turned or tilted, which it usually is not.
		if (c.bearing !== 0 || c.pitch !== 0) {
			parts.push(trim(c.bearing, ZOOM_PLACES), trim(c.pitch, ZOOM_PLACES));
		}
		q.set(PARAMS.camera, parts.join(','));
	}
	return q.toString();
}

/**
 * Parse the camera triple or quintuple.
 *
 * @param raw The parameter's value.
 * @returns The camera, or null when it does not parse or is out of range.
 */
export function parseCamera(raw: string | null): Camera | null {
	if (!raw) return null;
	const parts = raw.split(',');
	if (parts.length !== 3 && parts.length !== 5) return null;
	const nums = parts.map((x) => Number(x));
	if (nums.some((n) => !Number.isFinite(n))) return null;
	const c: Camera = {
		lon: nums[0], lat: nums[1], zoom: nums[2],
		bearing: nums[3] ?? 0, pitch: nums[4] ?? 0
	};
	return cameraInRange(c) ? c : null;
}

/**
 * Keep the members of a list the deployment publishes.
 *
 * Case-sensitive, because these are ids rather than labels, and duplicates
 * are dropped so a hand-edited link cannot make the same radar paint twice.
 *
 * @param raw The parameter's value.
 * @param allowed What this deployment publishes.
 * @returns The members that survive, in the order given.
 */
export function keepKnown(
	raw: string | null,
	allowed: readonly string[]
): string[] {
	if (!raw) return [];
	const seen = new Set<string>();
	const out: string[] = [];
	for (const part of raw.split(',')) {
		const v = part.trim();
		if (v && allowed.includes(v) && !seen.has(v)) {
			seen.add(v);
			out.push(v);
		}
	}
	return out;
}

/** An ISO 8601 instant, with or without a zone. */
const TS_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:\d{2})?$/;

/**
 * Whether a string is a timestamp this console would have written.
 *
 * Checked by shape and then by parsing, because `Date.parse` accepts a
 * great deal that is not ISO 8601 and reading a link should not depend on
 * the browser's leniency.
 *
 * @param raw The parameter's value.
 * @returns True when it is a usable instant.
 */
export function isTimestamp(raw: string | null): raw is string {
	return !!raw && TS_RE.test(raw) && Number.isFinite(Date.parse(raw));
}

/**
 * Read a link, dropping everything this deployment does not publish.
 *
 * A dropped field is left null or empty, which the caller fills from stored
 * prefs. Dropping rather than failing is deliberate: a link that names one
 * decommissioned radar should still open on the rest of what it asked for.
 *
 * @param query The query string, with or without the leading question mark.
 * @param vocab What this deployment publishes.
 * @returns What survived validation.
 */
export function decodeLink(query: string, vocab: Vocabulary): LinkState {
	const q = new URLSearchParams(
		query.startsWith('?') ? query.slice(1) : query);
	const product = q.get(PARAMS.product);
	const out = emptyLink();
	out.product = product && vocab.products.includes(product) ? product : null;
	const ts = q.get(PARAMS.ts);
	out.ts = isTimestamp(ts) ? ts : null;
	out.radars = keepKnown(q.get(PARAMS.radars), vocab.radars);
	const moment = q.get(PARAMS.moment);
	out.moment = moment && vocab.moments.includes(moment) ? moment : null;
	out.overlays = keepKnown(q.get(PARAMS.overlays), vocab.overlays);
	out.camera = parseCamera(q.get(PARAMS.camera));
	// A unit belongs to one product, so it is only meaningful once the
	// product survived. A link naming mm with no product, or with a
	// product that does not have mm, carries no unit at all.
	const unit = q.get(PARAMS.unit);
	const forProduct = out.product ? vocab.units[out.product] ?? [] : [];
	out.unit = unit && forProduct.includes(unit) ? unit : null;
	return out;
}

/**
 * Whether a link carries anything at all.
 *
 * A link with nothing in it must not override stored prefs, since opening
 * the bare console would otherwise reset the reader's saved view.
 *
 * @param s The decoded link.
 * @returns True when at least one field survived.
 */
export function hasAny(s: LinkState): boolean {
	return !!s.product || !!s.ts || !!s.moment || !!s.unit
		|| s.radars.length > 0 || s.overlays.length > 0 || !!s.camera;
}

/**
 * The step whose time is nearest the one a link asked for.
 *
 * Nearest rather than exact, because the sender's frame may have rolled
 * out of the window by the time the link is opened and the next frame over
 * is a better answer than the newest one. Null when nothing is within
 * `toleranceMs`, which leaves the console on its own default rather than
 * jumping to an unrelated hour.
 *
 * @param steps The step list, each with an ISO time or null.
 * @param ts The time the link carried.
 * @param toleranceMs How far from the asked-for time still counts.
 * @returns An index, or null.
 */
export function stepForTime(
	steps: readonly { ts?: string | null }[],
	ts: string | null,
	toleranceMs: number
): number | null {
	if (!isTimestamp(ts)) return null;
	const want = Date.parse(ts);
	let best: number | null = null;
	let bestGap = Infinity;
	for (let i = 0; i < steps.length; i++) {
		const raw = steps[i].ts;
		if (!raw) continue;
		const got = Date.parse(raw);
		if (!Number.isFinite(got)) continue;
		const gap = Math.abs(got - want);
		if (gap < bestGap) {
			bestGap = gap;
			best = i;
		}
	}
	return best !== null && bestGap <= toleranceMs ? best : null;
}

/**
 * How far from a link's time a step may sit and still be the one it meant.
 *
 * One hour, which is the window the composite step lists cover, so a link
 * opened while its frame is still in the window lands on that frame and a
 * link older than the window lands on nothing rather than on an unrelated
 * time.
 */
export const STEP_TOLERANCE_MS = 3_600_000;

/**
 * Merge a link into a URL's existing query, keeping what is not ours.
 *
 * The console is not the only thing that reads the address bar.
 * `?diag=probe` turns on the structural probe the visual baseline reads,
 * and rebuilding the query from the link alone drops it, so the harness
 * loses the probe the moment the first view settles.
 *
 * @param search The current query string, with or without the question
 *     mark.
 * @param link The encoded link.
 * @returns A query string, empty when nothing is left to carry.
 */
export function mergeQuery(search: string, link: string): string {
	const ours = new Set(Object.values(PARAMS) as string[]);
	const out = new URLSearchParams();
	const existing = new URLSearchParams(
		search.startsWith('?') ? search.slice(1) : search);
	for (const [k, v] of existing) {
		if (!ours.has(k)) out.append(k, v);
	}
	for (const [k, v] of new URLSearchParams(link)) out.append(k, v);
	return out.toString();
}
