// Which way the map is facing, and a way back to north.
//
// Run:  node validation_tests/js/run_map_compass.mjs
//
// The console opens at a bearing of 22 degrees so the Bay fits a landscape
// pane, and nothing on the map said so. Every direction read off a rotated
// map is wrong by that much: the measure tool's own harness put a reference
// leg 0.8 km out over 216 km until the camera was pinned north up.
//
// The needle turns against the map, so the two errors worth guarding are a
// needle that follows the rotation instead of opposing it, and one that
// stops at a wrap.

export function runTests(mod) {
  const {
    normalizeBearing, isNorthUp, needleRotation, bearingLabel,
    resetCamera, NORTH_TOLERANCE_DEG
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  console.log('any heading folds into one turn:');
  {
    check('a plain heading is itself', normalizeBearing(22) === 22);
    check('a full turn is north', normalizeBearing(360) === 0);
    check('past a turn wraps', normalizeBearing(385) === 25);
    check('a negative comes back positive', normalizeBearing(-22) === 338);
    check('many turns back wrap once', normalizeBearing(-725) === 355);
    check('NaN is north rather than NaN', normalizeBearing(NaN) === 0);
    check('infinity is north', normalizeBearing(Infinity) === 0);
  }

  console.log('\nthe needle turns against the map:');
  {
    check('north leaves it alone', needleRotation(0) === 0);
    check('a map turned 22 turns the needle back 22',
      needleRotation(22) === 338, String(needleRotation(22)));
    check('a map turned 90 turns the needle to 270',
      needleRotation(90) === 270);
    check('it never leaves one turn',
      [0, 1, 22, 180, 359, 400, -400].every((d) => {
        const r = needleRotation(d);
        return r >= 0 && r < 360;
      }));
    // A needle that followed the map would leave north pointing at the
    // top of the screen whatever the map did, which is the bug this
    // control exists to prevent.
    check('it opposes rather than follows', needleRotation(22) !== 22);
  }

  console.log('\nnorth up is a measured claim:');
  {
    check('exactly north is north up', isNorthUp(0));
    check('just inside the tolerance is north up',
      isNorthUp(NORTH_TOLERANCE_DEG - 0.01));
    check('just outside it is not', !isNorthUp(NORTH_TOLERANCE_DEG + 0.01));
    check('the console default is not north up', !isNorthUp(22));
    check('a hair west of north is north up', isNorthUp(359.9));
    check('a degree west of north is not', !isNorthUp(359));
  }

  console.log('\nthe label says where the map faces:');
  {
    check('north up says so', bearingLabel(0) === 'north up');
    // The sixteen-point rose the measure tool already uses. At the
    // console's own 22 degrees an eight-point rose says "N", which
    // reads as north on a map that is 22 degrees off it.
    check('the default reads as a heading and a point',
      bearingLabel(22) === '022° NNE', bearingLabel(22));
    check('the degrees are padded to three',
      bearingLabel(5).startsWith('005°'), bearingLabel(5));
    check('a full turn reads as north up', bearingLabel(360) === 'north up');
    check('due east is east', bearingLabel(90) === '090° E');
    check('due west is west', bearingLabel(270) === '270° W');
  }

  console.log('\nasking for north returns the camera, or nothing to do:');
  {
    check('a rotated map comes back to north',
      resetCamera(22, 0)?.bearing === 0);
    check('a pitched map comes back flat too',
      resetCamera(0, 50)?.pitch === 0);
    check('a map already north and flat has nothing to do',
      resetCamera(0, 0) === null);
    check('a hair off counts as nothing to do',
      resetCamera(0.2, 0.2) === null);
    check('a rotated and pitched map resets both',
      JSON.stringify(resetCamera(22, 50)) === '{"bearing":0,"pitch":0}',
      JSON.stringify(resetCamera(22, 50)));
    check('a NaN pitch does not stop the reset',
      resetCamera(22, NaN)?.bearing === 0);
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall compass assertions passed');
  return failures.length ? 1 : 0;
}
