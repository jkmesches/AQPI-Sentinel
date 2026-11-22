// Putting a published step list into one order.
//
// The strip places frames by time and the scrubber walks them by
// position, so the two agree only while the list is strictly
// increasing. fcst_temp's manifest is not: on 2026-11-22 it carried
// 131 steps holding 75 distinct times, one run out to the 27th and
// then a jump back 4.4 days over the same ground.

/**
 * Run the assertions against the compiled module.
 *
 * @param {object} mod The module under test.
 * @returns {number} 0 when every assertion held, 1 otherwise.
 */
export function runTests(mod) {
  const { inOrder, orderSteps } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const at = (h, name) => ({ i: 0, ts: `2026-11-22T${String(h).padStart(2, '0')}:00:00Z`, name });
  const good = [at(1, 'a'), at(2, 'b'), at(3, 'c')].map((s, i) => ({ ...s, i }));

  console.log('a sound list is left exactly as it came:');
  {
    check('it reads as in order', inOrder(good));
    const out = orderSteps(good);
    check('nothing is dropped', out.dropped === 0, String(out.dropped));
    check('the order is unchanged',
      out.steps.map((s) => s.name).join('') === 'abc',
      out.steps.map((s) => s.name).join(''));
    check('and so is every position',
      out.steps.every((s, i) => s.i === i));
    check('an empty list is sound', inOrder([]) && orderSteps([]).dropped === 0);
    check('and so is a single step', inOrder([at(1, 'a')]));
  }

  console.log('\na list that repeats a time is put right:');
  {
    // The shape fcst_temp publishes: a run, then a jump back, then a
    // second run over the same hours.
    const wild = [at(1, 'old1'), at(2, 'old2'), at(3, 'old3'),
      at(2, 'new2'), at(3, 'new3'), at(4, 'new4')];
    check('it reads as out of order', !inOrder(wild));
    const out = orderSteps(wild);
    check('one frame per time survives', out.steps.length === 4,
      String(out.steps.length));
    check('and the count says what went', out.dropped === 2,
      String(out.dropped));
    check('they come back oldest first',
      out.steps.map((s) => s.ts).join(' ')
      === [1, 2, 3, 4].map((h) => at(h, '').ts).join(' '),
      out.steps.map((s) => s.ts.slice(11, 16)).join(' '));
    // The later entry wins, because a publisher appending to a
    // manifest puts the newer run last. Taking the first would hand a
    // reader the older run's render of that hour.
    check('the later entry wins a repeated time',
      out.steps.map((s) => s.name).join(' ') === 'old1 new2 new3 new4',
      out.steps.map((s) => s.name).join(' '));
    check('and positions are renumbered to match',
      out.steps.every((s, i) => s.i === i),
      out.steps.map((s) => s.i).join(','));
  }

  console.log('\na step with no time stops the sort rather than losing it:');
  {
    // A tilt frame whose name did not parse has no place in a sort,
    // and dropping it would take a drawable frame off the strip.
    const some = [at(1, 'a'), { i: 1, ts: null, name: 'unparsed' }, at(3, 'c')];
    check('such a list reads as sound', inOrder(some));
    const out = orderSteps(some);
    check('it is returned whole', out.steps.length === 3,
      String(out.steps.length));
    check('nothing is dropped', out.dropped === 0, String(out.dropped));
    check('and the unparsed frame is still there',
      out.steps.some((s) => s.ts === null));
  }

  console.log('\nthe order is by instant, not by spelling:');
  {
    // Same moment, two spellings. A string sort would put the offset
    // form after the Z form and invert them.
    const mixed = [
      { i: 0, ts: '2026-11-22T09:00:00Z', name: 'nine' },
      { i: 1, ts: '2026-11-22T03:00:00.000000000', name: 'three' },
      { i: 2, ts: '2026-11-22T09:00:00Z', name: 'nine-again' }
    ];
    const out = orderSteps(mixed);
    check('the earlier instant comes first',
      out.steps[0].name === 'three', out.steps.map((s) => s.name).join(' '));
    check('and the repeat is gone', out.steps.length === 2,
      String(out.steps.length));
  }

  if (failures.length) {
    console.log(`\n${failures.length} FAILED: ${failures.join(', ')}`);
    return 1;
  }
  console.log('\nall step-ordering assertions passed');
  return 0;
}
