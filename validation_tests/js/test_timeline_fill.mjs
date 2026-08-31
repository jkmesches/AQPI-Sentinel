/**
 * Timeline cell fill — proportional-density encoding.
 *
 * Run:  node validation_tests/js/run_timeline_fill.mjs
 * (that wrapper compiles the TS with esbuild first, then imports this)
 *
 * Why this exists: the encoding has two silent failure modes, and both look
 * fine on a casual glance at the grid.
 *
 *   Guessing LOW  — a rare failure rounds to a 0px band and vanishes. The grid
 *                   then reports "all green" for a day that contained a real
 *                   outage. This is the worse of the two by a wide margin, and
 *                   MIN_BAD_PX is the only thing preventing it.
 *   Guessing HIGH — every cell reads solid, which is where we started: coarse
 *                   grains looking uniformly broken when they were not.
 *
 * Neither throws, neither shows up in a typecheck, and neither is visible
 * unless you happen to be looking at the one affected cell. So they are pinned
 * here instead.
 */
export function runTests(mod) {
	const { cellFill, badFraction, cellRatioText, MIN_BAD_PX, STATUS_BG } = mod;
	const failures = [];
	const check = (label, cond, detail = '') => {
		console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`);
		if (!cond) failures.push(label);
	};

	const H = 16;                                    // the drawn cell height (ROW_H - 6)
	const cell = (status, n, counts = {}) => ({ status, n, ...counts });
	/** The bad band's height as a % of the cell, parsed back out of the CSS. */
	const bandPct = (css) => {
		if (!css.startsWith('linear-gradient')) return 100;   // flat colour = solid
		const m = css.match(/0 ([\d.]+)%/);
		return m ? parseFloat(m[1]) : NaN;
	};

	// ---- 1. the load-bearing floor ---------------------------------------
	check('MIN_BAD_PX is non-zero', MIN_BAD_PX > 0, `got ${MIN_BAD_PX}`);

	// 1 bad run in 1000 is the real worst case measured at 1d grain.
	let css = cellFill(cell('fail', 1000, { n_fail: 1 }), 'fail', H);
	let px = (bandPct(css) / 100) * H;
	check('1 bad run in 1000 still draws a visible band',
	      px >= MIN_BAD_PX - 0.001, `${px.toFixed(2)}px, floor ${MIN_BAD_PX}`);

	// Sweep every plausible sparse ratio: none may round away.
	let vanished = [];
	for (const n of [24, 48, 288, 720, 1440, 2884]) {
		for (const bad of [1, 2, 3]) {
			const p = (bandPct(cellFill(cell('fail', n, { n_fail: bad }), 'fail', H)) / 100) * H;
			if (!(p >= MIN_BAD_PX - 0.001)) vanished.push(`${bad}/${n}=${p.toFixed(2)}px`);
		}
	}
	check('no sparse ratio rounds the band away', vanished.length === 0, vanished.join(' '));

	// ---- 2. proportionality (the point of the encoding) -------------------
	check('half a bucket bad fills about half the cell',
	      Math.abs(bandPct(cellFill(cell('fail', 100, { n_fail: 50 }), 'fail', H)) - 50) <= 7,
	      `${bandPct(cellFill(cell('fail', 100, { n_fail: 50 }), 'fail', H)).toFixed(1)}%`);

	const q = bandPct(cellFill(cell('fail', 1440, { n_fail: 179 }), 'fail', H));  // real comp_now cell
	check('12.4% bad fills ~1/8 of the cell', q > 6 && q < 25, `${q.toFixed(1)}%`);

	// Monotonic: more bad runs must never draw a shorter band.
	let prev = -1, mono = true;
	for (const bad of [1, 10, 50, 100, 200, 400, 700, 719, 720]) {
		const p = bandPct(cellFill(cell('fail', 720, { n_fail: bad }), 'fail', H));
		if (p < prev - 0.001) mono = false;
		prev = p;
	}
	check('band height is monotonic in the bad count', mono);

	// ---- 3. solid ends of the range --------------------------------------
	check('100% bad is a flat colour, not a gradient',
	      cellFill(cell('fail', 720, { n_fail: 720 }), 'fail', H) === STATUS_BG['fail']);
	check('98.9% bad (real XEBY day) reads essentially solid',
	      bandPct(cellFill(cell('fail', 720, { n_fail: 712 }), 'fail', H)) >= 95);

	// ---- 4. ambiguity must fail toward VISIBLE ----------------------------
	// A server predating the count columns sends status only. Rendering those
	// as an empty/green cell would silently erase all historical failures.
	check('pre-counts payload renders solid, never empty',
	      cellFill(cell('fail', 720), 'fail', H) === STATUS_BG['fail']);
	check('inconsistent payload (fail with n_fail=0) renders solid',
	      cellFill(cell('fail', 720, { n_fail: 0 }), 'fail', H) === STATUS_BG['fail']);
	check('count exceeding n renders solid',
	      cellFill(cell('fail', 10, { n_fail: 99 }), 'fail', H) === STATUS_BG['fail']);
	// Assert on badFraction directly: cellFill's `frac >= 1` early-return hides
	// a missing clamp, so testing only through cellFill leaves it unguarded.
	check('badFraction never exceeds 1',
	      badFraction(cell('fail', 10, { n_fail: 99 })) === 1,
	      String(badFraction(cell('fail', 10, { n_fail: 99 }))));

	// ---- 5. non-defect statuses stay flat --------------------------------
	for (const st of ['pass', 'skip', 'unknown']) {
		check(`${st} is a flat colour`,
		      cellFill(cell(st, 720), st, H) === STATUS_BG[st]);
	}
	check('empty bucket contributes no fill', badFraction(undefined) === 0);
	check('zero-run bucket contributes no fill', badFraction(cell('fail', 0)) === 0);

	// ---- 6. geometry edge cases ------------------------------------------
	// A row shorter than the floor must clamp to solid rather than emit a
	// gradient stop above 100%, which browsers render unpredictably.
	const short = cellFill(cell('fail', 720, { n_fail: 1 }), 'fail', 2);
	check('row shorter than the floor clamps to solid', short === STATUS_BG['fail'], short);
	let overflow = [];
	for (const h of [1, 2, 3, 4, 6, 10, 16, 22, 40]) {
		const p = bandPct(cellFill(cell('fail', 1000, { n_fail: 1 }), 'fail', h));
		if (!(p > 0 && p <= 100)) overflow.push(`h=${h}:${p}`);
	}
	check('band stays within 0..100% at every row height',
	      overflow.length === 0, overflow.join(' '));

	// ---- 7. warn and error use their own colour and their own count -------
	check('error cell bands on n_error',
	      bandPct(cellFill(cell('error', 1440, { n_error: 14, n_fail: 0 }), 'error', H)) < 30);
	check('error cell uses the error colour',
	      cellFill(cell('error', 1440, { n_error: 14 }), 'error', H).includes(STATUS_BG['error']));
	check('warn cell bands on n_warn',
	      bandPct(cellFill(cell('warn', 48, { n_warn: 4 }), 'warn', H)) < 40);
	// The remainder above the band must be the pass colour, not transparent —
	// otherwise a partial cell reads as a gap in coverage.
	check('remainder above the band is the pass colour',
	      cellFill(cell('fail', 720, { n_fail: 10 }), 'fail', H).includes(STATUS_BG['pass']));

	// ---- 8. tooltip agrees with the fill ---------------------------------
	check('tooltip reports the same ratio the fill encodes',
	      cellRatioText(cell('fail', 720, { n_fail: 712 })) === '712 of 720 runs FAIL (99%)',
	      cellRatioText(cell('fail', 720, { n_fail: 712 })));
	check('tooltip singularises one run',
	      cellRatioText(cell('fail', 1, { n_fail: 1 })) === '1 of 1 run FAIL (100%)',
	      cellRatioText(cell('fail', 1, { n_fail: 1 })));
	check('tooltip on a pass cell just counts runs',
	      cellRatioText(cell('pass', 288)) === '288 runs', cellRatioText(cell('pass', 288)));
	check('tooltip on an empty bucket is blank', cellRatioText(undefined) === '');

	console.log(failures.length
		? `\n${failures.length} FAILED: ${failures.join(', ')}`
		: '\nall timeline-fill assertions passed');
	return failures.length ? 1 : 0;
}
