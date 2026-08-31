/**
 * Persisted map settings — what the user had selected, restored on return.
 *
 * Before this, five booleans were stored under five ad-hoc keys and everything
 * that actually takes effort to set up (which radars, which product, which
 * moment, where you were looking) was thrown away on every navigation. This
 * consolidates them into one versioned blob and adds the rest.
 *
 * Two rules shape the whole module:
 *
 * 1. **Restoring must never be able to break the map.** Stored settings can
 *    outlive the things they name — a radar is decommissioned, a product is
 *    renamed, a release changes the vocabulary. So nothing is trusted on the
 *    way in: values are validated against what the caller says currently
 *    exists, and anything unrecognised falls back to a default rather than
 *    being handed to MapLibre. A stale preference must degrade to a working
 *    map, never to a broken one.
 *
 * 2. **Transient state is not a preference.** Playback position and whether
 *    the animation was running are deliberately NOT persisted: restoring a
 *    scrubber to a timestamp that has since scrolled out of the window shows
 *    stale weather, and auto-playing on arrival is hostile. Those reset to
 *    live on every visit.
 *
 * Storage can be unavailable or throw outright (private windows, blocked site
 * data, thumbnailing contexts), so every read and write is guarded and the
 * caller always gets a usable object.
 */

const KEY = 'sentinel-map-prefs';
const VERSION = 1;

/** The pre-consolidation keys, migrated once then left alone. */
const LEGACY_BOOL_KEYS: [keyof MapPrefs, string][] = [
	['watersheds',   'sentinel-map-watersheds'],
	['reservoirs',   'sentinel-map-reservoirs'],
	['terrain',      'sentinel-map-terrain'],
	['terrain3D',    'sentinel-map-terrain-3d'],
	['streamGauges', 'sentinel-map-stream-gauges']
];
const LEGACY_PANEL_KEY = 'sentinel-layers-open';

export interface MapCamera {
	lng: number;
	lat: number;
	zoom: number;
	bearing: number;
	pitch: number;
}

export interface MapPrefs {
	composite: string;
	moment: string;
	activeRadars: string[];
	nexrad: boolean;
	opacity: number;
	panelOpen: boolean;
	watersheds: boolean;
	reservoirs: boolean;
	terrain: boolean;
	terrain3D: boolean;
	streamGauges: boolean;
	tiltEl: number;
	camera: MapCamera | null;
}

export const DEFAULT_PREFS: MapPrefs = {
	composite: 'comp_ref',
	moment: 'Reflectivity',
	activeRadars: [],
	nexrad: false,
	opacity: 0.8,
	panelOpen: true,
	watersheds: false,
	reservoirs: false,
	terrain: false,
	terrain3D: false,
	streamGauges: false,
	tiltEl: 0,
	camera: null
};

export interface PrefsVocabulary {
	/** Radar ids that currently exist. Unknown ids are dropped on load. */
	radars?: readonly string[];
	/** Composite product ids that currently exist, including 'none'. */
	composites?: readonly string[];
	/** Moment names that currently exist. */
	moments?: readonly string[];
}

const bool = (v: unknown, d: boolean) => (typeof v === 'boolean' ? v : d);

function num(v: unknown, d: number, lo: number, hi: number): number {
	const n = typeof v === 'number' ? v : Number(v);
	if (!Number.isFinite(n)) return d;
	return Math.min(hi, Math.max(lo, n));
}

/** A camera is only usable if every field is finite and on the globe. */
function camera(v: unknown): MapCamera | null {
	if (!v || typeof v !== 'object') return null;
	const c = v as Record<string, unknown>;
	// typeof, not Number(): JSON.stringify writes NaN and Infinity as `null`,
	// and Number(null) is 0 — so coercing here turns a corrupt camera into a
	// perfectly valid one at 0°N 0°E and teleports the map to the Gulf of
	// Guinea. Empty string coerces to 0 the same way.
	if (typeof c.lng !== 'number' || typeof c.lat !== 'number' || typeof c.zoom !== 'number') {
		return null;
	}
	const lng = c.lng, lat = c.lat, zoom = c.zoom;
	if (![lng, lat, zoom].every(Number.isFinite)) return null;
	if (lng < -180 || lng > 180 || lat < -90 || lat > 90) return null;
	if (zoom < 0 || zoom > 24) return null;
	return {
		lng, lat, zoom,
		bearing: num(c.bearing, 0, -360, 360),
		pitch: num(c.pitch, 0, 0, 85)
	};
}

