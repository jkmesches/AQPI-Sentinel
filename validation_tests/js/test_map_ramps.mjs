// Reading an overlay pixel back as a value.
//
// Run:  node validation_tests/js/run_map_ramps.mjs
//
// A readout that is wrong reads exactly like one that is right. A number
// beside a cursor carries more authority than the picture under it, and
// nobody checks a number against a color by eye.
//
// The two failures this guards are both silent. A tolerance wider than the
// gap between two stops matches a color to a stop it does not belong to and
// prints a confident wrong number. A tolerance that admits anything turns
// the map background and the basemap labels into readings.
//
// The ramps themselves are measured separately, against real frames, by
// validation_tests/visual/ramp_coverage.py.

export function runTests(mod, ramps) {
  const {
    invert, parseHex, colorDistance, selfCoverage,
    MATCH_TOLERANCE, TRANSPARENT_ALPHA
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const refl = ramps['flow-reflectivity'];
  const vel = ramps['flow-velocity'];
  const all = Object.entries(ramps);

  console.log('hex parses, or says why not:');
  {
    check('lowercase', parseHex('#ccefff').join(',') === '204,239,255',
      parseHex('#ccefff').join(','));
    check('uppercase', parseHex('#CCEFFF').join(',') === '204,239,255');
    check('without the hash', parseHex('ccefff').join(',') === '204,239,255');
    check('black', parseHex('#000000').join(',') === '0,0,0');
    check('white', parseHex('#ffffff').join(',') === '255,255,255');
    for (const bad of ['#ccc', '#ccefffff', 'not a color', '#gggggg', '']) {
      let threw = false;
      try { parseHex(bad); } catch (e) { threw = e instanceof RangeError; }
      check(`${JSON.stringify(bad)} is refused`, threw);
    }
  }

  console.log('\ndistance is summed over the channels:');
  {
    check('a color is zero from itself', colorDistance([1, 2, 3], [1, 2, 3]) === 0);
    check('black to white is the maximum',
      colorDistance([0, 0, 0], [255, 255, 255]) === 765);
    check('one channel counts once', colorDistance([10, 0, 0], [0, 0, 0]) === 10);
    check('it is symmetric',
      colorDistance([9, 20, 3], [1, 2, 30]) === colorDistance([1, 2, 30], [9, 20, 3]));
    check('every channel counts',
      colorDistance([1, 1, 1], [0, 0, 0]) === 3);
  }

  console.log('\nevery stop reads back as itself:');
  {
    for (const [name, ramp] of all) {
      const c = selfCoverage(ramp);
      check(`${name}: ${c.exact}/${c.total}`, c.exact === c.total,
        `${c.exact}/${c.total}`);
    }
  }

  console.log('\nthe tolerance is under the closest pair in every ramp:');
  {
    // The regression this file exists for. A tolerance at or above the
    // closest stop separation can match a color to the wrong stop, and the
    // readout prints a confident wrong number.
    for (const [name, ramp] of all) {
      let min = Infinity;
      for (let i = 0; i < ramp.stops.length; i++) {
        for (let j = i + 1; j < ramp.stops.length; j++) {
          const d = colorDistance(parseHex(ramp.stops[i].color),
            parseHex(ramp.stops[j].color));
          if (d < min) min = d;
        }
      }
      check(`${name}: closest pair ${min} > tolerance ${MATCH_TOLERANCE}`,
        min > MATCH_TOLERANCE, `${min} vs ${MATCH_TOLERANCE}`);
    }
  }

  console.log('\nan exact color reads its own value:');
  {
    const stop = refl.stops[5];
    const r = invert(refl, parseHex(stop.color));
    check('the value comes back', r.value === stop.value, String(r?.value));
    check('with its band', r.lo === stop.lo && r.hi === stop.hi);
    check('at distance zero', r.distance === 0, String(r.distance));
    check('the band is below the value', r.lo <= r.value && r.value <= r.hi);
  }

  console.log('\na color just off a stop still reads it:');
  {
    const stop = refl.stops[8];
    const [rr, gg, bb] = parseHex(stop.color);
    const near = [rr + 3, gg - 3, bb + 2];
    const r = invert(refl, near);
    check('it matches', r !== null && r.value === stop.value, JSON.stringify(r));
    check('and reports how far', r.distance === 8, String(r?.distance));
  }

  console.log('\na color off the ramp reads nothing:');
  {
    // The map background, a basemap label showing through, and the edge of
    // the radar's coverage all land here. A guess would put a confident dBZ
    // on a road name.
    check('mid grey is not on the reflectivity ramp',
      invert(refl, [128, 128, 128]) === null,
      JSON.stringify(invert(refl, [128, 128, 128])));
    check('a landform brown is not', invert(refl, [120, 80, 40]) === null,
      JSON.stringify(invert(refl, [120, 80, 40])));
    const far = invert(refl, [1, 2, 3]);
    check('near-black is not', far === null, JSON.stringify(far));
    // Pure red is not a safe negative. Reflectivity paints 53.2 dBZ as
    // #fd0000, two channels off it, so a test that expected red to be
    // off-ramp would be asserting the ramp is wrong.
    const red = invert(refl, [255, 0, 0]);
    check('pure red is 53.2 dBZ, not off the ramp',
      red !== null && Math.abs(red.value - 53.2) < 0.1, JSON.stringify(red));
  }

  console.log('\na transparent pixel is outside coverage:');
  {
    const stop = refl.stops[3];
    const rgb = parseHex(stop.color);
    check('alpha zero reads nothing', invert(refl, rgb, 0) === null);
    check('at the floor it still reads nothing',
      invert(refl, rgb, TRANSPARENT_ALPHA) === null);
    check('just above the floor it reads',
      invert(refl, rgb, TRANSPARENT_ALPHA + 1) !== null);
    check('opaque reads', invert(refl, rgb, 255) !== null);
    check('the default is opaque', invert(refl, rgb) !== null);
  }

  console.log('\nthe ramps carry their provenance:');
  {
    for (const [name, ramp] of all) {
      check(`${name} names its source`,
        typeof ramp.source === 'string' && ramp.source.includes('colorbar'),
        ramp.source);
      check(`${name} carries the date it was read`,
        /^\d{4}-\d{2}-\d{2}$/.test(ramp.read_on), ramp.read_on);
      check(`${name} carries a unit and a label`,
        !!ramp.unit && !!ramp.label, `${ramp.unit} / ${ramp.label}`);
    }
  }

  console.log('\nthe stops cover the bar from end to end:');
  {
    for (const [name, ramp] of all) {
      const first = ramp.stops[0];
      const last = ramp.stops[ramp.stops.length - 1];
      check(`${name} starts at its minimum`,
        Math.abs(first.lo - ramp.min) < 0.5, `${first.lo} vs ${ramp.min}`);
      check(`${name} ends at its maximum`,
        Math.abs(last.hi - ramp.max) < 0.5, `${last.hi} vs ${ramp.max}`);
      const ascending = ramp.stops.every((s, i) =>
        i === 0 || s.value > ramp.stops[i - 1].value);
      check(`${name} ascends`, ascending);
      const contiguous = ramp.stops.every((s, i) =>
        i === 0 || Math.abs(s.lo - ramp.stops[i - 1].hi) < ramp.stops.length * 0.1);
      check(`${name} leaves no gap between stops`, contiguous);
    }
  }

  console.log('\nreflectivity is banded and velocity is smooth:');
  {
    // A discrete ramp and a continuous one read back differently, and the
    // legend draws them differently too.
    const banded = refl.stops.every((s) => s.hi - s.lo > 1);
    check('reflectivity bands are wider than a unit', banded);
    check('...about five dBZ each',
      Math.abs((refl.stops[5].hi - refl.stops[5].lo) - 4.4) < 1,
      (refl.stops[5].hi - refl.stops[5].lo).toFixed(2));
    check('velocity resolves near a metre per second',
      (vel.stops[5].hi - vel.stops[5].lo) < 1.5,
      (vel.stops[5].hi - vel.stops[5].lo).toFixed(2));
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall ramp assertions passed');
  return failures.length ? 1 : 0;
}
