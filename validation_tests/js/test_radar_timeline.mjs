/**
 * Shared multi-radar timebase — merge rule.
 *
 * Run:  node validation_tests/js/run_radar_timeline.mjs
 * (that wrapper compiles the TS with esbuild first, then imports this)
 *
 * Why this exists: both maps used to drive the scrubber from the FIRST
 * active radar's frame list and let the others snap to the nearest frame.
 * With several radars that leaves every other one permanently approximated.
 * The merge rule is the whole fix, and it has two failure modes that are
 * silent rather than loud — merging too aggressively hides genuinely
 * distinct sweeps, merging too little makes the scrubber N times denser than
 * the data. Both are pinned here.
 */
export function runTests(mod) {
	const { mergeTimelines } = mod;
	const failures = [];
	const check = (label, cond, detail = '') => {
		console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`);
		if (!cond) failures.push(label);
	};

	const T = (s) => new Date(s).toISOString();

	// 1. single radar: timeline is just its own frames
	let out = mergeTimelines([[T('2026-08-28T12:00:00Z'), T('2026-08-28T12:02:00Z')]]);
	check('single radar keeps every frame', out.length === 2, `got ${out.length}`);

	// 2. two radars, offset by 30s within one cadence -> ONE step per cycle
	out = mergeTimelines([
		[T('2026-08-28T12:00:00Z'), T('2026-08-28T12:02:00Z')],
		[T('2026-08-28T12:00:30Z'), T('2026-08-28T12:02:30Z')]
	]);
	check('near-simultaneous sweeps merge to one step per cycle',
	      out.length === 2, `got ${out.length}, expected 2`);

	// 3. the merged instant sits BETWEEN the two radars, not on one of them
	const mid = new Date(out[0].ts).getTime();
	const a = new Date(T('2026-08-28T12:00:00Z')).getTime();
	const b = new Date(T('2026-08-28T12:00:30Z')).getTime();
	check('merged step is the midpoint, favouring neither radar',
	      mid > a && mid < b, `${new Date(mid).toISOString()}`);

	// 4. genuinely distinct sweeps must NOT merge
	out = mergeTimelines([
		[T('2026-08-28T12:00:00Z'), T('2026-08-28T12:02:00Z'), T('2026-08-28T12:04:00Z')]
	]);
	check('120s-apart frames stay separate', out.length === 3, `got ${out.length}`);

	// 5. union: a radar with extra history contributes its older frames
	out = mergeTimelines([
		[T('2026-08-28T12:00:00Z')],
		[T('2026-08-28T11:50:00Z'), T('2026-08-28T12:00:05Z')]
	]);
	check('union covers the radar with deeper history', out.length === 2, `got ${out.length}`);

	// 6. ordering is chronological
	out = mergeTimelines([
		[T('2026-08-28T12:04:00Z'), T('2026-08-28T12:00:00Z')],
		[T('2026-08-28T12:02:00Z')]
	]);
	const ordered = out.every((s, i) => i === 0 ||
		new Date(s.ts).getTime() > new Date(out[i - 1].ts).getTime());
	check('steps come out in chronological order', ordered && out.length === 3, `n=${out.length}`);

	// 7. indices are contiguous from 0 (the scrubber uses them directly)
	check('indices are 0..n-1', out.every((s, i) => s.i === i));

	// 8. degenerate inputs don't throw
	check('empty input yields empty timeline', mergeTimelines([]).length === 0);
	check('nulls and junk are ignored',
	      mergeTimelines([[null, 'not-a-date', T('2026-08-28T12:00:00Z')]]).length === 1);

	// 9. labels are populated — the scrubber renders time/date directly
	out = mergeTimelines([[T('2026-08-28T12:34:56Z')]]);
	check('label fields are filled',
	      out[0].time === '12:34:56' && out[0].date === '28-Aug-2026' && out[0].day === 'Fri',
	      `${out[0].day} ${out[0].date} ${out[0].time}`);

	console.log(failures.length ? `\nFAILED: ${failures.join(', ')}` : '\nPASS');
	return failures.length ? 1 : 0;
}