function pick(v: unknown, allowed: readonly string[] | undefined, d: string): string {
	if (typeof v !== 'string') return d;
	// No vocabulary supplied means the caller cannot validate; keep the value.
	if (!allowed) return v;
	return allowed.includes(v) ? v : d;
}

function migrateLegacy(): Partial<MapPrefs> {
	const out: Partial<MapPrefs> = {};
	try {
		for (const [field, key] of LEGACY_BOOL_KEYS) {
			const v = localStorage.getItem(key);
			if (v === 'on') (out as Record<string, unknown>)[field] = true;
			else if (v === 'off') (out as Record<string, unknown>)[field] = false;
		}
		if (localStorage.getItem(LEGACY_PANEL_KEY) === 'closed') out.panelOpen = false;
	} catch { /* storage unavailable — defaults are fine */ }
	return out;
}

export function loadPrefs(vocab: PrefsVocabulary = {}): MapPrefs {
	let raw: string | null = null;
	try {
		raw = localStorage.getItem(KEY);
	} catch {
		return { ...DEFAULT_PREFS };
	}

	// Nothing stored under the new key: fold the old individual keys in once,
	// so upgrading does not silently reset toggles people already set.
	if (!raw) return { ...DEFAULT_PREFS, ...migrateLegacy() };

	let obj: Record<string, unknown>;
	try {
		const parsed = JSON.parse(raw);
		if (!parsed || typeof parsed !== 'object') throw new Error('not an object');
		obj = parsed as Record<string, unknown>;
	} catch {
		// Corrupt or hand-edited. Drop it rather than failing on every load.
		try { localStorage.removeItem(KEY); } catch { /* */ }
		return { ...DEFAULT_PREFS };
	}

	// A blob from a newer build may use a vocabulary this one does not have.
	// Field-level validation below already handles that, so an unknown version
	// is read leniently rather than discarded.
	const radars = Array.isArray(obj.activeRadars)
		? obj.activeRadars
			.filter((r): r is string => typeof r === 'string')
			.filter((r) => !vocab.radars || vocab.radars.includes(r))
		: DEFAULT_PREFS.activeRadars;

	return {
		composite:    pick(obj.composite, vocab.composites, DEFAULT_PREFS.composite),
		moment:       pick(obj.moment, vocab.moments, DEFAULT_PREFS.moment),
		activeRadars: [...new Set(radars)],
		nexrad:       bool(obj.nexrad, DEFAULT_PREFS.nexrad),
		opacity:      num(obj.opacity, DEFAULT_PREFS.opacity, 0, 1),
		panelOpen:    bool(obj.panelOpen, DEFAULT_PREFS.panelOpen),
		watersheds:   bool(obj.watersheds, DEFAULT_PREFS.watersheds),
		reservoirs:   bool(obj.reservoirs, DEFAULT_PREFS.reservoirs),
		terrain:      bool(obj.terrain, DEFAULT_PREFS.terrain),
		terrain3D:    bool(obj.terrain3D, DEFAULT_PREFS.terrain3D),
		streamGauges: bool(obj.streamGauges, DEFAULT_PREFS.streamGauges),
		tiltEl:       num(obj.tiltEl, DEFAULT_PREFS.tiltEl, 0, 90),
		camera:       camera(obj.camera)
	};
}

export function savePrefs(p: MapPrefs): void {
	try {
		localStorage.setItem(KEY, JSON.stringify({ v: VERSION, ...p }));
	} catch { /* quota or blocked — losing a preference is not worth throwing */ }
}

/** Reset to defaults, including the legacy keys so nothing is resurrected. */
export function clearPrefs(): MapPrefs {
	try {
		localStorage.removeItem(KEY);
		for (const [, key] of LEGACY_BOOL_KEYS) localStorage.removeItem(key);
		localStorage.removeItem(LEGACY_PANEL_KEY);
	} catch { /* */ }
	return { ...DEFAULT_PREFS };
}
