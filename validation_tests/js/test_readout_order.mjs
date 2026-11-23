/**
 * Which overlay the cursor readout reads.
 *
 * Run:  node validation_tests/js/run_readout_order.mjs
 *
 * Joseph, 2026-11-23: with a composite on, the probe read nothing over a
 * radar's own imagery. The readout read only the composite whenever one was
 * on, while a radar selected after it drew on top. The rule now is the map's
 * paint order, top down, falling through transparent pixels.
 */
export function runTests(mod) {
	const { topFirst, firstReading, anyAbove } = mod;
	const failures = [];
	const check = (label, cond, detail = '') => {
		console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`);
		if (!cond) failures.push(label);
	};
	const ids = (xs) => xs.map((x) => x.layerId).join(',');
	const comp = { layerId: 'comp-overlay-layer' };
	const xeby = { layerId: 'radar-XEBY-layer' };
	const kmux = { layerId: 'radar-KMUX-layer' };
	const base = ['basemap', 'water'];
	const markers = ['range-fill', 'radar-halo', 'radar-point'];

	console.log('paint order:');
	check('a radar added after the composite is read first',
		ids(topFirst([comp, xeby], [...base, 'comp-overlay-layer', 'radar-XEBY-layer', ...markers]))
		=== 'radar-XEBY-layer,comp-overlay-layer');
	check('a composite added after the radar is read first',
		ids(topFirst([comp, xeby], [...base, 'radar-XEBY-layer', 'comp-overlay-layer', ...markers]))
		=== 'comp-overlay-layer,radar-XEBY-layer');
	check('the order given does not matter, only the map\'s',
		ids(topFirst([xeby, comp], [...base, 'comp-overlay-layer', 'radar-XEBY-layer']))
		=== ids(topFirst([comp, xeby], [...base, 'comp-overlay-layer', 'radar-XEBY-layer'])));
	check('a NEXRAD under an X-band is read after it',
		ids(topFirst([kmux, xeby], ['radar-KMUX-layer', 'radar-XEBY-layer']))
		=== 'radar-XEBY-layer,radar-KMUX-layer');
	check('a held frame whose layer is off the map is never read',
		ids(topFirst([comp, xeby], [...base, 'comp-overlay-layer'])) === 'comp-overlay-layer');
	check('nothing on the map reads nothing', topFirst([comp, xeby], base).length === 0);
	check('the input is left alone', (() => {
		const input = [comp, xeby];
		topFirst(input, ['radar-XEBY-layer', 'comp-overlay-layer']);
		return input[0] === comp && input[1] === xeby;
	})());

	console.log('\nwhich scale the legend draws:');
	const radars = new Set(['radar-XEBY-layer', 'tilt-overlay-layer']);
	check('a radar over the composite is above it',
		anyAbove(['basemap', 'comp-overlay-layer', 'radar-XEBY-layer', 'radar-halo'],
			'comp-overlay-layer', radars));
	check('a radar under the composite is not',
		!anyAbove(['basemap', 'radar-XEBY-layer', 'comp-overlay-layer', 'radar-halo'],
			'comp-overlay-layer', radars));
	check('the radar markers do not count as imagery',
		!anyAbove(['comp-overlay-layer', 'radar-halo', 'radar-point'],
			'comp-overlay-layer', radars));
	check('a tilt over the composite is above it',
		anyAbove(['comp-overlay-layer', 'tilt-overlay-layer'], 'comp-overlay-layer', radars));
	check('with no composite on the map, a radar is above it',
		anyAbove(['basemap', 'radar-XEBY-layer'], 'comp-overlay-layer', radars));
	check('with no radar on the map, nothing is',
		!anyAbove(['basemap', 'comp-overlay-layer'], 'comp-overlay-layer', radars));

	console.log('\nreading down through them:');
	const at = { 'radar-XEBY-layer': null, 'comp-overlay-layer': '10.0 to 15.0 dBZ' };
	const got = firstReading([xeby, comp], (l) => at[l.layerId]);
	check('a transparent radar pixel shows the composite under it',
		got?.reading === '10.0 to 15.0 dBZ' && got.layer === comp, JSON.stringify(got));
	const painted = { ...at, 'radar-XEBY-layer': '30.0 to 35.0 dBZ' };
	check('a painted radar pixel is the reading, over the composite',
		firstReading([xeby, comp], (l) => painted[l.layerId])?.layer === xeby);
	const off = { ...at, 'radar-XEBY-layer': 'not on this scale' };
	check('an off-scale pixel on top ends the walk, since it is what is seen',
		firstReading([xeby, comp], (l) => off[l.layerId])?.reading === 'not on this scale');
	check('all transparent reads nothing',
		firstReading([xeby, comp], () => null) === null);
	check('no layers reads nothing', firstReading([], () => 'x') === null);
	let calls = 0;
	firstReading([xeby, comp], () => { calls += 1; return 'hit'; });
	check('the walk stops at the first answer', calls === 1, String(calls));

	console.log(failures.length
		? `\n${failures.length} FAILED: ${failures.join(', ')}`
		: '\nall readout-order assertions passed');
	return failures.length ? 1 : 0;
}
