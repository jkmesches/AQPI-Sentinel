/**
 * Sparkline series batching.
 *
 * Run:  node validation_tests/js/run_metrics_batching.mjs
 *
 * The sparkline refresh sent every series in one request. `metrics_recent`
 * raises 400 above `_MAX_SERIES`, so on a deployment with more checks than
 * that the whole poll failed and no sample reached the browser after the
 * initial seed. Measured on cira-aqpi: 84 checks against a cap of 64, with
 * the boundary confirmed by hand at 64 returning 200 and 65 returning 400.
 * The two smaller deployments sit under the cap at 45 and 10, so the bug
 * lived only on the largest one.
 *
 * A batching bug that drops one series is invisible, because the trace it
 * feeds stops updating while the traces around it keep moving. The assertions
 * below therefore check that each key survives once and in order, at the
 * sizes around the cap where an off-by-one would hide.
 */
export function runTests(mod) {
	const { chunkSeries, mergeSeries, METRICS_SERIES_MAX } = mod;
	const failures = [];
	const check = (label, cond, detail = '') => {
		console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`);
		if (!cond) failures.push(label);
	};

	const keys = (n) => Array.from({ length: n }, (_, i) => `check.${i}|age_s`);

	// --- 1. the cap matches the backend ----------------------------------
	check('cap is the backend _MAX_SERIES', METRICS_SERIES_MAX === 64,
	      String(METRICS_SERIES_MAX));

	// --- 2. nothing is lost, duplicated or reordered ----------------------
	for (const n of [0, 1, 63, 64, 65, 84, 128, 129]) {
		const src = keys(n);
		const batches = chunkSeries(src);
		const flat = batches.flat();
		check(`${n} series round-trips through batching`,
		      flat.length === n && flat.every((k, i) => k === src[i]),
		      `${batches.length} batch(es)`);
		check(`...and no batch exceeds the cap`,
		      batches.every((b) => b.length <= METRICS_SERIES_MAX && b.length > 0),
		      batches.map((b) => b.length).join('+') || 'none');
	}

	// --- 3. the boundary the production deployment sits astride -----------
	check('64 series is one request', chunkSeries(keys(64)).length === 1);
	check('65 series is two', chunkSeries(keys(65)).length === 2);
	check("cira-aqpi's 84 series is two", chunkSeries(keys(84)).length === 2);

	// --- 4. a size that would loop forever is refused ---------------------
	for (const bad of [0, -1, 1.5, NaN]) {
		let threw = false;
		try { chunkSeries(keys(3), bad); } catch (e) { threw = e instanceof RangeError; }
		check(`size ${String(bad)} is rejected`, threw);
	}

	// --- 5. merging keeps every key and every sample ----------------------
	const merged = mergeSeries([
		{ 'a|age_s': [{ ts: 't1', value: 1 }] },
		{ 'b|age_s': [{ ts: 't2', value: 2 }] },
		{ 'a|age_s': [{ ts: 't3', value: 3 }] }
	]);
	check('merge keeps keys from every batch',
	      Object.keys(merged).sort().join(',') === 'a|age_s,b|age_s',
	      Object.keys(merged).join(','));
	check('...and concatenates a key seen twice rather than replacing it',
	      merged['a|age_s'].length === 2,
	      JSON.stringify(merged['a|age_s']));

	// Empty and absent series must not create keys: the renderer treats an
	// empty array as "this check reported nothing", which is a real state.
	const sparse = mergeSeries([{ 'a|age_s': [] }, {}, { 'b|age_s': [{ ts: 't', value: 0 }] }]);
	check('empty arrays do not create keys',
	      Object.keys(sparse).join(',') === 'b|age_s', Object.keys(sparse).join(','));
	check('an empty batch list merges to an empty map',
	      Object.keys(mergeSeries([])).length === 0);

	console.log(failures.length
		? `\n${failures.length} FAILED: ${failures.join(', ')}`
		: '\nall metrics-batching assertions passed');
	return failures.length ? 1 : 0;
}
