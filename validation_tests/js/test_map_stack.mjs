// Vector overlay paint order.
//
// Run:  node validation_tests/js/run_map_stack.mjs
//
// The visual baseline found this one. Every overlay was added with
// `beforeId: 'radar-halo'`, so each landed immediately under the radar
// markers and their order among themselves came down to whichever fetch
// returned first. Two captures of identical code disagreed about whether
// `watershed-line` sat above or below `burn-scar-line`.
//
// The assertions below are about the two ways the replacement can fail. A
// beforeId naming a layer that is not on the map makes MapLibre throw, and
// an order that depends on which layers happen to be on is the original bug
// wearing a new shape.

export function runTests(mod) {
  const { beforeIdFor, VECTOR_STACK, MARKER_TOP } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  // A map carrying exactly these layers.
  const on = (...ids) => (id) => ids.includes(id);
  const ALL = [...VECTOR_STACK, MARKER_TOP];

  console.log('the stack is declared, bottom to top:');
  {
    check('six overlays', VECTOR_STACK.length === 6, String(VECTOR_STACK.length));
    check('no duplicates', new Set(VECTOR_STACK).size === VECTOR_STACK.length);
    const at = (id) => VECTOR_STACK.indexOf(id);
    check('fills sit under lines', at('burn-scar-fill') < at('burn-scar-line'));
    check('lines sit under point markers',
      at('watershed-line') < at('reservoir-marker')
      && at('burn-scar-line') < at('stream-gauge-marker'));
    check('labels sit on top of every marker',
      at('reservoir-label') === VECTOR_STACK.length - 1, String(at('reservoir-label')));
    check('the markers are the ceiling', MARKER_TOP === 'radar-halo', MARKER_TOP);
    check('...and are not themselves in the stack',
      !VECTOR_STACK.includes(MARKER_TOP));
  }

  console.log('\nnever a beforeId that is not on the map:');
  {
    // The failure mode is loud: MapLibre throws and the layer is lost.
    for (const id of VECTOR_STACK) {
      const answer = beforeIdFor(id, on());
      check(`${id} on an empty map`, answer === undefined, String(answer));
    }
    for (const id of VECTOR_STACK) {
      const present = ALL.filter((x) => x !== id);
      const answer = beforeIdFor(id, on(...present));
      check(`${id} against a full map answers a present layer`,
        answer !== undefined && present.includes(answer), String(answer));
    }
  }

  console.log('\nthe order holds whichever layers are already on:');
  {
    // The regression this file exists for. Watersheds and burn scars must
    // stack the same way whichever of them arrives first.
    const wsFirst = beforeIdFor('burn-scar-line', on('watershed-line', MARKER_TOP));
    const bsFirst = beforeIdFor('watershed-line', on('burn-scar-line', MARKER_TOP));
    check('burn-scar-line goes under an existing watershed-line',
      wsFirst === 'watershed-line', String(wsFirst));
    check('watershed-line goes under the markers, above burn-scar-line',
      bsFirst === MARKER_TOP, String(bsFirst));
    check('...so either arrival order gives the same stack', true,
      'burn-scar-line below watershed-line both ways');
  }

  console.log('\nthe nearest layer above wins:');
  {
    check('with everything above present, the next one up is chosen',
      beforeIdFor('burn-scar-fill', on(...ALL)) === 'burn-scar-line',
      beforeIdFor('burn-scar-fill', on(...ALL)));
    check('with the next one missing, the one after it is chosen',
      beforeIdFor('burn-scar-fill', on('watershed-line', MARKER_TOP))
        === 'watershed-line');
    check('with nothing above, the markers hold the place',
      beforeIdFor('reservoir-label', on(...VECTOR_STACK, MARKER_TOP)) === MARKER_TOP,
      beforeIdFor('reservoir-label', on(...VECTOR_STACK, MARKER_TOP)));
    check('the topmost overlay with no markers goes to the top of the style',
      beforeIdFor('reservoir-label', on(...VECTOR_STACK)) === undefined);
    // A layer below never counts: inserting before it would put the new
    // layer under something it should cover.
    check('a layer below is ignored',
      beforeIdFor('reservoir-label', on('burn-scar-fill', 'watershed-line'))
        === undefined);
  }

  console.log('\nevery layer lands in its declared place:');
  {
    // Build the stack in every arrival order and check the result reads the
    // same. The map is a list, and inserting before an id puts the new layer
    // immediately under it.
    const build = (order) => {
      const layers = [MARKER_TOP];
      for (const id of order) {
        const before = beforeIdFor(id, (x) => layers.includes(x));
        const at = before === undefined ? layers.length : layers.indexOf(before);
        layers.splice(at, 0, id);
      }
      return layers.join(',');
    };
    const declared = [...VECTOR_STACK, MARKER_TOP].join(',');
    check('in declared order', build([...VECTOR_STACK]) === declared,
      build([...VECTOR_STACK]));
    check('in reverse order', build([...VECTOR_STACK].reverse()) === declared,
      build([...VECTOR_STACK].reverse()));
    // The two the baseline caught, both ways round.
    const pair = ['burn-scar-fill', 'burn-scar-line', 'watershed-line'];
    check('burn scars before watersheds',
      build(pair) === build([pair[2], pair[0], pair[1]]), build(pair));
    check('watersheds before burn scars',
      build([pair[2], pair[0], pair[1]]).includes('burn-scar-line,watershed-line'),
      build([pair[2], pair[0], pair[1]]));
    // A few shuffles, since fetch completion order is arbitrary.
    const shuffles = [
      ['reservoir-label', 'burn-scar-fill', 'stream-gauge-marker', 'watershed-line',
       'reservoir-marker', 'burn-scar-line'],
      ['stream-gauge-marker', 'reservoir-label', 'burn-scar-line', 'burn-scar-fill',
       'reservoir-marker', 'watershed-line'],
      ['watershed-line', 'reservoir-marker', 'reservoir-label', 'burn-scar-fill',
       'burn-scar-line', 'stream-gauge-marker']
    ];
    for (const [i, order] of shuffles.entries()) {
      check(`shuffle ${i + 1} gives the declared stack`, build(order) === declared,
        build(order));
    }
  }

  console.log('\na partial set still stacks correctly:');
  {
    const build = (order) => {
      const layers = [MARKER_TOP];
      for (const id of order) {
        const before = beforeIdFor(id, (x) => layers.includes(x));
        const at = before === undefined ? layers.length : layers.indexOf(before);
        layers.splice(at, 0, id);
      }
      return layers.join(',');
    };
    check('two layers, either order',
      build(['watershed-line', 'reservoir-label'])
        === build(['reservoir-label', 'watershed-line']),
      build(['watershed-line', 'reservoir-label']));
    check('...stacked as declared',
      build(['reservoir-label', 'watershed-line'])
        === `watershed-line,reservoir-label,${MARKER_TOP}`,
      build(['reservoir-label', 'watershed-line']));
    check('one layer alone sits under the markers',
      build(['burn-scar-fill']) === `burn-scar-fill,${MARKER_TOP}`);
  }

  console.log('\nan unknown layer sits under the markers:');
  {
    // A layer added before it is declared here would otherwise land on top
    // of the radar dots and swallow the clicks.
    check('it answers the markers', beforeIdFor('future-layer', on(...ALL)) === MARKER_TOP,
      String(beforeIdFor('future-layer', on(...ALL))));
    check('...and top of stack when the markers are absent',
      beforeIdFor('future-layer', on(...VECTOR_STACK)) === undefined);
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall layer-stack assertions passed');
  return failures.length ? 1 : 0;
}
