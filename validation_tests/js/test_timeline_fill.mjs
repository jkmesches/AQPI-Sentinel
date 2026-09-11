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
	const { cellFill, badFraction, skipFraction, cellRatioText, MIN_BAD_PX, STATUS_BG } = mod;
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
	/**
	 * Height of the band drawn in a SPECIFIC colour.
	 *
	 * bandPct alone is not enough once a cell can hold more than two colours:
	 * it matches whichever band starts at 0, so a gradient whose bad band had
	 * vanished and left grey at the bottom still reported "100%". A mutation
	 * that let skips squeeze the bad band out of existence passed the whole
	 * suite because of exactly that.
	 */
	const colourPct = (css, colour) => {
		if (!css.startsWith('linear-gradient')) return css === colour ? 100 : 0;
		const esc = colour.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
		const m = css.match(new RegExp(esc + ' (?:0|([\\d.]+)%) ([\\d.]+)%'));
		if (!m) return 0;
		return parseFloat(m[2]) - (m[1] ? parseFloat(m[1]) : 0);
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

	// ---- 5. non-defect statuses stay flat when that is all they are ------
	for (const st of ['pass', 'skip', 'unknown']) {
		check(`${st} with nothing mixed in is a flat colour`,
		      cellFill(cell(st, 720), st, H) === STATUS_BG[st]);
	}

	// ---- 5b. SKIPS ARE NOT PASSES ----------------------------------------
	// A skip means the check ran and declined to judge — usually a cascade
	// demote while an upstream was unhealthy. Painting it green asserts we
	// looked and found nothing wrong. Measured over 24h at 1h grain, 141 of
	// 1,093 buckets held a skip; 59 drew as solid green and 2 as a green
	// remainder, so a blind spot was indistinguishable from a healthy hour.
	const greyOf = (css) => css.includes(STATUS_BG['skip']);

	// The dominant case: worst status is `pass` (pass outranks skip), so the
	// cell used to be drawn as one flat green block with the skips erased.
	const mixed = cellFill(cell('pass', 100, { n_skip: 40 }), 'pass', H);
	check('a pass bucket containing skips is not solid green',
	      mixed !== STATUS_BG['pass'], mixed);
	check('...and shows grey for the skipped share', greyOf(mixed), mixed);
	check('...and still shows green for the share that really passed',
	      mixed.includes(STATUS_BG['pass']), mixed);

	// The second case: the remainder above a bad band was hard-coded to pass.
	const badMix = cellFill(cell('fail', 100, { n_fail: 20, n_skip: 60 }), 'fail', H);
	check('the remainder above a bad band is not assumed to be pass',
	      greyOf(badMix), badMix);
	check('a bad band still starts at the bottom of the cell',
	      bandPct(badMix) > 0 && bandPct(badMix) < 100, `${bandPct(badMix)}%`);

	// An outage must stay visible even when most of the bucket is skipped —
	// skips yield to the bad band, never the reverse.
	const rareBad = cellFill(cell('fail', 1000, { n_fail: 1, n_skip: 999 }), 'fail', H);
	// Assert on the FAIL colour specifically. Measuring "the band starting at
	// 0" would accept a cell whose bad band had been squeezed out entirely,
	// leaving grey at the bottom — which is the failure this pins.
	check('one failure among 999 skips still draws its floor',
	      (colourPct(rareBad, STATUS_BG['fail']) / 100) * H >= MIN_BAD_PX - 0.001,
	      `${((colourPct(rareBad, STATUS_BG['fail']) / 100) * H).toFixed(2)}px of fail`);
	check('...and the cell still contains the fail colour at all',
	      rareBad.includes(STATUS_BG['fail']), rareBad);
	check('...without pushing the cell past 100%',
	      !/(\d{3,}(\.\d+)?)%/.test(rareBad.replace(/100%/g, '')), rareBad);

	// All-skip stays flat grey whichever way it is described.
	check('an all-skip bucket is flat grey',
	      cellFill(cell('skip', 50, { n_skip: 50 }), 'skip', H) === STATUS_BG['skip']);

	// Ambiguity resolves OPPOSITE to badFraction: grey means "no data", so
	// inventing it would be its own lie. Absent counts must not grey a cell.
	check('skipFraction is 0 when the server sent no count',
	      skipFraction(cell('pass', 720)) === 0);
	check('...except on a skip-status cell, which can only be all skips',
	      skipFraction(cell('skip', 720)) === 1);
	check('a pre-counts pass cell stays flat green',
	      cellFill(cell('pass', 720), 'pass', H) === STATUS_BG['pass']);
	check('skipFraction never exceeds 1',
	      skipFraction(cell('pass', 10, { n_skip: 99 })) === 1);
	check('skipFraction ignores a negative count',
	      skipFraction(cell('pass', 10, { n_skip: -5 })) === 0);
	check('skipFraction on an empty bucket is 0', skipFraction(undefined) === 0);

	// Proportional, like the bad band.
	const q1 = cellFill(cell('pass', 100, { n_skip: 25 }), 'pass', 40);
	const q3 = cellFill(cell('pass', 100, { n_skip: 75 }), 'pass', 40);
	const greyPct = (css) => colourPct(css, STATUS_BG['skip']);
	check('the grey band grows with the skip share',
	      greyPct(q3) > greyPct(q1),
	      `25%->${greyPct(q1).toFixed(0)}%, 75%->${greyPct(q3).toFixed(0)}%`);
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

	// ---- 7b. ERRORS ARE NOT PASSES ---------------------------------------
	// The same assumption as the skip bug, one band higher up. A cell whose
	// worst status is `fail` drew its fail band, its skip band, and then
	// everything left over in the PASS colour — including runs that errored.
	//
	// XEBY on 2026-09-11 is the case in point: 673 fail, 23 error, 20 skip and
	// not one pass in 24 hours. Its 22:00 bucket held 12 fail, 2 error, 1 skip
	// of 15 runs, and drew roughly an eighth of its height green. A radar down
	// for weeks showed green for the runs where the probe itself failed.
	const xeby = cellFill(cell('fail', 15, { n_fail: 12, n_error: 2, n_skip: 1 }), 'fail', H);
	check('a bucket with no passing runs draws NO green',
	      !xeby.includes(STATUS_BG['pass']), xeby);
	check('...and draws the error share in the error colour',
	      xeby.includes(STATUS_BG['error']), xeby);
	check('...with fail still at the bottom of the cell',
	      colourPct(xeby, STATUS_BG['fail']) > 0 && bandPct(xeby) > 0, xeby);
	check('...and the skip share still grey', xeby.includes(STATUS_BG['skip']), xeby);

	// Bands must be ordered by severity, bottom-up, matching the server's
	// worst_rank. Reading them out of the gradient keeps that pinned.
	const order = ['fail', 'error', 'warn'].map((s) => xeby.indexOf(STATUS_BG[s]))
		.filter((i) => i >= 0);
	check('bands run most-severe first from the bottom',
	      order.every((v, i) => i === 0 || order[i - 1] < v), JSON.stringify(order));

	// A warn mixed into a failing bucket is its own band too, not green.
	const warnMix = cellFill(cell('fail', 100, { n_fail: 50, n_warn: 50 }), 'fail', H);
	check('warn runs inside a fail bucket are not painted green',
	      !warnMix.includes(STATUS_BG['pass']), warnMix);
	check('...they are painted warn', warnMix.includes(STATUS_BG['warn']), warnMix);

	// One error among a thousand fails still has to be visible, for the same
	// reason MIN_BAD_PX exists for the fail band itself.
	const rareErr = cellFill(cell('fail', 1000, { n_fail: 999, n_error: 1 }), 'fail', H);
	check('one error among 999 fails still draws a band',
	      rareErr.includes(STATUS_BG['error']), rareErr);
	check('...without pushing the cell past 100%',
	      !/(\d{3,}(\.\d+)?)%/.test(rareErr.replace(/100%/g, '')), rareErr);

	// The green that remains must be real: a bucket that genuinely contains
	// passes still shows them, or the fix would have traded one lie for another.
	const realPass = cellFill(cell('fail', 100, { n_fail: 10, n_error: 10, n_skip: 10 }), 'fail', H);
	check('a bucket that really does contain passes still shows green',
	      realPass.includes(STATUS_BG['pass']), realPass);

	// Ambiguity keeps resolving toward visibility, not toward invention.
	check('a pre-counts fail cell is still solid rather than part-green',
	      cellFill(cell('fail', 720), 'fail', H) === STATUS_BG['fail']);

	// Pass is the ONE band with no floor. Giving it one rounded a 1.1% healthy
	// share up to a visible slice and made an almost-entirely-broken cell read
	// as less broken — over-reporting health, the one direction this encoding
	// must never fail in. Caught by the 98.9% assertion above; pinned here so
	// the reason survives.
	const almostAllBad = cellFill(cell('fail', 720, { n_fail: 719 }), 'fail', H);
	check('a 0.1% healthy share does not earn a green pixel',
	      !almostAllBad.includes(STATUS_BG['pass']), almostAllBad);
	// ...but defects keep theirs, in the opposite direction.
	const tinyErr = cellFill(cell('fail', 720, { n_fail: 719, n_error: 1 }), 'fail', H);
	check('a 0.1% error share DOES earn a band', tinyErr.includes(STATUS_BG['error']),
	      tinyErr);

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
