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

export function runTests(mod, coverage, products) {
  const {
    rampFor, coverageFor, radarsSharing, keyOf, RAMPS, MEASURED_ON,
    rampForProduct, productCoverageFor
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
    // Asked as "has no table", not as "has no row". The row appeared the
    // day someone looked, and a rule that demanded its absence would have
    // failed on the record getting better rather than on anything breaking.
    const rhohv = coverageFor('CBAND', 'RhoHV');
    check('the C-band RhoHV has no table',
      rampFor('CBAND', 'RhoHV') === null);
    check('and if it has been looked at, the record says why not',
      rhohv === null || (rhohv.covered === false && !!rhohv.note),
      rhohv ? `covered=${rhohv.covered} note=${!!rhohv.note}` : 'no row');
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
    for (const p of products.products.filter((r) => r.covered)) {
      check(`${p.ramp} is in RAMPS for ${p.product}`, !!RAMPS[p.ramp]);
      named.add(p.ramp);
    }
    const unused = Object.keys(RAMPS).filter((n) => !named.has(n));
    check('no ramp ships without a covered pair or product',
      unused.length === 0, unused.join(' ') || 'none');
  }

  // A product reaches a legend and a readout only on evidence, the same
  // rule the radar pairs follow. radarca publishes a colorbar for every
  // product and several of them do not read their own frames, so having
  // a bar is not evidence that it reads the pixels.
  console.log('\na product answers only where it was measured:');
  {
    check('a covered product finds its ramp',
      rampForProduct('comp_ref') !== null);
    // Over whatever the file records rather than over one named
    // product. Naming qpe_1hr here meant that measuring it and letting
    // it through failed a check about rejection in general.
    const rejected = products.products.filter((r) => !r.covered);
    check('every covered row resolves to its own ramp',
      products.products.filter((r) => r.covered).every(
        (r) => rampForProduct(r.product, r.unit) !== null),
      `${products.products.filter((r) => r.covered).length} covered`);
    check('no rejected row resolves to anything',
      rejected.every((r) => rampForProduct(r.product, r.unit) === null),
      `${rejected.length} rejected`);
    check('an unknown product finds none',
      rampForProduct('not_a_product') === null);
    check('the unit picks the bar',
      rampForProduct('fcst_total_precip', 'in') !== null);
    check('a single-unit product answers without one',
      rampForProduct('water_depth', null) !== null);
    for (const p of products.products.filter((r) => !r.covered)) {
      check(`${p.product} reports nothing`,
        rampForProduct(p.product, p.unit) === null);
      check(`${p.product} says why`,
        (productCoverageFor(p.product, p.unit)?.note ?? '').length > 20);
    }
    // A rejected row records what the measurement found, so a later
    // pass starts from evidence rather than from scratch. That is how
    // the QPE products came back: their rows carried the pixel share
    // the published bar managed, which is what said the bar was wrong
    // rather than the products unreadable.
    for (const p of rejected) {
      check(`${p.product} keeps what was measured`,
        Number.isFinite(productCoverageFor(p.product, p.unit)?.pixels),
        String(productCoverageFor(p.product, p.unit)?.pixels));
    }
    // A covered row carries what it measured. No threshold on the
    // share itself, because what counts as enough differs by ramp:
    // comp_ref reads 33% and is right, since the rest of its frame is
    // coastline drawn into the image rather than data. What the file
    // owes a reader is the reason, so a share short of near-total has
    // to say where the remainder went.
    const SPEAKS_FOR_ITSELF = 90;
    for (const p of products.products.filter((r) => r.covered)) {
      const row = productCoverageFor(p.product, p.unit);
      const share = row?.pixels;
      check(`${p.product}/${p.unit} records what it measured`,
        Number.isFinite(share) && share > 0, String(share));
      if (Number.isFinite(share) && share < SPEAKS_FOR_ITSELF) {
        check(`${p.product}/${p.unit} says where the other ` +
          `${(100 - share).toFixed(0)}% went`,
          (row?.note ?? '').length > 20,
          row?.note ? 'noted' : 'NO NOTE');
      }
    }
  }

  console.log('\nthe shared-colorbar record names only real radars:');
  {
    for (const [name, rec] of Object.entries(coverage.shared_colorbars)) {
      if (name.startsWith('_')) continue;
      check(`${name} names a checked-in ramp`, !!RAMPS[name]);
      check(`${name} lists radars`, radarsSharing(name).length > 0,
        radarsSharing(name).join(' '));
      // The record says these radars publish a byte-identical colorbar,
      // so one of THEM has to carry the frame measurement that anchors
      // it. Byte identity moves a measurement between radars; it does
      // not make one.
      //
      // Asking instead whether one arbitrary covered row's radar was in
      // the list made the check depend on the order of coverage.json,
      // and adding three measured pairs reordered it onto the C-band,
      // which is deliberately absent from these lists and is measured
      // from its own frames.
      const anchors = covered.filter((p) => p.ramp === name
        && radarsSharing(name).some(
          (r) => r.toLowerCase() === p.radar.toLowerCase()));
      check(`${name} is anchored by a radar it names`,
        anchors.length > 0,
        anchors.map((p) => `${p.radar}/${p.moment}`).join(' ') || 'none');
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
      // Frames always, pixels only where there were any. A pair whose
      // frames are all transparent is a result the record should keep,
      // and the rule that matters is that nothing claims coverage from
      // nothing, which the count check below enforces. Demanding pixels
      // instead pushed a real null result out of the record.
      check(`${p.radar}/${p.moment} reports what it sampled`,
        p.frames > 0 && (p.pixels > 0 || (!p.covered && !!p.note)),
        `${p.frames} frames, ${p.pixels} px, covered=${p.covered}`);
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
