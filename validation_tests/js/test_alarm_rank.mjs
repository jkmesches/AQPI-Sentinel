/**
 * Front-page alarm ordering and filtering.
 *
 * Run:  node validation_tests/js/run_alarm_rank.mjs
 *
 * The dashboard shows the top few alarms, most severe first, with acknowledged
 * ones hidden. Each of those three behaviours has a way to go wrong that looks
 * fine on screen:
 *
 *   - ordering by arrival buries a critical under a pile of warns;
 *   - hiding acknowledged alarms is correct, but hiding them from the COUNT
 *     makes the dashboard under-report the state of the system;
 *   - capping the list is correct, but capping it silently means problems
 *     vanish with nothing saying they exist.
 *
 * A dashboard that shows fewer problems than there are is worse than one that
 * shows too many, so the assertions below lean on what must remain VISIBLE and
 * COUNTED, not just on what gets drawn.
 */
export function runTests(mod) {
	const { rankAlarms, compareAlarms, SEV_RANK } = mod;
	const failures = [];
	const check = (label, cond, detail = '') => {
		console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`);
		if (!cond) failures.push(label);
	};

	let seq = 0;
	const A = (severity, opts = {}) => ({
		id: opts.id ?? ++seq,
		severity,
		opened_at: opts.opened_at ?? '2026-08-31T12:00:00Z',
		ack: opts.ack ?? null,
		check_id: 'layer1.product.x', target: 'x', stage: 'L1', message: 'm'
	});
	const ids = (rows) => rows.map((r) => r.id);

	// ---- 1. severity ordering -------------------------------------------
	let r = rankAlarms([A('info'), A('warn'), A('critical'), A('warn')]);
	check('critical sorts to the top',
	      r.shown[0].severity === 'critical', r.shown.map((a) => a.severity).join(','));
	check('info sorts to the bottom',
	      r.shown[r.shown.length - 1].severity === 'info', r.shown.map((a) => a.severity).join(','));
	check('severity order is critical > warn > info',
	      SEV_RANK.critical > SEV_RANK.warn && SEV_RANK.warn > SEV_RANK.info);

	// A critical that arrived last must still lead. This is the whole point.
	r = rankAlarms([
		A('warn',     { id: 1, opened_at: '2026-08-31T08:00:00Z' }),
		A('warn',     { id: 2, opened_at: '2026-08-31T09:00:00Z' }),
		A('critical', { id: 3, opened_at: '2026-08-31T23:00:00Z' })
	]);
	check('the newest critical still outranks older warns', r.shown[0].id === 3, String(ids(r.shown)));

	// ---- 2. tie-break: oldest first within a severity --------------------
	r = rankAlarms([
		A('critical', { id: 1, opened_at: '2026-08-31T20:00:00Z' }),
		A('critical', { id: 2, opened_at: '2026-08-31T06:00:00Z' }),
		A('critical', { id: 3, opened_at: '2026-08-31T13:00:00Z' })
	]);
	check('same severity sorts oldest first',
	      JSON.stringify(ids(r.shown)) === JSON.stringify([2, 3, 1]), String(ids(r.shown)));

	// ---- 3. acknowledged alarms ------------------------------------------
	const ACK = { acked_by: 'someone', acked_at: '2026-08-31T12:30:00Z' };
	const mixed = [
		A('critical', { id: 1, ack: ACK }),
		A('warn',     { id: 2 }),
		A('info',     { id: 3, ack: ACK })
	];
	r = rankAlarms(mixed);
	check('acknowledged alarms are hidden by default',
	      JSON.stringify(ids(r.shown)) === JSON.stringify([2]), String(ids(r.shown)));
	check('...but are still counted, so the toggle can be offered', r.acked === 2, String(r.acked));
	r = rankAlarms(mixed, { showAcked: true });
	check('showAcked brings them back, still severity-ordered',
	      JSON.stringify(ids(r.shown)) === JSON.stringify([1, 2, 3]), String(ids(r.shown)));

	// An acknowledged critical is hidden from the list but must never be
	// erased: it stays in the acked count and in the caller's total.
	r = rankAlarms([A('critical', { id: 9, ack: ACK })]);
	check('a lone acknowledged critical leaves the list empty', r.shown.length === 0);
	check('...and is still reported as acknowledged, not as nothing',
	      r.acked === 1, String(r.acked));

	// ---- 4. the cap must not hide things silently ------------------------
	const eight = [1, 2, 3, 4, 5, 6, 7, 8].map((i) =>
		A('warn', { id: i, opened_at: `2026-08-31T0${i}:00:00Z` }));
	r = rankAlarms(eight, { limit: 5 });
	check('the list is capped at the limit', r.shown.length === 5, String(r.shown.length));
	check('the remainder is counted, not dropped', r.overflow === 3, String(r.overflow));
	check('shown + overflow accounts for every visible alarm',
	      r.shown.length + r.overflow === eight.length);
	check('the full ranking is still available to the caller',
	      r.ranked.length === eight.length, String(r.ranked.length));
	check('the capped rows are the MOST urgent, not the first five given',
	      JSON.stringify(ids(r.shown)) === JSON.stringify([1, 2, 3, 4, 5]), String(ids(r.shown)));

	// Overflow counts only what is actually withheld from view.
	r = rankAlarms(eight.slice(0, 3), { limit: 5 });
	check('no overflow when everything fits', r.overflow === 0, String(r.overflow));

	// ---- 5. it must not mutate the store's array -------------------------
	// sentinel.alarms is reactive state; sorting in place would reorder it
	// under Svelte and desynchronise every other consumer.
	// Must be exercised with showAcked:true. On the default path the internal
	// .filter() already returns a fresh array, so an in-place sort is invisible
	// there and the mutation survives; showAcked:true is the path where the
	// store's own array reaches .sort() directly.
	for (const showAcked of [false, true]) {
		const original = [A('info', { id: 1 }), A('critical', { id: 2 })];
		const before = ids(original);
		rankAlarms(original, { showAcked });
		check(`the input array is not reordered in place (showAcked=${showAcked})`,
		      JSON.stringify(ids(original)) === JSON.stringify(before), String(ids(original)));
	}

	// ---- 6. degenerate input --------------------------------------------
	for (const [label, val] of [['null', null], ['undefined', undefined], ['empty', []]]) {
		const rr = rankAlarms(val);
		check(`${label} input yields an empty, safe result`,
		      rr.shown.length === 0 && rr.overflow === 0 && rr.acked === 0);
	}
	r = rankAlarms([A('nonsense'), A('critical')]);
	check('an unknown severity is ranked last but NEVER dropped',
	      r.shown.length === 2 && r.shown[0].severity === 'critical',
	      r.shown.map((a) => a.severity).join(','));
	r = rankAlarms([A('warn', { id: 1, opened_at: 'not-a-date' }), A('warn', { id: 2 })]);
	check('an unparseable date does not drop or crash the row',
	      r.shown.length === 2, String(ids(r.shown)));

	console.log(failures.length
		? `\n${failures.length} FAILED: ${failures.join(', ')}`
		: '\nall alarm-ranking assertions passed');
	return failures.length ? 1 : 0;
}
