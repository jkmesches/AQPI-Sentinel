// How large the console draws the things laid over the map.
//
// Run:  node validation_tests/js/run_map_ui_scale.mjs
//
// One number sizes the legend, the compass, the display bar and the cursor
// readout together, and every one of them multiplies a px constant by it.
// So the failures that matter are a stored value that no longer matches an
// offered size, a step that runs off the end of the list, and a scaled
// length of zero, which collapses a bar or a needle to nothing.

export function runTests(mod) {
  const {
    UI_SCALES, DEFAULT_UI_SCALE, normalizeScale, stepScale, scaleName, scaled
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  const smallest = UI_SCALES[0];
  const largest = UI_SCALES[UI_SCALES.length - 1];

  console.log('the offered sizes are a usable set:');
  {
    check('the default is one of them', UI_SCALES.includes(DEFAULT_UI_SCALE));
    check('they rise', UI_SCALES.every((s, i) => i === 0 || s > UI_SCALES[i - 1]));
    check('every one is positive', UI_SCALES.every((s) => s > 0));
    // Below about 0.8 the 12 px labels in this console drop under 10 px,
    // and above 2 a legend at 280 px wide stops fitting a 426 px pane.
    check('none is smaller than 0.8', smallest >= 0.8, String(smallest));
    check('none is larger than 2', largest <= 2, String(largest));
    check('each has a name',
      UI_SCALES.every((s) => typeof scaleName(s) === 'string'
        && scaleName(s).length > 0));
    check('the names are distinct',
      new Set(UI_SCALES.map(scaleName)).size === UI_SCALES.length);
  }

  console.log('\na stored size always resolves to an offered one:');
  {
    check('an offered size is itself', normalizeScale(1.25) === 1.25);
    check('a string from storage parses', normalizeScale('1.5') === 1.5);
    check('a retired size snaps to the nearest',
      UI_SCALES.includes(normalizeScale(1.1)), String(normalizeScale(1.1)));
    // It takes the nearer neighbor in both directions. Snapping always
    // down loses a reader's choice to enlarge, and always to the default
    // discards it entirely.
    check('1.2 snaps up to 1.25',
      normalizeScale(1.2) === 1.25, String(normalizeScale(1.2)));
    check('1.1 snaps down to 1',
      normalizeScale(1.1) === 1, String(normalizeScale(1.1)));
    check('past the top clamps to the largest',
      normalizeScale(8) === largest);
    check('below the bottom clamps to the smallest',
      normalizeScale(0.1) === smallest);
    check('zero is the default', normalizeScale(0) === DEFAULT_UI_SCALE);
    check('a negative is the default', normalizeScale(-2) === DEFAULT_UI_SCALE);
    check('NaN is the default', normalizeScale(NaN) === DEFAULT_UI_SCALE);
    check('infinity is the default',
      normalizeScale(Infinity) === DEFAULT_UI_SCALE);
    check('nothing stored is the default',
      normalizeScale(null) === DEFAULT_UI_SCALE);
    check('junk is the default',
      normalizeScale('large') === DEFAULT_UI_SCALE);
    check('it is idempotent',
      UI_SCALES.concat([0.9, 1.7, 3]).every(
        (s) => normalizeScale(normalizeScale(s)) === normalizeScale(s)));
  }

  console.log('\nstepping stays on the list and inside it:');
  {
    check('up one moves one place',
      stepScale(UI_SCALES[0], 1) === UI_SCALES[1]);
    check('down one moves back',
      stepScale(UI_SCALES[1], -1) === UI_SCALES[0]);
    check('no step is no change', stepScale(1.25, 0) === 1.25);
    check('up from the largest holds',
      stepScale(largest, 1) === largest);
    check('down from the smallest holds',
      stepScale(smallest, -1) === smallest);
    check('a long step clamps rather than wrapping',
      stepScale(smallest, 99) === largest);
    check('a long step back clamps',
      stepScale(largest, -99) === smallest);
    check('a fractional step is truncated, not rounded away',
      stepScale(UI_SCALES[0], 1.9) === UI_SCALES[1]);
    check('NaN steps nowhere', stepScale(1, NaN) === 1);
    check('every result is offered',
      UI_SCALES.every((s) => [-2, -1, 0, 1, 2].every(
        (d) => UI_SCALES.includes(stepScale(s, d)))));
  }

  console.log('\nscaled lengths never collapse:');
  {
    check('the default size changes nothing', scaled(20, 1) === 20);
    check('a legend bar doubles', scaled(20, 2) === 40);
    check('an 11 px label at 1.5 is 17', scaled(11, 1.5) === 17,
      String(scaled(11, 1.5)));
    check('the result is a whole number',
      [9.5, 10.5, 11, 20, 56, 280].every(
        (px) => UI_SCALES.every((s) => Number.isInteger(scaled(px, s)))));
    // A tick 4 px tall at the smallest size rounds to 3, and a hairline
    // at 0 would take the tick off the bar entirely.
    check('nothing rounds to zero',
      [0.5, 1, 2, 4].every((px) => UI_SCALES.every(
        (s) => scaled(px, s) >= 1)));
    check('a bad length is a pixel, not NaN', scaled(NaN, 1) === 1);
    check('an infinite length is a pixel', scaled(Infinity, 1) === 1);
    check('a bad size falls back to the default',
      scaled(20, NaN) === scaled(20, DEFAULT_UI_SCALE));
    check('larger is never smaller',
      UI_SCALES.every((s, i) => i === 0
        || scaled(56, s) >= scaled(56, UI_SCALES[i - 1])));
    // The compass needle and its dial are drawn from two constants, and
    // a needle that outgrew its dial at one size would clip.
    check('the needle stays inside the dial at every size',
      UI_SCALES.every((s) => scaled(40, s) < scaled(56, s)));
  }

  console.log(failures.length === 0
    ? '\nall checks passed'
    : `\n${failures.length} failed: ${failures.join(', ')}`);
  return failures.length === 0 ? 0 : 1;
}
