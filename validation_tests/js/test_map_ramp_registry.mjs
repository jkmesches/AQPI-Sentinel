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
    rampForProduct, productCoverageFor, tiltRampFor, TILT_PAIRS
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
    //
    // This used to demand that at least one pair be rejected, so the loop
    // below was never testing nothing. That made a record with no
    // failures left in it fail, which is a rule that punishes the work
    // going well. What it was really after is that every pair lands in
    // one bucket or the other, and that holds whichever is empty.
    check('every pair is either covered or rejected, with none adrift',
      covered.length + rejected.length === coverage.pairs.length
      && coverage.pairs.length > 0,
      `${covered.length} covered, ${rejected.length} rejected, `
      + `${coverage.pairs.length} pairs`);
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
    // Asked as "its own table", not as "no table". These two read 0 of
    // 253 and 0 of 256 against the X-band's until the renderer was read
    // and their real colormaps checked in, and a rule that demanded the
    // gap would have failed on the gap closing. What has to stay true is
    // that nothing hands the C-band an X-band table for a moment it
    // paints differently, which is the fault those counts were catching.
    check('the C-band ZDR has a table of its own',
      rampFor('CBAND', 'CorrDifferentialReflectivity') !== null
      && rampFor('CBAND', 'CorrDifferentialReflectivity')
         !== rampFor('XEBY', 'CorrDifferentialReflectivity'));
    check('the C-band PhiDP has a table of its own',
      rampFor('CBAND', 'PhiDP') !== null
      && rampFor('CBAND', 'PhiDP') !== rampFor('XEBY', 'FilteredPhiDP'));
    check('and its ZDR range is the renderer\'s, not the X-band\'s',
      rampFor('CBAND', 'CorrDifferentialReflectivity')?.min === -1
      && rampFor('CBAND', 'CorrDifferentialReflectivity')?.max === 5,
      `${rampFor('CBAND', 'CorrDifferentialReflectivity')?.min} to `
      + `${rampFor('CBAND', 'CorrDifferentialReflectivity')?.max}`);
    // Asked as "has no table", not as "has no row". The row appeared the
    // day someone looked, and a rule that demanded its absence would have
    // failed on the record getting better rather than on anything breaking.
    // The C-band is the only radar that publishes RhoHV, so whatever
    // table it has cannot be shared with anyone, and the question is
    // whether it reads at all and on its own scale.
    const rhohv = rampFor('CBAND', 'RhoHV');
    check('the C-band RhoHV reads', rhohv !== null);
    check('on the renderer\'s own 0 to 1', rhohv?.min === 0 && rhohv?.max === 1,
      `${rhohv?.min} to ${rhohv?.max}`);
    check('and no X-band radar is offered it',
      ['XEBY', 'XSCV', 'XSCW', 'XSCR', 'XSWR']
        .every((r) => rampFor(r, 'RhoHV') === null));
    check('the C-band reflectivity is the same object XEBY gets',
      rampFor('CBAND', 'CorrReflectivity')
      === rampFor('XEBY', 'CorrReflectivity'));
  }

  console.log('\nthe AQPI reflectivity ramp follows the frames, not the bar:');
  {
    // v0.15.0 read this ramp off the published colorbar, 22 linear bins
    // over -20 to 80, and an earlier version of this group asserted that
    // it therefore meant values 7.3 dBZ away from FLOW's for one color.
    // The frames are painted through BoundaryNorm(-25..75 by 5, extend
    // both) and the bar is not, so the readout was off by up to 9.5 dBZ.
    // Under the frames' own norm the two deployments agree bin for bin.
    const aqpi = rampFor('XEBY', 'CorrReflectivity');
    const flow = rampFor('FLOW', 'Reflectivity');
    check('both exist', aqpi !== null && flow !== null);
    check('they are different tables', aqpi !== flow);
    const aColors = aqpi.stops.map((s) => s.color).join();
    const fColors = flow.stops.map((s) => s.color).join();
    check('their colors are identical', aColors === fColors);
    const cyan = aqpi.stops.find((s) => s.color === '#04e8e6');
    check('#04e8e6 is 5 to 10 dBZ, where the NWS palette puts it',
      cyan?.lo === 5 && cyan?.hi === 10, `${cyan?.lo} to ${cyan?.hi}`);
    // FLOW's edges were measured off its bar and sit up to 1.2 dBZ from
    // the round fives, so the test is that each FLOW center falls in the
    // AQPI bin of the same color, not that the edges are equal.
    const inner = aqpi.stops.slice(1, -1).every((s, i) => {
      const v = flow.stops[i + 1].value;
      return v >= s.lo && v < s.hi;
    });
    check('inside -25 to 75 each color names the same 5 dBZ bin on both', inner);
    check('the ends are open, below -25 and from 75 up',
      aqpi.stops[0].open === 'below' && aqpi.stops[0].hi === -25
      && aqpi.stops[21].open === 'above' && aqpi.stops[21].lo === 75,
      `${aqpi.stops[0].open} ${aqpi.stops[0].hi}, `
      + `${aqpi.stops[21].open} ${aqpi.stops[21].lo}`);
    check('the C-band reads through the same table',
      rampFor('CBAND', 'CorrReflectivity') === aqpi);
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
    for (const p of TILT_PAIRS.filter((r) => r.covered)) {
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
        p.frames > 0 && (p.pixels > 0 || !!p.note),
        `${p.frames} frames, ${p.pixels} px, covered=${p.covered}`);
      check(`${p.radar}/${p.moment} reports a matched-over-distinct count`,
        /^\d+\/\d+$/.test(p.colors), p.colors);
      // Two kinds of evidence, each with its own bar to clear. A pair
      // covered on its frames has to have read every color back. A pair
      // covered on its own published colorbar has to name that bar's
      // hash, because the whole claim is that the table came from a file
      // this radar publishes rather than from a sibling's.
      const [got, total] = p.colors.split('/').map(Number);
      const basis = p.basis ?? 'frames';
      check(`${p.radar}/${p.moment} names a basis this file knows`,
        basis === 'frames' || basis === 'colorbar', basis);
      const earned = basis === 'colorbar'
        ? !!p.colorbar_md5 && !!p.ramp
        : got === total && total > 0;
      check(`${p.radar}/${p.moment}: covered agrees with its evidence`,
        p.covered === earned,
        `basis=${basis} ${p.colors} md5=${p.colorbar_md5 ?? '-'} `
        + `covered=${p.covered}`);
      check(`${p.radar}/${p.moment}: a colorbar basis says what is outstanding`,
        basis === 'frames' || /frame check/i.test(p.note ?? ''),
        p.note ? p.note.slice(0, 60) : 'no note');
    }
  }

  // The tilt frames are radar-display's, painted from other tables over
  // other ranges than Chivo's, and both trees spell one moment "velocity".
  // The registry folds case, so the two have separate indexes, and a
  // shared one would hand the readout whichever table was registered last.
  console.log('\na tilt pair answers from its own record:');
  {
    for (const p of TILT_PAIRS.filter((r) => r.covered)) {
      const r = tiltRampFor(p.radar, p.moment);
      check(`tilt ${p.radar}/${p.moment} -> ${p.ramp}`,
        r !== null && r === RAMPS[p.ramp],
        r ? `${r.moment} ${r.min}..${r.max} ${r.unit}` : 'null');
      const [got, total] = p.colors.split('/').map(Number);
      check(`tilt ${p.radar}/${p.moment}: covered agrees with its evidence`,
        p.covered === (got === total && total > 0), p.colors);
    }
    const tiltVel = tiltRampFor('XSCV', 'velocity');
    const chivoVel = rampFor('XSCV', 'Velocity');
    check('a tilt velocity and a Chivo Velocity are different tables',
      tiltVel !== null && chivoVel !== null && tiltVel !== chivoVel,
      `${tiltVel?.min}..${tiltVel?.max} vs ${chivoVel?.min}..${chivoVel?.max}`);
    check('...and the Chivo lookup never answers with a tilt table',
      !['reflectivity', 'velocity', 'copolarcorrelation'].some((m) => {
        const r = rampFor('XSCV', m);
        return r !== null && r.source === RAMPS['tilt-velocity'].source;
      }));
    check('an unmeasured radar has no tilt ramp', tiltRampFor('FLOW', 'velocity') === null);
    check('a Chivo moment name is not a tilt moment',
      tiltRampFor('XSCV', 'CorrReflectivity') === null);
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall ramp registry assertions passed');
  return failures.length ? 1 : 0;
}
