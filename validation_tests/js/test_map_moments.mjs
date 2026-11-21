// The moment vocabulary a deployment offers.
//
// Run:  node validation_tests/js/run_map_moments.mjs
//
// This exists because the console carried AQPI's five moment labels as a
// constant while XQPI names two of them differently and has no RhoHV. Three
// of the five buttons on the XQPI deployment sent a label the scan route
// answers 400 to. The assertions below use the real payload both profiles
// produce, so the regression is a failing test rather than a dead button on
// one deployment.

const AQPI = {
  XEBY: {
    'Reflectivity': 'CorrReflectivity',
    'Differential Reflectivity': 'CorrDifferentialReflectivity',
    'Velocity': 'Velocity',
    'PhiDP': 'FilteredPhiDP',
    'RhoHV': 'RhoHV'
  },
  CBAND: {
    'Reflectivity': 'CorrReflectivity',
    'Differential Reflectivity': 'CorrDifferentialReflectivity',
    'Velocity': 'Velocity',
    'PhiDP': 'PhiDP',
    'RhoHV': 'RhoHV'
  }
};
const XQPI = {
  FLOW: {
    'Reflectivity': 'Reflectivity',
    'Velocity': 'Velocity',
    'DifferentialReflectivity': 'DifferentialReflectivity',
    'DifferentialPhase': 'DifferentialPhase'
  }
};
const LEGACY = ['Reflectivity', 'Velocity', 'Differential Reflectivity',
  'PhiDP', 'RhoHV'];

export function runTests(mod) {
  const { momentChoices, momentDisplay, prefixFor, defaultMoment } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  console.log('each profile offers its own labels:');
  {
    const a = momentChoices(AQPI, LEGACY).map((c) => c.key);
    check('AQPI offers five', a.length === 5, a.join(' | '));
    check('AQPI keeps its spelling',
      a.includes('Differential Reflectivity') && a.includes('PhiDP'));
    const x = momentChoices(XQPI, LEGACY).map((c) => c.key);
    check('XQPI offers four', x.length === 4, x.join(' | '));
    check('XQPI uses its own spelling',
      x.includes('DifferentialReflectivity') && x.includes('DifferentialPhase'));
    check('XQPI does not offer RhoHV', !x.includes('RhoHV'));
    check('XQPI does not offer the AQPI spelling',
      !x.includes('Differential Reflectivity') && !x.includes('PhiDP'));
  }

  console.log('\nevery offered label resolves to a prefix on some radar:');
  {
    // The regression. A label the picker offers but the backend cannot map
    // is a button that answers 400.
    for (const [name, table] of [['AQPI', AQPI], ['XQPI', XQPI]]) {
      for (const c of momentChoices(table, LEGACY)) {
        const ok = Object.keys(table).some(
          (r) => prefixFor(table, r, c.key) !== null);
        check(`${name}: ${c.key} maps on some radar`, ok);
      }
    }
  }

  console.log('\nthe C-band exception survives the round trip:');
  {
    check('the X-band PhiDP is FilteredPhiDP',
      prefixFor(AQPI, 'XEBY', 'PhiDP') === 'FilteredPhiDP',
      String(prefixFor(AQPI, 'XEBY', 'PhiDP')));
    check('the C-band PhiDP is PhiDP',
      prefixFor(AQPI, 'CBAND', 'PhiDP') === 'PhiDP',
      String(prefixFor(AQPI, 'CBAND', 'PhiDP')));
    check('they differ, which is the whole exception',
      prefixFor(AQPI, 'XEBY', 'PhiDP') !== prefixFor(AQPI, 'CBAND', 'PhiDP'));
  }

  console.log('\nthe order is the same on both profiles:');
  {
    const a = momentChoices(AQPI, LEGACY).map((c) => c.full);
    const x = momentChoices(XQPI, LEGACY).map((c) => c.full);
    check('AQPI reads Z, V, Zdr, PhiDP, RhoHV',
      a.join(',') === 'Reflectivity,Velocity,Differential Refl,Differential Phase,Correlation Coef',
      a.join(','));
    check('XQPI reads the same, minus the one it lacks',
      x.join(',') === 'Reflectivity,Velocity,Differential Refl,Differential Phase',
      x.join(','));
    check('the two spellings land in the same slot',
      a.indexOf('Differential Refl') === x.indexOf('Differential Refl'));
  }

  console.log('\nthe picker still works before the payload arrives:');
  {
    const f = momentChoices(null, LEGACY);
    check('it falls back to the given list', f.length === LEGACY.length,
      f.map((c) => c.key).join(' '));
    check('an empty table also falls back',
      momentChoices({}, LEGACY).length === LEGACY.length);
    check('a radar with no moments also falls back',
      momentChoices({ XEBY: {} }, LEGACY).length === LEGACY.length);
    check('an empty fallback gives nothing',
      momentChoices(null, []).length === 0);
  }

  console.log('\nthe union covers a moment only one radar carries:');
  {
    const mixed = { A: { 'Reflectivity': 'R' }, B: { 'Velocity': 'V' } };
    const keys = momentChoices(mixed, LEGACY).map((c) => c.key);
    check('both appear', keys.length === 2 && keys.includes('Reflectivity')
      && keys.includes('Velocity'), keys.join(' '));
    const dup = { A: { 'Reflectivity': 'R' }, B: { 'Reflectivity': 'R' } };
    check('a shared moment appears once',
      momentChoices(dup, LEGACY).length === 1);
  }

  console.log('\nan unknown label is shown rather than dropped:');
  {
    const d = momentDisplay('SomeNewMoment');
    check('it names itself', d.short === 'SomeNewMoment'
      && d.full === 'SomeNewMoment', `${d.short}/${d.full}`);
    const keys = momentChoices({ A: { SomeNewMoment: 'X' } }, LEGACY)
      .map((c) => c.key);
    check('and still reaches the picker', keys.includes('SomeNewMoment'),
      keys.join(' '));
    check('it sorts after the known ones',
      momentChoices({ A: { SomeNewMoment: 'X', Velocity: 'V' } }, LEGACY)
        .map((c) => c.key).join(',') === 'Velocity,SomeNewMoment');
  }

  console.log('\na missing prefix reads as missing:');
  {
    check('an unknown radar', prefixFor(AQPI, 'nosuch', 'Reflectivity') === null);
    check('an unknown moment', prefixFor(AQPI, 'XEBY', 'nosuch') === null);
    check('a null table', prefixFor(null, 'XEBY', 'Reflectivity') === null);
    check('the X-bands carry RhoHV on AQPI',
      prefixFor(AQPI, 'XEBY', 'RhoHV') === 'RhoHV');
    check('FLOW carries no RhoHV',
      prefixFor(XQPI, 'FLOW', 'RhoHV') === null);
  }

  console.log('\nthe default moment is one the deployment has:');
  {
    check('AQPI defaults to reflectivity',
      defaultMoment(momentChoices(AQPI, LEGACY)) === 'Reflectivity');
    check('XQPI defaults to reflectivity',
      defaultMoment(momentChoices(XQPI, LEGACY)) === 'Reflectivity');
    const odd = momentChoices({ A: { Velocity: 'V' } }, LEGACY);
    check('without reflectivity it takes the first',
      defaultMoment(odd) === 'Velocity', String(defaultMoment(odd)));
    check('an empty list has no default', defaultMoment([]) === null);
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall moment vocabulary assertions passed');
  return failures.length ? 1 : 0;
}
