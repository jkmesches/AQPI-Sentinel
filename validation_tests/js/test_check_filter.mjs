/**
 * The dashboard's source filters.
 *
 * Run:  node validation_tests/js/run_check_filter.mjs
 *
 * The backend classifies each check (reads_radarca, nexrad), and the filter
 * hides by that classification alone. A check the backend has not classified
 * must never be hidden, or a backend older than a flag would hide whatever
 * the filter guessed.
 */
export function runTests(mod) {
	const { hiddenCheckIds, sourceCounts } = mod;
	const failures = [];
	const check = (label, cond, detail = '') => {
		console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`);
		if (!cond) failures.push(label);
	};

	const catalog = [
		{ id: 'layer2.radar.XEBY', reads_radarca: true, nexrad: false },
		{ id: 'layer2.backend.XEBY', reads_radarca: false, nexrad: false },
		{ id: 'layer3.input.KDAX', reads_radarca: false, nexrad: true },
		{ id: 'layer3.composite.KDAX', reads_radarca: false, nexrad: true },
		{ id: 'layer0.net.dns', reads_radarca: false, nexrad: false },
		{ id: 'from.an.older.backend' }
	];
	const ids = (f) => [...hiddenCheckIds(catalog, f)].sort().join(',');

	check('nothing is hidden with both filters off',
	      ids({ hideRadarca: false, hideNexrad: false }) === '');
	check('hiding RadarCA hides only the RadarCA reading',
	      ids({ hideRadarca: true, hideNexrad: false }) === 'layer2.radar.XEBY',
	      ids({ hideRadarca: true, hideNexrad: false }));
	check('hiding NEXRAD hides both NEXRAD checks',
	      ids({ hideRadarca: false, hideNexrad: true })
	      === 'layer3.composite.KDAX,layer3.input.KDAX');
	check('both together hide the union',
	      hiddenCheckIds(catalog, { hideRadarca: true, hideNexrad: true }).size === 3);
	check('an unclassified check is never hidden',
	      !hiddenCheckIds(catalog, { hideRadarca: true, hideNexrad: true })
	        .has('from.an.older.backend'));
	check('a non-boolean flag is not read as true',
	      hiddenCheckIds([{ id: 'x', reads_radarca: 'yes', nexrad: 1 }],
	                     { hideRadarca: true, hideNexrad: true }).size === 0);
	const counts = sourceCounts(catalog);
	check('the counts say what each filter could hide',
	      counts.radarca === 1 && counts.nexrad === 2, JSON.stringify(counts));
	check('an empty catalog offers neither filter',
	      JSON.stringify(sourceCounts([])) === '{"radarca":0,"nexrad":0}');

	console.log(failures.length
		? `\n${failures.length} FAILED: ${failures.join(', ')}`
		: '\nall check-filter assertions passed');
	return failures.length ? 1 : 0;
}
