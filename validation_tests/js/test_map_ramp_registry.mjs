// Which ramp reads which radar's which moment.
//
// Run:  node validation_tests/js/run_map_ramp_registry.mjs
//
// The rule this enforces is the plan's: a ramp is measured against real
// frames before it reaches a legend or a readout. The registry answers from
// the coverage record, so the assertions here are mostly about the two ways
// that can go wrong. A pair can be claimed without evidence, and a pair the
// measurement rejected can leak through anyway.
//
// The sharp case is the C-band (CBAND, folder sscb). It paints reflectivity
// and velocity from the X-band tables and ZDR and PhiDP from something else,
// so a registry keyed on profile alone would put a confident dB under the
// cursor on a frame it cannot read.

export function runTests(mod, coverage) {
  const {
    rampFor, coverageFor, radarsSharing, keyOf, RAMPS, MEASURED_ON
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const covered = coverage.pairs.filter((p) => p.covered);
  const rejected = coverage.pairs.filter((p) => !p.covered);

  console.log('every covered pair resolves to its own ramp:');
  {
    for (const p of covered) {
      const r = rampFor(p.radar, p.moment);
      check(`${p.radar}/${p.moment} -> ${p.ramp}`,
        r !== null && r === RAMPS[p.ramp],
        r ? `${r.moment} ${r.min}..${r.max} ${r.unit}` : 'null');
    }
  }

  console.log('\na pair the measurement rejected resolves to nothing:');
  {
    // The rule the plan states outright: anything short of full coverage
    // means the ramp is wrong for that product, and the readout stays off.
    check('there is at least one rejected pair to check', rejected.length > 0,
      `${rejected.length}`);
    for (const p of rejected) {
      check(`${p.radar}/${p.moment} has no ramp`,
        rampFor(p.radar, p.moment) === null,
        JSON.stringify(rampFor(p.radar, p.moment)?.moment ?? null));
      check(`${p.radar}/${p.moment} still reports why`,
        coverageFor(p.radar, p.moment)?.covered === false
        && !!coverageFor(p.radar, p.moment)?.note);
    }
  }

  console.log('\nthe C-band shares two ramps and has its own other two:');
  {
    // The C-band publishes no colorbar of its own, so the only evidence
    // either way is what its frames contain. Sitting in the same tree as the
    // X-band radars says nothing about which table it paints from.
    check('the C-band reflectivity reads',
      rampFor('CBAND', 'CorrReflectivity') !== null);
    check('the C-band velocity reads', rampFor('CBAND', 'Velocity') !== null);
    check('the C-band ZDR does not',
      rampFor('CBAND', 'CorrDifferentialReflectivity') === null);
    check('the C-band PhiDP does not', rampFor('CBAND', 'PhiDP') === null);
    check('the C-band RhoHV was never measured',
      rampFor('CBAND', 'RhoHV') === null
      && coverageFor('CBAND', 'RhoHV') === null);
    check('the C-band reflectivity is the same object XEBY gets',
      rampFor('CBAND', 'CorrReflectivity')
      === rampFor('XEBY', 'CorrReflectivity'));
  }

  console.log('\nthe two reflectivity ramps are not interchangeable:');
  {
    // Same colors, different range. A single shared reflectivity ramp would
    // have been wrong by 7.3 dBZ on one of the two deployments, and nothing
    // on screen would have said so.
    const aqpi = rampFor('XEBY', 'CorrReflectivity');
    const flow = rampFor('FLOW', 'Reflectivity');
    check('both exist', aqpi !== null && flow !== null);
    check('they are different tables', aqpi !== flow);
    const aColors = aqpi.stops.map((s) => s.color).join();
    const fColors = flow.stops.map((s) => s.color).join();
    check('their colors are identical', aColors === fColors);
    check('their ranges are not',
      aqpi.min !== flow.min, `${aqpi.min} vs ${flow.min}`);
    const i = 5;
    const drift = Math.abs(aqpi.stops[i].value - flow.stops[i].value);
    check('so one color means two values, 7.3 dBZ apart',
      Math.abs(drift - 7.26) < 0.1, drift.toFixed(2));
  }

  console.log('\nan unknown radar or moment reads nothing:');
  {
    check('an unknown radar', rampFor('nosuch', 'Reflectivity') === null);
    check('an unknown moment', rampFor('XEBY', 'NoSuchMoment') === null);
    check('both unknown', rampFor('', '') === null);
    check('a moment spelled as another tree spells it',
      rampFor('XEBY', 'reflectivity') === null);
    check('a radar the console cannot select',
      rampFor('KBBX', 'reflectivity') === null);
    check('an X-band moment the C-band does not share',
      rampFor('CBAND', 'FilteredPhiDP') === null);
  }

  console.log('\nthe lookup folds case but keeps the two fields apart:');
  {
    check('lower-case radar', rampFor('xeby', 'CorrReflectivity') !== null);
    check('lower-case moment', rampFor('XEBY', 'corrreflectivity') !== null);
    check('mixed', rampFor('Xeby', 'CORRREFLECTIVITY') !== null);
    // A key built by plain concatenation would make ('ab','c') and ('a','bc')
    // the same pair.
    check('the fields cannot run together',
      keyOf('ab', 'c') !== keyOf('a', 'bc'));
  }

  console.log('\nevery ramp a covered pair names is checked in:');
  {
    const named = new Set(covered.map((p) => p.ramp));
    for (const n of named) {
      check(`${n} is in RAMPS`, !!RAMPS[n]);
    }
    const unused = Object.keys(RAMPS).filter((n) => !named.has(n));
    check('no ramp ships without a covered pair', unused.length === 0,
      unused.join(' ') || 'none');
  }

  console.log('\nthe shared-colorbar record names only real radars:');
  {
    for (const [name, rec] of Object.entries(coverage.shared_colorbars)) {
      if (name.startsWith('_')) continue;
      check(`${name} names a checked-in ramp`, !!RAMPS[name]);
      check(`${name} lists radars`, radarsSharing(name).length > 0,
        radarsSharing(name).join(' '));
      const measured = covered.find((p) => p.ramp === name);
      check(`${name}'s measured radar is among them`,
        !measured || radarsSharing(name)
          .some((r) => r.toLowerCase() === measured.radar.toLowerCase()),
        `${measured?.radar} in [${radarsSharing(name).join(' ')}]`);
    }
    check('an unknown ramp shares with nobody',
      radarsSharing('nosuch').length === 0);
    // The C-band is deliberately absent: it publishes no colorbar, so there is no
    // byte-identity evidence for it, only the frame measurement.
    for (const name of Object.keys(coverage.shared_colorbars)) {
      if (name.startsWith('_')) continue;
      check(`${name} does not claim the C-band`,
        !radarsSharing(name).includes('CBAND'));
    }
  }

  console.log('\nthe record carries its own provenance:');
  {
    check('it is dated', /^\d{4}-\d{2}-\d{2}$/.test(MEASURED_ON), MEASURED_ON);
    for (const p of coverage.pairs) {
      check(`${p.radar}/${p.moment} reports frames and pixels`,
        p.frames > 0 && p.pixels > 0, `${p.frames} frames, ${p.pixels} px`);
      check(`${p.radar}/${p.moment} reports a matched-over-distinct count`,
        /^\d+\/\d+$/.test(p.colors), p.colors);
      const [got, total] = p.colors.split('/').map(Number);
      check(`${p.radar}/${p.moment}: covered agrees with the count`,
        p.covered === (got === total && total > 0),
        `${p.colors} covered=${p.covered}`);
    }
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall ramp registry assertions passed');
  return failures.length ? 1 : 0;
}
