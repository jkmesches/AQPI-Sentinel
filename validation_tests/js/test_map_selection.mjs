// Radar selection and the camera box it implies.
//
// Run:  node validation_tests/js/run_map_selection.mjs
//
// The console frames the camera on whatever a preset selects, and both
// faults available here render as a working map. A duplicate id puts two
// image sources on one radar, where the second overwrites the first and the
// overlay stops responding to the one the operator thinks is on top. An
// empty selection folded over Infinity hands fitBounds an inverted box and
// sends the camera to the far side of the world.

export function runTests(mod) {
  const { uniqueIds, toggleId, selectionBounds } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  // AQPI's fleet as /api/radars/meta reports it, plus FLOW for a one-radar
  // deployment at a different latitude.
  const RADARS = [
    { id: 'XSCV', lat: 37.1831, lon: -121.7063, range_m: 40000, kind: 'xband' },
    { id: 'XEBY', lat: 37.8156, lon: -122.062, range_m: 40000, kind: 'xband' },
    { id: 'CBAND', lat: 38.0119, lon: -122.6778, range_m: 100000, kind: 'cband' },
    { id: 'KDAX', lat: 38.501, lon: -121.677, range_m: 100000, kind: 'nexrad' },
    { id: 'FLOW', lat: 34.2048, lon: -118.17081, range_m: 40346, kind: 'xband' }
  ];
  const byId = Object.fromEntries(RADARS.map((r) => [r.id, r]));

  console.log('duplicates never reach the overlay stack:');
  {
    check('a repeat is dropped',
      uniqueIds(['XEBY', 'XEBY']).join(',') === 'XEBY', uniqueIds(['XEBY', 'XEBY']).join(','));
    check('first-seen order is kept',
      uniqueIds(['CBAND', 'XEBY', 'CBAND', 'XSCV']).join(',') === 'CBAND,XEBY,XSCV',
      uniqueIds(['CBAND', 'XEBY', 'CBAND', 'XSCV']).join(','));
    check('an empty list stays empty', uniqueIds([]).length === 0);
    check('a clean list is unchanged',
      uniqueIds(['A', 'B', 'C']).join(',') === 'A,B,C');
    // The caller reassigns reactive state with this, and a returned input
    // would not trip Svelte's reference check.
    const input = ['XEBY'];
    check('the result is a fresh array', uniqueIds(input) !== input);
  }

  console.log('\ntoggling adds to the end and removes in place:');
  {
    check('absent is added', toggleId(['XEBY'], 'XSCV').join(',') === 'XEBY,XSCV');
    check('present is removed', toggleId(['XEBY', 'XSCV'], 'XEBY').join(',') === 'XSCV');
    check('adding to nothing', toggleId([], 'FLOW').join(',') === 'FLOW');
    check('removing the only one leaves nothing', toggleId(['FLOW'], 'FLOW').length === 0);
    check('an id not in the list is added, not ignored',
      toggleId(['XEBY'], 'KDAX').join(',') === 'XEBY,KDAX');
    // Off then on again puts it last, so a reader who just clicked it sees
    // it on top of the stack.
    check('off then on moves it to the end',
      toggleId(toggleId(['A', 'B', 'C'], 'A'), 'A').join(',') === 'B,C,A');
    const input = ['XEBY'];
    check('the result is a fresh array', toggleId(input, 'XSCV') !== input);
    check('...and the input is not mutated', input.join(',') === 'XEBY');
  }

  console.log('\nan empty selection leaves the camera alone:');
  {
    // The regression this file exists for. A fold seeded with Infinity
    // returns an inverted box, and fitBounds obeys it.
    check('no ids gives null', selectionBounds(RADARS, []) === null);
    check('no radars gives null', selectionBounds([], ['XEBY']) === null);
    check('ids that match nothing give null',
      selectionBounds(RADARS, ['NOPE', 'ALSONOPE']) === null);
    check('...even mixed with an empty radar list',
      selectionBounds([], []) === null);
  }

  console.log('\nthe box contains the rings, not just the sites:');
  {
    const one = selectionBounds(RADARS, ['XEBY']);
    const r = byId.XEBY;
    check('a single radar gives a box around it',
      one.west < r.lon && one.east > r.lon && one.south < r.lat && one.north > r.lat);
    // 40 km of range is about 0.36 degrees of latitude. A box around the
    // point alone would be zero-sized and fitBounds would zoom to the limit.
    check('...spanning its range, not zero',
      Math.abs((one.north - one.south) - 2 * (40 / 111)) < 0.01,
      (one.north - one.south).toFixed(4));
    check('...wider in longitude than in latitude, at this latitude',
      (one.east - one.west) > (one.north - one.south));
  }

  console.log('\nseveral radars give their union:');
  {
    const two = selectionBounds(RADARS, ['XEBY', 'XSCV']);
    const a = selectionBounds(RADARS, ['XEBY']);
    const b = selectionBounds(RADARS, ['XSCV']);
    check('west is the smaller', Math.abs(two.west - Math.min(a.west, b.west)) < 1e-9);
    check('east is the larger', Math.abs(two.east - Math.max(a.east, b.east)) < 1e-9);
    check('south is the smaller', Math.abs(two.south - Math.min(a.south, b.south)) < 1e-9);
    check('north is the larger', Math.abs(two.north - Math.max(a.north, b.north)) < 1e-9);
    check('the union contains both', two.west <= a.west && two.east >= b.east);
    check('order does not change the box',
      JSON.stringify(selectionBounds(RADARS, ['XSCV', 'XEBY'])) === JSON.stringify(two));
    check('a repeated id does not change the box',
      JSON.stringify(selectionBounds(RADARS, ['XEBY', 'XSCV', 'XEBY'])) === JSON.stringify(two));
  }

  console.log('\nrange is respected, not just position:');
  {
    // CBAND reaches 100 km against XEBY's 40, and sits north-west of it.
    const wide = selectionBounds(RADARS, ['CBAND']);
    const narrow = selectionBounds(RADARS, ['XEBY']);
    check('a 100 km radar gives a wider box',
      (wide.north - wide.south) > (narrow.north - narrow.south) * 2,
      `${(wide.north - wide.south).toFixed(3)} vs ${(narrow.north - narrow.south).toFixed(3)}`);
    const pair = selectionBounds(RADARS, ['CBAND', 'XEBY']);
    check('the wider radar sets the west edge', Math.abs(pair.west - wide.west) < 1e-9);
  }

  console.log('\nradars far apart still give one box:');
  {
    // FLOW is 400 km south of the Bay fleet. A profile never mixes them,
    // but a union that silently dropped one would be the quiet failure.
    const far = selectionBounds(RADARS, ['KDAX', 'FLOW']);
    check('the box spans both', far.north > 39 && far.south < 34,
      `${far.south.toFixed(2)}..${far.north.toFixed(2)}`);
    check('...and is a valid box', far.west < far.east && far.south < far.north);
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall selection assertions passed');
  return failures.length ? 1 : 0;
}
