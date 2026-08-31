/**
 * Persisted map settings.
 *
 * Run:  node validation_tests/js/run_map_prefs.mjs
 *
 * The dangerous direction here is restoring, not saving. Stored settings
 * outlive the things they name — a radar gets decommissioned, a product is
 * renamed, a release changes the vocabulary — and handing a stale id to
 * MapLibre produces a map that is broken on arrival, for a user who has no
 * idea why and no obvious way to recover. Worse, it is sticky: the bad value
 * is reloaded on every visit, so the map stays broken until someone clears
 * site data.
 *
 * So most of what follows is about hostile stored input: unknown ids, corrupt
 * JSON, out-of-range numbers, wrong types, and storage that throws outright.
 * The bar is that loadPrefs() always returns something a map can be built
 * from, and that Reset genuinely returns to a clean state rather than leaving
 * a legacy key behind to resurrect the old value.
 */
export function runTests(mod) {
	const { loadPrefs, savePrefs, clearPrefs, DEFAULT_PREFS } = mod;
	const failures = [];
	const check = (label, cond, detail = '') => {
		console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`);
		if (!cond) failures.push(label);
	};

	// --- a localStorage stand-in we can corrupt and break on demand -------
	let store = new Map();
	let mode = 'ok';                       // 'ok' | 'throw-get' | 'throw-set'
	globalThis.localStorage = {
		getItem: (k) => { if (mode === 'throw-get') throw new Error('blocked'); return store.has(k) ? store.get(k) : null; },
		setItem: (k, v) => { if (mode === 'throw-set') throw new Error('quota'); store.set(k, String(v)); },
		removeItem: (k) => { store.delete(k); }
	};
	const KEY = 'sentinel-map-prefs';
	const reset = () => { store = new Map(); mode = 'ok'; };
	const VOCAB = {
		radars: ['XSCV', 'XSCW', 'CBAND'],
		composites: ['none', 'comp_ref', 'qpe_1hr'],
		moments: ['Reflectivity', 'Velocity']
	};

	// --- 1. round trip ---------------------------------------------------
	reset();
	let p = loadPrefs(VOCAB);
	check('a fresh browser gets defaults', p.composite === DEFAULT_PREFS.composite && p.activeRadars.length === 0);

	savePrefs({ ...DEFAULT_PREFS, composite: 'qpe_1hr', activeRadars: ['XSCV', 'CBAND'],
	            moment: 'Velocity', nexrad: true, opacity: 0.35, terrain: true, panelOpen: false,
	            camera: { lng: -122.4, lat: 37.8, zoom: 8.5, bearing: 12, pitch: 30 } });
	p = loadPrefs(VOCAB);
	check('selections survive a round trip',
	      p.composite === 'qpe_1hr' && p.moment === 'Velocity' &&
	      JSON.stringify(p.activeRadars) === JSON.stringify(['XSCV', 'CBAND']));
	check('toggles and opacity survive', p.nexrad === true && p.terrain === true &&
	      p.panelOpen === false && p.opacity === 0.35);
	check('the camera survives', p.camera && p.camera.zoom === 8.5 && p.camera.bearing === 12);

	// --- 2. stale vocabulary must degrade, never break -------------------
	reset();
	savePrefs({ ...DEFAULT_PREFS, activeRadars: ['XSCV', 'GONE', 'ALSO_GONE'] });
	p = loadPrefs(VOCAB);
	check('a decommissioned radar is dropped',
	      JSON.stringify(p.activeRadars) === JSON.stringify(['XSCV']), String(p.activeRadars));

	reset();
	savePrefs({ ...DEFAULT_PREFS, composite: 'removed_product', moment: 'NotAMoment' });
	p = loadPrefs(VOCAB);
	check('an unknown product falls back to the default', p.composite === DEFAULT_PREFS.composite, p.composite);
	check('an unknown moment falls back to the default', p.moment === DEFAULT_PREFS.moment, p.moment);

	// Without a vocabulary the caller cannot validate, so values are kept
	// rather than silently reset — otherwise an early load would wipe them.
	reset();
	savePrefs({ ...DEFAULT_PREFS, composite: 'something_custom' });
	check('values are preserved when no vocabulary is supplied',
	      loadPrefs().composite === 'something_custom');

	// --- 3. hostile stored input -----------------------------------------
	reset();
	store.set(KEY, '{not valid json');
	p = loadPrefs(VOCAB);
	check('corrupt JSON yields defaults, not a crash', p.composite === DEFAULT_PREFS.composite);
	check('...and the corrupt blob is discarded, not re-read forever', !store.has(KEY));

	for (const [label, blob] of [
		['null',        'null'],
		['a bare array', '[1,2,3]'],
		['a string',    '"hello"'],
		['a number',    '42']
	]) {
		reset(); store.set(KEY, blob);
		p = loadPrefs(VOCAB);
		check(`${label} yields a usable object`,
		      p && typeof p === 'object' && Array.isArray(p.activeRadars));
	}

	reset();
	store.set(KEY, JSON.stringify({ opacity: 'lots', tiltEl: 'high', nexrad: 'yes',
	                                activeRadars: 'XSCV', camera: 'somewhere' }));
	p = loadPrefs(VOCAB);
	check('wrong types fall back per field', p.opacity === DEFAULT_PREFS.opacity &&
	      p.tiltEl === DEFAULT_PREFS.tiltEl && p.nexrad === DEFAULT_PREFS.nexrad);
	check('a non-array radar list does not become a string of characters',
	      Array.isArray(p.activeRadars) && p.activeRadars.length === 0, String(p.activeRadars));
	check('a non-object camera is rejected', p.camera === null);

	// Out-of-range numbers are clamped rather than rejected: an opacity of 5
	// is a bug, but 1.0 is a better recovery than resetting the whole panel.
	reset();
	store.set(KEY, JSON.stringify({ opacity: 5, tiltEl: -3 }));
	p = loadPrefs(VOCAB);
	check('opacity is clamped into range', p.opacity === 1, String(p.opacity));
	check('tilt is clamped into range', p.tiltEl === 0, String(p.tiltEl));

	// A camera off the globe would send MapLibre somewhere undefined.
	for (const [label, cam] of [
		['longitude out of range', { lng: 999, lat: 37, zoom: 8 }],
		['latitude out of range',  { lng: -122, lat: 99, zoom: 8 }],
		['zoom out of range',      { lng: -122, lat: 37, zoom: 99 }],
		['NaN coordinates',        { lng: NaN, lat: 37, zoom: 8 }],
		// JSON turns NaN/Infinity into null, and Number(null) is 0 — so these
		// are the cases that silently produce a valid-looking 0,0 camera.
		['null longitude',         { lng: null, lat: 37, zoom: 8 }],
		['empty-string zoom',      { lng: -122, lat: 37, zoom: '' }],
		['Infinity zoom',          { lng: -122, lat: 37, zoom: Infinity }],
		['numeric strings',        { lng: '-122', lat: '37', zoom: '8' }],
		['missing fields',         { lng: -122 }]
	]) {
		reset(); store.set(KEY, JSON.stringify({ camera: cam }));
		check(`camera rejected: ${label}`, loadPrefs(VOCAB).camera === null);
	}

	reset();
	savePrefs({ ...DEFAULT_PREFS, activeRadars: ['XSCV', 'XSCV', 'XSCW'] });
	check('duplicate radar ids are collapsed',
	      JSON.stringify(loadPrefs(VOCAB).activeRadars) === JSON.stringify(['XSCV', 'XSCW']));

	// --- 4. storage that is unavailable or throws ------------------------
	reset(); mode = 'throw-get';
	p = loadPrefs(VOCAB);
	check('a throwing getItem still yields defaults', p.composite === DEFAULT_PREFS.composite);
	reset(); mode = 'throw-set';
	let threw = false;
	try { savePrefs({ ...DEFAULT_PREFS }); } catch { threw = true; }
	check('a throwing setItem does not propagate', !threw);
	mode = 'ok';

	// --- 5. legacy migration ---------------------------------------------
	// The pre-consolidation keys must carry over, or upgrading silently
	// resets toggles people had already set.
	reset();
	store.set('sentinel-map-watersheds', 'on');
	store.set('sentinel-map-terrain', 'on');
	store.set('sentinel-layers-open', 'closed');
	p = loadPrefs(VOCAB);
	check('legacy toggles are migrated', p.watersheds === true && p.terrain === true);
	check('legacy panel state is migrated', p.panelOpen === false);
	check('legacy keys not set stay at their default', p.reservoirs === false);

	// Once a real blob exists it wins; the legacy keys must not override it.
	reset();
	store.set('sentinel-map-watersheds', 'on');
	savePrefs({ ...DEFAULT_PREFS, watersheds: false });
	check('a saved blob takes precedence over legacy keys',
	      loadPrefs(VOCAB).watersheds === false);

	// --- 6. reset really resets ------------------------------------------
	reset();
	store.set('sentinel-map-watersheds', 'on');
	savePrefs({ ...DEFAULT_PREFS, composite: 'qpe_1hr', activeRadars: ['XSCV'], terrain: true });
	let after = clearPrefs();
	check('clear returns defaults', after.composite === DEFAULT_PREFS.composite && after.activeRadars.length === 0);
	p = loadPrefs(VOCAB);
	check('...and a reload after reset is still default', p.composite === DEFAULT_PREFS.composite);
	check('...including legacy keys, so old values cannot come back',
	      p.watersheds === false, String(p.watersheds));

	// --- 7. transient state is deliberately not persisted ----------------
	reset();
	savePrefs({ ...DEFAULT_PREFS, playing: true, stepIdx: 17 });
	p = loadPrefs(VOCAB);
	check('playback state is never restored',
	      p.playing === undefined && p.stepIdx === undefined,
	      JSON.stringify({ playing: p.playing, stepIdx: p.stepIdx }));

	console.log(failures.length
		? `\n${failures.length} FAILED: ${failures.join(', ')}`
		: '\nall map-prefs assertions passed');
	return failures.length ? 1 : 0;
}
