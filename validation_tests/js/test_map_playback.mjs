// Playback stepping and the blank-frame skip.
//
// Run:  node validation_tests/js/run_map_playback.mjs
//
// Both failures available here animate convincingly. A skip rule with no
// stop condition spins the whole list without painting when every frame is
// blank, and the loop stops being something a reader can interrupt. Skipping
// against a stale or partial activity list jumps to the wrong frames, which
// reads as playback picking steps at random.

export function runTests(mod) {
  const {
    nextInterestingStep, remainingDwell, stepTo,
    ACTIVITY_FLOOR, MAX_SKIP_PER_TICK, FRAME_DWELL_MS
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const BLANK = 0.001;
  const BUSY = 0.08;
  // n frames, with the ones at `blanks` below the floor.
  const act = (n, blanks = []) =>
    Array.from({ length: n }, (_, i) => (blanks.includes(i) ? BLANK : BUSY));

  console.log('with no activity data it steps linearly:');
  {
    check('no data steps by one', nextInterestingStep(0, 10, []) === 1);
    check('...and wraps at the end', nextInterestingStep(9, 10, []) === 0);
    // A partial reading would skip against the wrong frames, so it is used
    // only when it covers the list exactly.
    check('a short list is ignored', nextInterestingStep(0, 10, act(5)) === 1);
    check('a long list is ignored',
      nextInterestingStep(0, 10, act(20, [1, 2, 3])) === 1);
    check('an empty step list stays at zero', nextInterestingStep(5, 0, []) === 0);
  }

  console.log('\nblank frames are skipped when the data covers them:');
  {
    check('one blank is passed over',
      nextInterestingStep(0, 10, act(10, [1])) === 2);
    check('a run of three is passed over',
      nextInterestingStep(0, 10, act(10, [1, 2, 3])) === 4);
    check('a busy next frame is not skipped',
      nextInterestingStep(0, 10, act(10, [5])) === 1);
    check('skipping wraps past the end',
      nextInterestingStep(7, 10, act(10, [8, 9])) === 0,
      String(nextInterestingStep(7, 10, act(10, [8, 9]))));
    check('exactly at the floor is not blank',
      nextInterestingStep(0, 5, [BUSY, ACTIVITY_FLOOR, BUSY, BUSY, BUSY]) === 1);
    check('just under the floor is blank',
      nextInterestingStep(0, 5,
        [BUSY, ACTIVITY_FLOOR - 1e-6, BUSY, BUSY, BUSY]) === 2);
  }

  console.log('\nthe skip is bounded, so a blank list still plays:');
  {
    // The regression this file exists for.
    const allBlank = Array.from({ length: 40 }, () => BLANK);
    const landed = nextInterestingStep(0, 40, allBlank);
    check('a wholly blank list still advances', landed !== 0, String(landed));
    check('...by exactly the skip bound plus one',
      landed === (1 + MAX_SKIP_PER_TICK) % 40, String(landed));
    check('the bound is six', MAX_SKIP_PER_TICK === 6, String(MAX_SKIP_PER_TICK));
    // Running the loop forward must cover the list rather than cycling a
    // few indices forever.
    let at = 0;
    const seen = new Set([0]);
    for (let k = 0; k < 200; k++) {
      at = nextInterestingStep(at, 40, allBlank);
      seen.add(at);
    }
    check('...and repeated ticks reach every frame', seen.size === 40,
      `${seen.size}/40`);
  }

  console.log('\nthe current frame is never skipped onto:');
  {
    // A single-frame list has nowhere else to go, and the `i !== from`
    // guard is what stops the scan there.
    check('one blank frame stays put', nextInterestingStep(0, 1, [BLANK]) === 0);
    check('one busy frame stays put', nextInterestingStep(0, 1, [BUSY]) === 0);
    // An all-blank list used to park. The scan walked the whole way round
    // and landed back on `from`, and returning that stopped playback with
    // the control still reading as playing and the time axis frozen.
    // Seven tilt frames on a clear day are exactly that shape, since the
    // skip bound is six. Fixed on 2026-11-21.
    const allBlank = (n) => Array.from({ length: n }, () => BLANK);
    for (const [from, n] of [[0, 2], [3, 7], [0, 7], [6, 7], [5, 6],
      [0, 3], [2, 4], [0, 12], [11, 12]]) {
      const got = nextInterestingStep(from, n, allBlank(n));
      check(`an all-blank list of ${n} advances from ${from}`,
        got !== from, `stayed on ${got}`);
      check(`...and ${got} is a real step`, got >= 0 && got < n, String(got));
    }
    check('an all-blank pair advances to the other frame',
      nextInterestingStep(0, 2, [BLANK, BLANK]) === 1,
      String(nextInterestingStep(0, 2, [BLANK, BLANK])));
    check('an all-blank seven advances by one',
      nextInterestingStep(3, 7, allBlank(7)) === 4,
      String(nextInterestingStep(3, 7, allBlank(7))));
    check('and wraps at the end',
      nextInterestingStep(6, 7, allBlank(7)) === 0,
      String(nextInterestingStep(6, 7, allBlank(7))));
    check('one busy frame among them is still what the scan finds',
      nextInterestingStep(0, 7,
        [BLANK, BLANK, BLANK, BUSY, BLANK, BLANK, BLANK]) === 3);
    // Playing an all-blank list has to keep moving, or the time axis
    // stops while the control says it is running. Walked here rather than
    // asserted at one position, since the old fault appeared only at a
    // particular length.
    for (const n of [2, 3, 5, 6, 7, 8, 13]) {
      const seen = new Set();
      let at = 0;
      for (let tick = 0; tick < n * 2; tick++) {
        at = nextInterestingStep(at, n, allBlank(n));
        seen.add(at);
      }
      check(`playing ${n} blank frames visits more than one`, seen.size > 1,
        `visited ${seen.size}`);
    }
  }

  console.log('\nthe dwell absorbs the time the frame took:');
  {
    check('a fast frame waits out the rest',
      remainingDwell(1000, 1200, 900) === 700, String(remainingDwell(1000, 1200, 900)));
    check('a frame that took the whole dwell waits none',
      remainingDwell(1000, 1900, 900) === 0);
    // The real interval is whichever is longer, the dwell or the load.
    check('a slow frame never waits a negative time',
      remainingDwell(1000, 5000, 900) === 0, String(remainingDwell(1000, 5000, 900)));
    check('an instant frame waits the full dwell',
      remainingDwell(1000, 1000, 900) === 900);
    check('the default dwell is 900 ms', FRAME_DWELL_MS === 900,
      String(FRAME_DWELL_MS));
    check('...and is used when none is given',
      remainingDwell(0, 0) === FRAME_DWELL_MS);
  }

  console.log('\nthe step buttons wrap:');
  {
    check('first goes to zero', stepTo(5, 10, 'first') === 0);
    check('last goes to the end', stepTo(5, 10, 'last') === 9);
    check('next advances', stepTo(5, 10, 'next') === 6);
    check('prev retreats', stepTo(5, 10, 'prev') === 4);
    check('next wraps at the end', stepTo(9, 10, 'next') === 0);
    // Without the + total, this is -1, which reads as "no step".
    check('prev wraps at the start', stepTo(0, 10, 'prev') === 9,
      String(stepTo(0, 10, 'prev')));
    check('an empty list has no step', stepTo(0, 0, 'next') === -1);
    check('...whichever button', stepTo(0, 0, 'first') === -1
      && stepTo(0, 0, 'last') === -1 && stepTo(0, 0, 'prev') === -1);
    check('a single step stays put',
      stepTo(0, 1, 'next') === 0 && stepTo(0, 1, 'prev') === 0);
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall playback assertions passed');
  return failures.length ? 1 : 0;
}
