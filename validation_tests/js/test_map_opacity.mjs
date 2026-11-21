// One opacity per raster layer, and what happens to the old single value.
//
// Run:  node validation_tests/js/run_map_opacity.mjs
//
// Three things here are worth asserting rather than reading.
//
// The defaults must be the values that shipped. Turning three constants into
// three settings is supposed to change nothing until someone moves a slider,
// and a default that drifts would repaint every console on upgrade.
//
// A stored value must never reach MapLibre unchecked. `raster-opacity` of NaN
// paints nothing, which reads as a layer that failed to load rather than as a
// bad preference, and the value arrives from a blob an older build wrote.
//
// The old single slider must survive the upgrade. It moved the radar overlays
// and nothing else, so it applies to that layer alone, and only where the new
// per-layer value is absent, or one upgrade would keep overwriting a setting
// made after it.

const SHIPPED = { composite: 0.7, radars: 0.8, nexrad: 0.6 };

const JUNK = [NaN, Infinity, -Infinity, null, 'wide', '', {}, [], true,
  undefined];

export function runTests(mod) {
  const {
    OPACITY_LAYERS, DEFAULT_OPACITIES, OPACITY_LABELS, normalizeOpacity,
    normalizeOpacities, opacityPercent, opacityFromPercent, isHidden
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  console.log('the defaults are what shipped before the sliders existed:');
  for (const [layer, want] of Object.entries(SHIPPED)) {
    check(`${layer} opens at ${want}`, DEFAULT_OPACITIES[layer] === want,
      String(DEFAULT_OPACITIES[layer]));
  }
  check('there are exactly three layers', OPACITY_LAYERS.length === 3,
    OPACITY_LAYERS.join(' '));
  check('every layer has a default',
    OPACITY_LAYERS.every((l) => typeof DEFAULT_OPACITIES[l] === 'number'));
  check('every layer has a label',
    OPACITY_LAYERS.every((l) => (OPACITY_LABELS[l] ?? '').length > 0),
    OPACITY_LAYERS.map((l) => OPACITY_LABELS[l]).join(' '));
  check('no default is out of range',
    OPACITY_LAYERS.every((l) => DEFAULT_OPACITIES[l] >= 0
      && DEFAULT_OPACITIES[l] <= 1));

  console.log('\nnothing unusable reaches the paint property:');
  for (const bad of JUNK) {
    const got = normalizeOpacity(bad, 0.5);
    check(`${String(bad)} falls back`, got === 0.5, String(got));
  }
  check('above one clamps down', normalizeOpacity(4, 0.5) === 1);
  check('below zero clamps up', normalizeOpacity(-4, 0.5) === 0);
  check('zero is a real setting, not a missing one',
    normalizeOpacity(0, 0.5) === 0);
  check('a numeric string is honored, since a slider reports strings',
    normalizeOpacity('0.25', 0.5) === 0.25);

  console.log('\na whole stored set is repaired layer by layer:');
  {
    const got = normalizeOpacities({ composite: 0.2, radars: 'junk' });
    check('a good value is kept', got.composite === 0.2);
    check('a bad one falls back to that layer, not to another',
      got.radars === SHIPPED.radars, String(got.radars));
    check('a missing one takes its default',
      got.nexrad === SHIPPED.nexrad, String(got.nexrad));
    check('nothing else is added',
      Object.keys(got).length === OPACITY_LAYERS.length,
      Object.keys(got).join(' '));
  }
  for (const bad of [null, undefined, 7, 'none', []]) {
    const got = normalizeOpacities(bad);
    check(`a stored ${String(bad)} gives the full defaults`,
      OPACITY_LAYERS.every((l) => got[l] === DEFAULT_OPACITIES[l]),
      JSON.stringify(got));
  }
  check('the result is a fresh object, not the shared defaults',
    normalizeOpacities({}) !== DEFAULT_OPACITIES);
  {
    const got = normalizeOpacities({});
    got.radars = 0.1;
    check('...and writing to it does not move the defaults',
      DEFAULT_OPACITIES.radars === SHIPPED.radars);
  }

  console.log('\nthe old single slider upgrades onto the radars alone:');
  {
    const got = normalizeOpacities({}, 0.3);
    check('it becomes the radar opacity', got.radars === 0.3);
    check('it does not touch the composite',
      got.composite === SHIPPED.composite);
    check('it does not touch NEXRAD', got.nexrad === SHIPPED.nexrad);
  }
  check('a per-layer value set since the upgrade wins over it',
    normalizeOpacities({ radars: 0.9 }, 0.3).radars === 0.9);
  check('a zero set since the upgrade is not mistaken for absent',
    normalizeOpacities({ radars: 0 }, 0.3).radars === 0);
  check('a junk legacy value does not poison the layer',
    normalizeOpacities({}, 'half').radars === SHIPPED.radars);
  check('no legacy value leaves the default',
    normalizeOpacities({}).radars === SHIPPED.radars);

  console.log('\nthe slider and its readout agree:');
  check('0.7 reads as 70', opacityPercent(0.7) === 70);
  check('0 reads as 0', opacityPercent(0) === 0);
  check('1 reads as 100', opacityPercent(1) === 100);
  check('a junk value reads as 0 rather than NaN%',
    opacityPercent(NaN) === 0, String(opacityPercent(NaN)));
  check('70 comes back as 0.7', opacityFromPercent(70, 0.5) === 0.7);
  check('a slider string comes back', opacityFromPercent('35', 0.5) === 0.35);
  check('a junk percent keeps what was there',
    opacityFromPercent('abc', 0.42) === 0.42);
  // Number('') is 0, and 0 is a legal opacity, so without an explicit guard
  // a blank value hides the layer instead of being rejected.
  check('a blank percent keeps what was there rather than hiding the layer',
    opacityFromPercent('', 0.42) === 0.42,
    String(opacityFromPercent('', 0.42)));
  check('a whitespace percent too',
    opacityFromPercent('   ', 0.42) === 0.42);
  check('a blank opacity keeps what was there',
    normalizeOpacity('  ', 0.42) === 0.42);
  check('a percent over 100 clamps', opacityFromPercent(400, 0.5) === 1);
  check('a negative percent clamps', opacityFromPercent(-5, 0.5) === 0);
  for (const pct of [0, 1, 37, 70, 99, 100]) {
    check(`${pct}% survives the round trip`,
      opacityPercent(opacityFromPercent(pct, 0.5)) === pct);
  }

  console.log('\na layer at zero is reported as hidden:');
  check('zero is hidden', isHidden(0) === true);
  check('a rounding-down value is hidden too, since nothing shows',
    isHidden(0.002) === true, String(opacityPercent(0.002)));
  check('one percent is not hidden', isHidden(0.01) === false);
  check('the default is not hidden', isHidden(DEFAULT_OPACITIES.radars) === false);

  if (failures.length) {
    console.log(`\n${failures.length} failed:`);
    for (const f of failures) console.log(`  ${f}`);
    return 1;
  }
  console.log('\nall opacity assertions passed');
  return 0;
}
