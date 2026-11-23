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
	const { rangeSpanDeg, rangeRingCoords, radarExtent, frameExtent, isSelectable,
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

	// --- 6. which radars a click selects ---------------------------------
	// The backend says so per deployment: NEXRAD frames exist only where
	// the mount does, and the field is absent on a backend that predates it.
	const KDAX_SCAN = { ...KDAX, scan: true, frame_half_m: 200000 };
	check('a NEXRAD site with frames is selectable', isSelectable(KDAX_SCAN) === true);
	check('...and its pin is clickable',
	      radarPointsGeo([KDAX_SCAN], status, paint).features[0].properties.clickable === true);
	check('a NEXRAD site the backend says has none is not',
	      isSelectable({ ...KDAX, scan: false }) === false);
	check('an X-band the backend says has none is not either',
	      isSelectable({ ...XEBY, scan: false }) === false);
	check('without the field, the kind decides as it did',
	      isSelectable(XEBY) && isSelectable({ ...XEBY, kind: 'cband' }) && !isSelectable(KDAX));

	// --- 7. where a frame is placed --------------------------------------
	const lat2y = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
	const y2lat = (y) => ((2 * Math.atan(Math.exp(y)) - Math.PI / 2) * 180) / Math.PI;
	const KDAX_EXACT = { ...KDAX_SCAN, lat: 38.50111, lon: -121.67783 };
	const fe = frameExtent(KDAX_EXACT);
	// MapLibre interpolates linearly in Mercator, so the middle pixel lands
	// at the Mercator midpoint of the box.
	const midLat = y2lat((lat2y(fe.north) + lat2y(fe.south)) / 2);
	check('the middle pixel of a NEXRAD frame lands on the radar',
	      Math.abs(midLat - KDAX_EXACT.lat) * 111.19 < 0.001,
	      `${((midLat - KDAX_EXACT.lat) * 111.19 * 1000).toFixed(2)} m`);
	check('...in longitude too', Math.abs((fe.west + fe.east) / 2 - KDAX_EXACT.lon) < 1e-9);
	const naive = { north: KDAX_EXACT.lat + 200 / 111, south: KDAX_EXACT.lat - 200 / 111 };
	const naiveMid = y2lat((lat2y(naive.north) + lat2y(naive.south)) / 2);
	const offKm = (naiveMid - KDAX_EXACT.lat) * 111.19;
	check('a box centered in latitude would put it 2.5 km north (the bug this avoids)',
	      Math.abs(offKm - 2.5) < 0.05, `${offKm.toFixed(3)} km`);
	// 400 km across at the radar's own Mercator scale, east to west and
	// north to south alike, so the square frame stays square.
	const kmPerRadAtRadar = 6371 * Math.cos((KDAX_EXACT.lat * Math.PI) / 180);
	const widthKm = ((fe.east - fe.west) * Math.PI / 180) * kmPerRadAtRadar;
	const heightKm = (lat2y(fe.north) - lat2y(fe.south)) * kmPerRadAtRadar;
	check('the frame spans 400 km across at the radar', Math.abs(widthKm - 400) < 1e-6,
	      `${widthKm.toFixed(4)} km`);
	check('...and 400 km top to bottom', Math.abs(heightKm - 400) < 1e-6,
	      `${heightKm.toFixed(4)} km`);
	check('...and less latitude north of the radar than south (Mercator stretches poleward)',
	      fe.north - KDAX_EXACT.lat < KDAX_EXACT.lat - fe.south,
	      `${(fe.north - KDAX_EXACT.lat).toFixed(5)} vs ${(KDAX_EXACT.lat - fe.south).toFixed(5)}`);
	const xe = frameExtent(XEBY);
	const re = radarExtent(XEBY);
	check('an X-band frame keeps its range box',
	      xe.west === re.west && xe.east === re.east && xe.north === re.north
	      && xe.south === re.south);
	for (const bad of [0, -1, NaN, Infinity]) {
		let threw = false;
		try { frameExtent({ ...KDAX, frame_half_m: bad }); } catch (e) {
			threw = e instanceof RangeError;
		}
		check(`a frame half width of ${String(bad)} is refused`, threw);
	}

	console.log(failures.length
		? `\n${failures.length} FAILED: ${failures.join(', ')}`
		: '\nall radar-geo assertions passed');
	return failures.length ? 1 : 0;
}
