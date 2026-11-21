// Points the reader has pinned to watch a value at.
//
// Run:  node validation_tests/js/run_map_probe.mjs
//
// A pin is shared by every pane and removed by clicking the one that was
// clicked, so the failures that matter are an id that collides with a
// pin already on the map, a removal that takes the wrong pin, a label
// that stops matching the pin it is drawn on, and a list that grows
// without bound until the markers cover the imagery they report on.

export function runTests(mod) {
  const { addPin, removePin, pinLabel, nextPinId, PIN_LIMIT } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  const fill = (n) => {
    let pins = [];
    for (let i = 0; i < n; i += 1) pins = addPin(pins, -122 + i * 0.01, 38, `p${i}`);
    return pins;
  };

  console.log('a pin is a ground point on this globe:');
  {
    const one = addPin([], -122.4, 37.8, 'a');
    check('a point is kept', one.length === 1);
    check('its ground is kept exactly',
      one[0].lng === -122.4 && one[0].lat === 37.8);
    check('its id is kept', one[0].id === 'a');
    check('a NaN longitude is refused', addPin([], NaN, 38, 'a').length === 0);
    check('a NaN latitude is refused', addPin([], -122, NaN, 'a').length === 0);
    check('an infinite point is refused',
      addPin([], Infinity, 38, 'a').length === 0);
    check('a latitude past the pole is refused',
      addPin([], -122, 91, 'a').length === 0);
    check('a longitude past the meridian is refused',
      addPin([], 181, 38, 'a').length === 0);
    check('the exact pole is a place', addPin([], 0, 90, 'a').length === 1);
    check('an empty id is refused', addPin([], -122, 38, '').length === 0);
    // Two pins with one id make a click ambiguous, and the removal
    // would take whichever the filter reached first.
    check('a duplicate id is refused',
      addPin(addPin([], -122, 38, 'a'), -121, 39, 'a').length === 1);
  }

  console.log('\nadding never mutates the list it was given:');
  {
    const before = addPin([], -122, 38, 'a');
    const after = addPin(before, -121, 39, 'b');
    check('the original is untouched', before.length === 1);
    check('the new list has both', after.length === 2);
    check('a refused point still returns a new list',
      addPin(before, NaN, 38, 'c') !== before);
    check('order is the order they were pinned',
      after[0].id === 'a' && after[1].id === 'b');
  }

  console.log('\nthe map holds a bounded number:');
  {
    check('the limit is small enough to draw in three panes',
      PIN_LIMIT > 0 && PIN_LIMIT <= 20, String(PIN_LIMIT));
    const full = fill(PIN_LIMIT);
    check('it fills to the limit', full.length === PIN_LIMIT);
    const over = addPin(full, -120, 40, 'extra');
    check('one more does not grow it', over.length === PIN_LIMIT);
    check('the oldest went', !over.some((p) => p.id === 'p0'));
    check('the newest is there',
      over[over.length - 1].id === 'extra');
    check('the ones between kept their order',
      over.slice(0, -1).every((p, i) => p.id === `p${i + 1}`));
    // A click at the limit has to do something, or the tool reads as
    // broken at exactly the moment a reader is working hardest.
    check('pinning at the limit is never a no-op',
      over[over.length - 1].id !== full[full.length - 1].id);
  }

  console.log('\nremoval takes the pin that was clicked:');
  {
    const three = fill(3);
    const gone = removePin(three, 'p1');
    check('it is shorter by one', gone.length === 2);
    check('it took the named pin', !gone.some((p) => p.id === 'p1'));
    check('it left the others',
      gone[0].id === 'p0' && gone[1].id === 'p2');
    check('the original is untouched', three.length === 3);
    check('an unknown id changes nothing',
      removePin(three, 'nope').length === 3);
    check('removing from empty is empty', removePin([], 'p0').length === 0);
  }

  console.log('\nlabels number what is on the map now:');
  {
    check('the first pin is 1', pinLabel(0) === '1');
    check('the third is 3', pinLabel(2) === '3');
    check('a negative index does not read as 0 or -1', pinLabel(-5) === '1');
    check('a fractional index truncates', pinLabel(2.9) === '3');
    // Labels come from position, so a pin removed from the middle
    // renumbers the rest. That is the point: the reader counts what is
    // drawn, and the id is what the code removes by.
    const left = removePin(fill(3), 'p0');
    check('after a removal the labels still run from 1',
      left.map((_, i) => pinLabel(i)).join(',') === '1,2');
  }

  console.log('\nids are unique enough to remove by:');
  {
    const ids = new Set();
    for (let i = 0; i < 500; i += 1) ids.add(nextPinId());
    check('500 in a row are all distinct', ids.size === 500);
    check('each is a non-empty string',
      [...ids].every((s) => typeof s === 'string' && s.length > 0));
    // Two pins dropped in the same millisecond is an ordinary
    // double-click, and a clock-only id would collide on it.
    check('a timestamp alone is not the id',
      new Set([nextPinId(), nextPinId()]).size === 2);
  }

  console.log(failures.length === 0
    ? '\nall probe assertions passed'
    : `\n${failures.length} failed: ${failures.join(', ')}`);
  return failures.length === 0 ? 0 : 1;
}
