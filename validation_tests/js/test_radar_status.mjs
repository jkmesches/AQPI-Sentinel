// One status per radar, collapsed across the stages that observe one.
//
// Both maps render this. The desktop one read `L2` alone and defaulted an
// unmatched radar to `pass`, which painted FLOW permanently green on XQPI,
// where `L2` registers no checks at all because nothing serves FLOW over
// HTTP. The mobile map had the correct rule inline and the desktop copy was
// never updated, so the bug was invisible from either file on its own.
//
// Two assertions here are the ones that catch a shipped bug. A radar nothing
// reported must come back `skip`, never `pass`. And where the filesystem has a
// reading, radarca must not paint, because its verdict on a radar whose
// volumes are landing on disk is what turned working radars red.
//
// Everything else could pass while either bug was live.

const R = (check_id, target, status) => ({ check_id, target, status });

export function runTests(mod) {
  const { radarStatusByTarget, radarStatusOf, radarVerdicts, radarStageReadings,
          RADAR_STATUS_STAGES, NO_READING } = mod;
  const statusOf = (stages, id) => radarStatusOf(radarStatusByTarget(stages), id);
  const sourceOf = (stages, id) => radarVerdicts(stages)[id]?.from ?? 'absent';
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

  console.log('\nwhere the filesystem has a reading, radarca does not paint:');
  {
    const stages = {
      L2:  [R('layer2.radar.XEBY', 'XEBY', 'pass')],
      LB2: [R('layer2.backend.XEBY', 'XEBY', 'fail')]
    };
    check('radarca pass + disk fail = fail', statusOf(stages, 'XEBY') === 'fail',
      statusOf(stages, 'XEBY'));
  }
  {
    // The combination that painted red pins on working radars. radarca's own
    // status endpoint runs a p90 of 8-13 s and reaches 42 s, and the volumes
    // were on disk the whole time.
    const stages = {
      L2:  [R('layer2.radar.XEBY', 'XEBY', 'fail')],
      LB2: [R('layer2.backend.XEBY', 'XEBY', 'pass')]
    };
    check('disk pass + radarca fail = pass', statusOf(stages, 'XEBY') === 'pass',
      statusOf(stages, 'XEBY'));
    check('...and the verdict names the disk', sourceOf(stages, 'XEBY') === 'LB2',
      sourceOf(stages, 'XEBY'));
  }
  {
    // A radar can arrive on time and still drop out of the composite. That
    // fault belongs to the composite, and it is reported on the Products rail
    // named by which radars are missing, so the pin keeps its arrival verdict.
    const stages = {
      L2:  [R('layer2.radar.XSCR', 'XSCR', 'pass')],
      LB2: [R('layer2.backend.XSCR', 'XSCR', 'pass')],
      LB3: [R('layer3.composite.XSCR', 'XSCR', 'warn')]
    };
    check('a composite dropout does not repaint an arriving radar',
      statusOf(stages, 'XSCR') === 'pass', statusOf(stages, 'XSCR'));
    check('...with LB2 named as what decided', sourceOf(stages, 'XSCR') === 'LB2',
      sourceOf(stages, 'XSCR'));
  }
  {
    // KBBX, KDAX and KMUX on the AQPI deployment. They reach the composite by a path
    // with no arrival check, so composite input is the only thing Sentinel
    // observes about them and it stands as their arrival reading.
    const stages = {
      LB3: [R('layer3.composite.KDAX', 'KDAX', 'fail'),
            R('layer3.input.KDAX', 'KDAX', 'warn')]
    };
    check('LB3 decides where LB2 has no row', statusOf(stages, 'KDAX') === 'fail',
      statusOf(stages, 'KDAX'));
    check('...worst of its own rows, and named LB3', sourceOf(stages, 'KDAX') === 'LB3',
      sourceOf(stages, 'KDAX'));
  }
  {
    // A radar only radarca knows about, on a deployment that reads the
    // filesystem. Its opinion is not evidence, and gray says the backend does
    // not see the radar.
    const stages = {
      L2:  [R('layer2.radar.XNEW', 'XNEW', 'pass')],
      LB2: [R('layer2.backend.XEBY', 'XEBY', 'pass')]
    };
    check('radarca alone paints nothing on a backend deployment',
      statusOf(stages, 'XNEW') === 'skip', statusOf(stages, 'XNEW'));
    check('...and the verdict claims no source', sourceOf(stages, 'XNEW') === 'none',
      sourceOf(stages, 'XNEW'));
    check('...while the radar beside it is unaffected',
      statusOf(stages, 'XEBY') === 'pass', statusOf(stages, 'XEBY'));
  }

  console.log('\nwithin one stage, the worst row wins:');
  {
    // `skip` ranks BELOW pass (see grouping.STATUS_RANK), so one check
    // declining to judge must not gray out a radar its neighbors checked.
    const stages = {
      LB2: [R('layer2.backend.XSCW', 'XSCW', 'skip'),
            R('layer2.backend.XSCW.age', 'XSCW', 'pass')]
    };
    check('a skipped reading does not outrank a real pass',
      statusOf(stages, 'XSCW') === 'pass', statusOf(stages, 'XSCW'));
  }
  {
    // A check that threw cannot vouch for the radar, and `error` is not the
    // same answer as `skip`: the second would claim nothing is watching.
    const stages = {
      LB2: [R('layer2.backend.KDAX', 'KDAX', 'warn'),
            R('layer2.backend.KDAX.age', 'KDAX', 'error')]
    };
    check('error outranks warn', statusOf(stages, 'KDAX') === 'error',
      statusOf(stages, 'KDAX'));
  }

  console.log('\nwith no backend row at all, radarca is all there is:');
  {
    const stages = {
      L2: [R('layer2.radar.XEBY', 'XEBY', 'fail'),
           R('layer2.radar.XSCR', 'XSCR', 'pass')]
    };
    check('an L2 fail paints', statusOf(stages, 'XEBY') === 'fail',
      statusOf(stages, 'XEBY'));
    check('an L2 pass paints', statusOf(stages, 'XSCR') === 'pass',
      statusOf(stages, 'XSCR'));
    check('...and says L2 did it', sourceOf(stages, 'XEBY') === 'L2',
      sourceOf(stages, 'XEBY'));
  }
  {
    // The hazard in deciding the tier by a stage key. The test deployment
    // has no mount, and a present-but-empty LB2 must not gray out its fleet.
    const stages = { L2: [R('layer2.radar.XEBY', 'XEBY', 'pass')], LB2: [], LB3: [] };
    check('an empty LB2 falls back to radarca', statusOf(stages, 'XEBY') === 'pass',
      statusOf(stages, 'XEBY'));
  }

  console.log('\nthe hover card gets every stage that reported the radar:');
  {
    // The case the pin cannot show: arriving on time, missing from the
    // composite. The verdict is `pass` and the second row says why the
    // composite is short.
    const stages = {
      L2:  [R('layer2.radar.XSCR', 'XSCR', 'fail')],
      LB2: [R('layer2.backend.XSCR', 'XSCR', 'pass')],
      LB3: [R('layer3.composite.XSCR', 'XSCR', 'warn')]
    };
    const rows = radarStageReadings(stages, 'XSCR');
    check('one row per reporting stage', rows.length === 3, String(rows.length));
    check('...in the order the verdict considers them',
      rows.map((x) => x.stage).join(',') === 'LB2,LB3,L2',
      rows.map((x) => x.stage).join(','));
    check('...each labeled by what it observes',
      rows.map((x) => x.subject).join(',') === 'arrival,composite,radarca',
      rows.map((x) => x.subject).join(','));
    check('...carrying its own status, not the verdict',
      rows.map((x) => x.status).join(',') === 'pass,warn,fail',
      rows.map((x) => x.status).join(','));
    check('...while the pin stays on the verdict', statusOf(stages, 'XSCR') === 'pass',
      statusOf(stages, 'XSCR'));
  }
  {
    // A stage with no row is left out rather than listed as `skip`: a check
    // the profile never registered is not a reading.
    const stages = { LB2: [R('layer2.backend.FLOW', 'FLOW', 'pass')] };
    check('an unregistered stage is absent, not skip',
      radarStageReadings(stages, 'FLOW').map((x) => x.stage).join(',') === 'LB2',
      radarStageReadings(stages, 'FLOW').map((x) => x.stage).join(','));
    check('a radar nothing reported gets no rows',
      radarStageReadings(stages, 'XEBY').length === 0);
    check('no stages at all gets no rows',
      radarStageReadings(undefined, 'FLOW').length === 0
      && radarStageReadings(null, 'FLOW').length === 0);
  }
  {
    // Worst row per stage, so two arrival checks on one radar collapse the
    // same way the verdict collapses them.
    const stages = {
      LB2: [R('layer2.backend.XEBY', 'XEBY', 'pass'),
            R('layer2.backend.XEBY.age', 'XEBY', 'fail')]
    };
    check('each row is the worst of its stage',
      radarStageReadings(stages, 'XEBY')[0].status === 'fail',
      radarStageReadings(stages, 'XEBY')[0].status);
  }
  {
    // The fleet and DROPS rows are dropped here too, or a card would show a
    // reading that is not about this radar.
    const stages = {
      LB2: [R('layer2.backend.fleet', 'xband-fleet', 'fail')],
      LB3: [R('layer3.backend.drops', 'drops', 'fail')]
    };
    check('no fleet row reaches a card',
      radarStageReadings(stages, 'xband-fleet').length === 0);
    check('no DROPS row reaches a card',
      radarStageReadings(stages, 'drops').length === 0);
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
