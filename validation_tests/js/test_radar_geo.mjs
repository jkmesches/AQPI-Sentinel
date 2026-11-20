/**
 * Radar markers and range rings.
 *
 * Run:  node validation_tests/js/run_radar_geo.mjs
 *
 * A wrong ring looks like a right one, so eyeballing it proves nothing.
 * The longitude term is what goes wrong.
 * A degree of longitude narrows toward the poles, and dropping the
 * cos(latitude) division draws an AQPI ring 21% too narrow while still
 * drawing a plausible circle.
 *
 * The rest is about the properties each feature carries, because the map
 * styles itself from them. A missing `clickable` turns a NEXRAD site into
 * something an operator can click and get nothing from.
 */
export function runTests(mod) {
	const { rangeSpanDeg, rangeRingCoords, radarExtent,
	        radarPointsGeo, radarRangesGeo } = mod;
	const failures = [];
	const check = (label, cond, detail = '') => {
		console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`);
		if (!cond) failures.push(label);
	};

	// XEBY and KDAX as /api/radars/meta reports them, plus FLOW for a
	// deployment with one radar at a different latitude.
	const XEBY = { id: 'XEBY', lat: 37.8156, lon: -122.062, range_m: 40000,
	               kind: 'xband', name: 'East Bay' };
	const KDAX = { id: 'KDAX', lat: 38.501, lon: -121.677, range_m: 100000, kind: 'nexrad' };
	const FLOW = { id: 'FLOW', lat: 34.2048, lon: -118.17081, range_m: 40346, kind: 'xband' };
	const sites = [XEBY, KDAX, FLOW];

	const status = (id) => ({ XEBY: 'pass', KDAX: 'warn', FLOW: 'fail' })[id] ?? 'skip';
	const paint = (s) => ({ verdict: `v-${s}`, center: `c-${s}`, halo: `h-${s}` });

	// --- 1. the longitude correction ------------------------------------
	const span = rangeSpanDeg(XEBY.lat, XEBY.range_m);
	check('a degree of longitude is narrower than one of latitude',
	      span.dLon > span.dLat, `dLon ${span.dLon.toFixed(4)} dLat ${span.dLat.toFixed(4)}`);
	const widening = span.dLon / span.dLat;
	const expected = 1 / Math.cos((XEBY.lat * Math.PI) / 180);
	check('...by exactly 1/cos(lat)', Math.abs(widening - expected) < 1e-12,
	      `${widening.toFixed(6)} vs ${expected.toFixed(6)}`);
	check('...which is a 21% error if dropped, at this latitude',
	      Math.abs((1 - 1 / widening) - 0.212) < 0.003,
	      `${((1 - 1 / widening) * 100).toFixed(1)}%`);
	check('the equator needs no correction',
	      Math.abs(rangeSpanDeg(0, 40000).dLon - rangeSpanDeg(0, 40000).dLat) < 1e-12);

	// --- 2. the ring itself ----------------------------------------------
	const ring = rangeRingCoords(XEBY);
	check('a ring has one point per segment plus the closing one', ring.length === 65,
	      String(ring.length));
	check('...and closes exactly',
	      ring[0][0] === ring[64][0] && ring[0][1] === ring[64][1],
	      `${ring[0]} vs ${ring[64]}`);
	check('...centered on the radar',
	      Math.abs((Math.min(...ring.map((c) => c[0])) + Math.max(...ring.map((c) => c[0]))) / 2
	               - XEBY.lon) < 1e-9);
	const coarse = rangeRingCoords(XEBY, 8);
	check('a coarser ring is still closed and still a ring',
	      coarse.length === 9 && coarse[0][0] === coarse[8][0], String(coarse.length));

	for (const bad of [2, 0, -1, 2.5, NaN]) {
		let threw = false;
		try { rangeRingCoords(XEBY, bad); } catch (e) { threw = e instanceof RangeError; }
		check(`${String(bad)} segments is refused`, threw);
	}

	// --- 3. the extent contains the ring ---------------------------------
	const ext = radarExtent(KDAX);
	const kring = rangeRingCoords(KDAX);
	check('the extent contains every point of the ring',
	      kring.every(([lon, lat]) =>
	          lon >= ext.west - 1e-9 && lon <= ext.east + 1e-9 &&
	          lat >= ext.south - 1e-9 && lat <= ext.north + 1e-9));
	check('...and is no larger than it needs to be',
	      Math.abs(ext.east - Math.max(...kring.map((c) => c[0]))) < 1e-9);

	// --- 4. what the map styles itself from ------------------------------
	const pts = radarPointsGeo(sites, status, paint);
	check('one point per radar', pts.features.length === 3, String(pts.features.length));
	const byId = Object.fromEntries(pts.features.map((f) => [f.properties.id, f.properties]));
	check('each carries its own status', byId.XEBY.status === 'pass' && byId.FLOW.status === 'fail',
	      `${byId.XEBY.status}/${byId.FLOW.status}`);
	check('...and the three colors that status resolves to',
	      byId.KDAX.color === 'v-warn' && byId.KDAX.center_color === 'c-warn'
	      && byId.KDAX.halo_color === 'h-warn');
	check('a NEXRAD site is not clickable', byId.KDAX.clickable === false);
	check('...while an X-band one is', byId.XEBY.clickable === true && byId.FLOW.clickable === true);
	check('a radar with no name falls back to its id', byId.KDAX.name === 'KDAX', byId.KDAX.name);
	check('nothing is active without an overlay to activate',
	      pts.features.every((f) => f.properties.active === false));

	const active = radarPointsGeo(sites, status, paint, (id) => id === 'XEBY');
	check('...and an active radar says so when one is passed',
	      active.features.filter((f) => f.properties.active).map((f) => f.properties.id)
	        .join(',') === 'XEBY');

	// --- 5. rings as features --------------------------------------------
	const rings = radarRangesGeo(sites, status, paint);
	check('one polygon per radar', rings.features.length === 3);
	check('...each a single closed ring',
	      rings.features.every((f) => f.geometry.coordinates.length === 1
	                                  && f.geometry.coordinates[0].length === 65));
	check('...colored by that radar\'s own verdict',
	      rings.features.find((f) => f.properties.id === 'FLOW').properties.color === 'v-fail');

	// A radar nothing reported must not borrow another's color.
	const unknown = radarPointsGeo([{ ...XEBY, id: 'NEW' }], () => 'skip', paint);
	check('an unreported radar takes the skip paint',
	      unknown.features[0].properties.center_color === 'c-skip');

	console.log(failures.length
		? `\n${failures.length} FAILED: ${failures.join(', ')}`
		: '\nall radar-geo assertions passed');
	return failures.length ? 1 : 0;
}
