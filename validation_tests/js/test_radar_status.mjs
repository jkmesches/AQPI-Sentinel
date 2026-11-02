// One status per radar, collapsed across the stages that observe one.
//
// Both maps render this. The desktop one read `L2` alone and defaulted an
// unmatched radar to `pass`, which painted FLOW permanently green on XQPI —
// where `L2` registers no checks at all, because nothing serves FLOW over
// HTTP. The mobile map had the correct rule inline and the desktop copy was
// never updated, so the bug was invisible from either file on its own.
//
// The load-bearing assertion is the one about absence: a radar nothing
// reported must come back `skip`, never `pass`. Everything else here could
// pass while the original bug was live.

const R = (check_id, target, status) => ({ check_id, target, status });

export function runTests(mod, grouping) {
  const { radarStatusByTarget, radarStatusOf, RADAR_STATUS_STAGES, NO_READING } = mod;
  const { isAlerting } = grouping;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  console.log('a radar nothing reported is never green:');
  {
    // The bug, stated directly. Every one of these is a shape the maps hit.
    check('no stages at all', radarStatusOf(radarStatusByTarget(undefined), 'FLOW') === 'skip');
    check('null stages', radarStatusOf(radarStatusByTarget(null), 'FLOW') === 'skip');
    check('empty stages', radarStatusOf(radarStatusByTarget({}), 'FLOW') === 'skip');
    check('stages present but every radar stage empty',
      radarStatusOf(radarStatusByTarget({ L2: [], LB2: [], LB3: [] }), 'FLOW') === 'skip');
    check('other radars reported, this one not',
      radarStatusOf(radarStatusByTarget({ LB2: [R('layer2.backend.XEBY', 'XEBY', 'pass')] }),
        'FLOW') === 'skip');
    check('NO_READING is what that default is', NO_READING === 'skip', NO_READING);
    // Guard the specific regression: `pass` must not be reachable by default.
    const everyDefault = [undefined, null, {}, { L2: [] }].map(
      (st) => radarStatusOf(radarStatusByTarget(st), 'FLOW'));
    check('no default path yields pass', everyDefault.every((s) => s !== 'pass'),
      everyDefault.join(','));
  }

  console.log('\nthe XQPI shape: LB2 only, no L2 to be had:');
  {
    // layer2_radar is in config.RADARCA_ONLY_MODULES, so L2 is absent here.
    for (const status of ['pass', 'warn', 'fail', 'error', 'skip']) {
      const by = radarStatusByTarget({ LB2: [R('layer2.backend.FLOW', 'FLOW', status)] });
      check(`LB2 ${status} reads through as ${status}`,
        radarStatusOf(by, 'FLOW') === status, radarStatusOf(by, 'FLOW'));
    }
    // The headline: a down radar must not read healthy.
    const down = radarStatusByTarget({ LB2: [R('layer2.backend.FLOW', 'FLOW', 'fail')] });
    check('a failing FLOW is not pass', radarStatusOf(down, 'FLOW') !== 'pass');
  }

  console.log('\nworst wins across stages, in either direction:');
  {
    const by = radarStatusByTarget({
      L2:  [R('layer2.radar.XEBY', 'XEBY', 'pass')],
      LB2: [R('layer2.backend.XEBY', 'XEBY', 'fail')]
    });
    check('radarca pass + disk fail = fail', radarStatusOf(by, 'XEBY') === 'fail',
      radarStatusOf(by, 'XEBY'));
  }
  {
    // Order-independence: the same two readings the other way round.
    const by = radarStatusByTarget({
      L2:  [R('layer2.radar.XEBY', 'XEBY', 'fail')],
      LB2: [R('layer2.backend.XEBY', 'XEBY', 'pass')]
    });
    check('disk pass + radarca fail = fail', radarStatusOf(by, 'XEBY') === 'fail',
      radarStatusOf(by, 'XEBY'));
  }
  {
    const by = radarStatusByTarget({
      L2:  [R('layer2.radar.XSCR', 'XSCR', 'pass')],
      LB2: [R('layer2.backend.XSCR', 'XSCR', 'pass')],
      LB3: [R('layer3.composite.XSCR', 'XSCR', 'warn')]
    });
    check('dropped out of the composite is not fully healthy',
      radarStatusOf(by, 'XSCR') === 'warn', radarStatusOf(by, 'XSCR'));
  }
  {
    // `skip` ranks BELOW pass (see grouping.STATUS_RANK), so one stage
    // declining to judge must not gray out a radar another stage checked.
    const by = radarStatusByTarget({
      L2:  [R('layer2.radar.XSCW', 'XSCW', 'skip')],
      LB2: [R('layer2.backend.XSCW', 'XSCW', 'pass')]
    });
    check('a skipped reading does not outrank a real pass',
      radarStatusOf(by, 'XSCW') === 'pass', radarStatusOf(by, 'XSCW'));
  }
  {
    const by = radarStatusByTarget({
      LB2: [R('layer2.backend.KDAX', 'KDAX', 'warn')],
      LB3: [R('layer3.composite.KDAX', 'KDAX', 'error')]
    });
    check('error outranks warn', radarStatusOf(by, 'KDAX') === 'error',
      radarStatusOf(by, 'KDAX'));
  }

  console.log('\nrows that share these stages but are not radars are dropped:');
  {
    // Both fleet correlation ids live in a radar stage and describe the fleet.
    // Left in, they render as a phantom radar pin.
    const by = radarStatusByTarget({
      L2:  [R('layer2.xband.fleet', 'xband-fleet', 'fail')],
      LB2: [R('layer2.backend.fleet', 'xband-fleet', 'fail')]
    });
    check('no fleet target survives', Object.keys(by).length === 0,
      Object.keys(by).join(','));
  }
  {
    // The DROPS producer is a pipeline stage, not a radar.
    const by = radarStatusByTarget({
      LB3: [R('layer3.backend.drops', 'drops', 'warn')]
    });
    check('the DROPS producer is not a radar', Object.keys(by).length === 0,
      Object.keys(by).join(','));
  }
  {
    // ...and dropping them must not drop the real radars beside them.
    const by = radarStatusByTarget({
      LB2: [R('layer2.backend.FLOW', 'FLOW', 'pass'),
            R('layer2.backend.fleet', 'xband-fleet', 'fail')],
      LB3: [R('layer3.backend.drops', 'drops', 'warn'),
            R('layer3.composite.FLOW', 'FLOW', 'pass')]
    });
    check('only FLOW remains', Object.keys(by).join(',') === 'FLOW', Object.keys(by).join(','));
    check('and its own readings are intact', radarStatusOf(by, 'FLOW') === 'pass',
      radarStatusOf(by, 'FLOW'));
  }

  console.log('\nstages outside the radar set are ignored:');
  {
    // A product row carries a target too. If L1/LB1 were read here, a product
    // id would land in the map as a radar.
    const by = radarStatusByTarget({
      L1:  [R('layer1.product.qpe_1hr', 'qpe_1hr', 'fail')],
      LB1: [R('layer1.backend.qpe_1hr', 'qpe_1hr', 'fail')],
      L0:  [R('layer0.origin.alive', 'origin', 'fail')],
      LB2: [R('layer2.backend.FLOW', 'FLOW', 'pass')]
    });
    check('products do not become radars', Object.keys(by).join(',') === 'FLOW',
      Object.keys(by).join(','));
    check('a product failure does not reach a radar',
      radarStatusOf(by, 'qpe_1hr') === 'skip', radarStatusOf(by, 'qpe_1hr'));
  }

  console.log('\nthe stage set is the documented one:');
  {
    check('exactly L2, LB2, LB3', RADAR_STATUS_STAGES.join(',') === 'L2,LB2,LB3',
      RADAR_STATUS_STAGES.join(','));
  }

  console.log('\nthe map\'s "down" button selects the broken, not the unknown:');
  {
    // selectDown composes isAlerting over radarStatusOf. The two defaults it
    // could have picked both fail: `?? 'pass'` (what shipped) selected nothing
    // on XQPI, and a plain `!== 'pass'` over `skip` would select the whole
    // fleet before any stage had reported.
    const down = (stages, id) => isAlerting(radarStatusOf(radarStatusByTarget(stages), id));
    const lb2 = (status) => ({ LB2: [R('layer2.backend.FLOW', 'FLOW', status)] });

    check('a failing radar is selected', down(lb2('fail'), 'FLOW') === true);
    check('an erroring radar is selected', down(lb2('error'), 'FLOW') === true);
    check('a warning radar is selected', down(lb2('warn'), 'FLOW') === true);
    check('a healthy radar is not', down(lb2('pass'), 'FLOW') === false);
    check('a radar that declined to judge is not', down(lb2('skip'), 'FLOW') === false);
    check('a radar nothing reported is not', down({}, 'FLOW') === false);
    check('...nor on a profile with no stages at all', down(undefined, 'FLOW') === false);
    // The original symptom: a down radar must now actually be selectable.
    check('the XQPI regression is closed', down(lb2('fail'), 'FLOW') === true);
  }

  console.log('\nthe input is not mutated:');
  {
    const rows = [R('layer2.backend.FLOW', 'FLOW', 'pass')];
    const stages = { LB2: rows };
    radarStatusByTarget(stages);
    check('row array intact', rows.length === 1, String(rows.length));
    check('stages map intact', Object.keys(stages).join(',') === 'LB2');
    check('the row object itself is untouched', rows[0].status === 'pass');
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall radar-status assertions passed');
  return failures.length ? 1 : 0;
}
