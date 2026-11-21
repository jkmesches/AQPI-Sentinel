// Holding both panes' frame swaps until both are ready.
//
// Run:  node validation_tests/js/run_map_frame_gate.mjs
//
// Each pane already swaps its own layers together. Across two panes there
// was nothing, so the pane that loaded first painted the new minute while
// the other was still fetching, and a comparison showed one product at
// 04:52 beside the other at 04:50. A reader cannot see that the panes are
// a frame apart, which makes it the same fault as letting them drift.
//
// The timeout is what the assertions mostly cover. A pane whose product
// has no frame inside the shared window never arrives, and a gate that
// waited for it would stop the other pane from painting at all. Holding a
// frame forever is a worse failure than showing it a moment early, so the
// release path matters more than the hold.

export function runTests(mod) {
  const {
    newGate, setExpected, arrive, overdue, drain, isWaiting, GATE_WAIT_MS
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  // Thunks that record the order they ran in.
  const fired = [];
  const thunk = (tag) => () => fired.push(tag);
  const run = (applies) => { for (const a of applies) a(); };

  console.log('one pane fires at once, with no one to wait for:');
  {
    fired.length = 0;
    const gate = newGate();
    const out = arrive(gate, 0, [thunk('a1'), thunk('a2')], 0);
    check('it is released immediately', out !== null);
    check('with both of its swaps', out.length === 2);
    check('and nothing is left waiting', isWaiting(gate) === false);
    run(out);
    check('they run in the order given', fired.join() === 'a1,a2',
      fired.join());
  }

  console.log('\ntwo panes wait for each other:');
  {
    fired.length = 0;
    const gate = newGate();
    setExpected(gate, 2);
    const first = arrive(gate, 0, [thunk('a')], 0);
    check('the first pane is held', first === null);
    check('and the gate says so', isWaiting(gate) === true);
    check('nothing has run', fired.length === 0);
    const both = arrive(gate, 1, [thunk('b')], 120);
    check('the second releases both', both !== null && both.length === 2,
      String(both && both.length));
    run(both);
    check('pane 0 before pane 1', fired.join() === 'a,b', fired.join());
    check('and the gate is empty', isWaiting(gate) === false);
  }

  console.log('\nthe later pane arriving out of order still works:');
  {
    fired.length = 0;
    const gate = newGate();
    setExpected(gate, 2);
    check('pane 1 alone is held', arrive(gate, 1, [thunk('b')], 0) === null);
    const both = arrive(gate, 0, [thunk('a')], 50);
    run(both);
    check('they still run in pane order', fired.join() === 'a,b',
      fired.join());
  }

  console.log('\na pane that re-arrives replaces its own frame:');
  {
    fired.length = 0;
    const gate = newGate();
    setExpected(gate, 2);
    arrive(gate, 0, [thunk('old')], 0);
    arrive(gate, 0, [thunk('new')], 40);
    check('it is still waiting for the other', isWaiting(gate) === true);
    const both = arrive(gate, 1, [thunk('b')], 80);
    check('only two swaps come out', both.length === 2,
      String(both.length));
    run(both);
    check('the stale one does not run', !fired.includes('old'),
      fired.join());
    check('the newest one does', fired.join() === 'new,b', fired.join());
  }

  console.log('\na pane that never arrives cannot hold the other:');
  {
    const gate = newGate();
    setExpected(gate, 2);
    arrive(gate, 0, [thunk('a')], 1000);
    check('not overdue immediately', overdue(gate, 1000) === false);
    check('not overdue just short of the wait',
      overdue(gate, 1000 + GATE_WAIT_MS - 1) === false);
    check('overdue at the wait',
      overdue(gate, 1000 + GATE_WAIT_MS) === true);
    check('an empty gate is never overdue',
      overdue(newGate(), 1e9) === false);
    const out = drain(gate);
    check('draining gives the one that arrived', out.length === 1);
    check('and empties the gate', isWaiting(gate) === false);
    check('draining an empty gate gives nothing',
      drain(newGate()).length === 0);
  }

  console.log('\nthe wait is measured from the first arrival:');
  {
    const gate = newGate();
    setExpected(gate, 3);
    arrive(gate, 0, [thunk('a')], 100);
    arrive(gate, 1, [thunk('b')], 100 + GATE_WAIT_MS - 10);
    // Measured from the latest arrival this would not be overdue, and a
    // pane re-arriving every frame could hold the others indefinitely.
    check('a later arrival does not restart the clock',
      overdue(gate, 100 + GATE_WAIT_MS) === true);
  }

  console.log('\nclosing a pane releases what was held for it:');
  {
    fired.length = 0;
    const gate = newGate();
    setExpected(gate, 2);
    arrive(gate, 0, [thunk('a')], 0);
    const released = setExpected(gate, 1);
    check('the held frame comes out', released.length === 1);
    run(released);
    check('and it runs', fired.join() === 'a');
    check('the gate is empty', isWaiting(gate) === false);
  }

  console.log('\nthe expected count cannot be made nonsense:');
  {
    for (const bad of [0, -3, NaN, Infinity, 1.6]) {
      const gate = newGate();
      setExpected(gate, bad);
      check(`${bad} leaves at least one expected`, gate.expected >= 1,
        String(gate.expected));
      check(`...and one pane still fires`,
        arrive(gate, 0, [thunk('x')], 0) !== null);
    }
    const gate = newGate();
    setExpected(gate, 2.9);
    check('a fraction truncates rather than rounding up',
      gate.expected === 2, String(gate.expected));
  }

  console.log('\na pane with no swaps still counts as arrived:');
  {
    fired.length = 0;
    const gate = newGate();
    setExpected(gate, 2);
    check('an empty arrival is held like any other',
      arrive(gate, 1, [], 0) === null);
    const both = arrive(gate, 0, [thunk('a')], 10);
    check('and releases the other', both.length === 1, String(both.length));
    run(both);
    check('the one with work runs', fired.join() === 'a');
  }

  if (failures.length) {
    console.log(`\n${failures.length} failed:`);
    for (const f of failures) console.log(`  ${f}`);
    return 1;
  }
  console.log('\nall frame-gate assertions passed');
  return 0;
}
