// Burn-scar age filtering.
//
// Run:  node validation_tests/js/run_map_burn_scars.mjs
//
// The filter is a MapLibre expression, evaluated by the style and never by
// code a test can step through. A wrong expression draws a plausible map,
// and reading the window it describes is the only way to see the fault, so
// the assertions below treat the expression as data.
//
// The case that matters is a perimeter with no ALARM_DATE. Without the
// `has` guard, `['get', 'ALARM_DATE']` is null, the subtraction is null, and
// `['<=', null, years]` lets it through, so every undated polygon in the
// file draws at every slider position.

export function runTests(mod) {
  const {
    burnScarAge, burnScarFilter, newestBurnScar,
    YEAR_MS, ALARM_DATE, BURN_SCAR_SOURCE, BURN_SCAR_FILL, BURN_SCAR_LINE
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  const NOW = Date.UTC(2026, 10, 20, 12, 0, 0);
  const yearsAgo = (y) => NOW - y * YEAR_MS;

  // A tiny evaluator for the subset of expressions this module emits. Enough
  // to answer which features a filter admits, without pulling in MapLibre.
  const evaluate = (expr, props) => {
    if (!Array.isArray(expr)) return expr;
    const [op, ...args] = expr;
    const val = (a) => evaluate(a, props);
    if (op === 'get') return props[args[0]] ?? null;
    if (op === 'has') return Object.prototype.hasOwnProperty.call(props, args[0]);
    if (op === 'all') return args.every((a) => val(a) === true);
    if (op === '-') {
      const [a, b] = args.map(val);
      return a === null || b === null ? null : a - b;
    }
    if (op === '/') {
      const [a, b] = args.map(val);
      return a === null || b === null ? null : a / b;
    }
    if (op === '<=') {
      const [a, b] = args.map(val);
      // MapLibre refuses a comparison against null rather than coercing it.
      return a === null || b === null ? false : a <= b;
    }
    throw new Error(`unhandled operator ${op}`);
  };
  const admits = (years, props) => evaluate(burnScarFilter(years, NOW), props);

  console.log('a year is 365.25 days:');
  {
    check('YEAR_MS is the Julian year', YEAR_MS === 365.25 * 24 * 3600 * 1000,
      String(YEAR_MS));
    // A 365-day year drifts a quarter day each year, which is 7.5 days over
    // the slider's 30-year span.
    check('...which differs from 365 days by 7.5 days over 30 years',
      Math.abs((YEAR_MS - 365 * 24 * 3600 * 1000) * 30 / 86400000 - 7.5) < 0.01);
  }

  console.log('\nthe age expression measures years since the fire:');
  {
    const age = (y) => evaluate(burnScarAge(NOW), { [ALARM_DATE]: yearsAgo(y) });
    check('a fire today is age zero', Math.abs(age(0)) < 1e-9, String(age(0)));
    check('a fire one year ago is age one', Math.abs(age(1) - 1) < 1e-9);
    check('a fire ten years ago is age ten', Math.abs(age(10) - 10) < 1e-9);
    check('the clock is injected, not read',
      JSON.stringify(burnScarAge(NOW)) === JSON.stringify(burnScarAge(NOW)));
    check('...and a different clock gives a different expression',
      JSON.stringify(burnScarAge(NOW)) !== JSON.stringify(burnScarAge(NOW + 1000)));
  }

  console.log('\nthe window admits what it says it does:');
  {
    check('a fire inside the window draws',
      admits(10, { [ALARM_DATE]: yearsAgo(5) }) === true);
    check('a fire outside it does not',
      admits(10, { [ALARM_DATE]: yearsAgo(11) }) === false);
    check('exactly at the edge draws, since the test is <=',
      admits(10, { [ALARM_DATE]: yearsAgo(10) }) === true);
    check('just past the edge does not',
      admits(10, { [ALARM_DATE]: yearsAgo(10.001) }) === false);
    check('the default window is two years', admits(2, { [ALARM_DATE]: yearsAgo(1.9) }) === true
      && admits(2, { [ALARM_DATE]: yearsAgo(2.1) }) === false);
    check('the widest window still excludes older',
      admits(30, { [ALARM_DATE]: yearsAgo(29) }) === true
      && admits(30, { [ALARM_DATE]: yearsAgo(31) }) === false);
  }

  console.log('\nan undated perimeter never draws:');
  {
    // The regression this file exists for.
    check('no ALARM_DATE at the narrow end', admits(2, {}) === false);
    check('...nor at the wide end', admits(30, {}) === false);
    check('...nor with other properties present',
      admits(30, { FIRE_NAME: 'KINCADE', GIS_ACRES: 77758 }) === false);
    check('the guard is the first clause',
      burnScarFilter(10, NOW)[1][0] === 'has'
      && burnScarFilter(10, NOW)[1][1] === ALARM_DATE,
      JSON.stringify(burnScarFilter(10, NOW)[1]));
    // A future date is data we do not trust, and it must not be special.
    check('a date in the future still draws, as any recent fire would',
      admits(2, { [ALARM_DATE]: NOW + YEAR_MS }) === true);
  }

  console.log('\nthe panel names the newest perimeter:');
  {
    const fc = (...dates) => ({
      type: 'FeatureCollection',
      features: dates.map((d) => ({ properties: d === null ? {} : { [ALARM_DATE]: d } }))
    });
    check('the most recent of several',
      newestBurnScar(fc(yearsAgo(5), yearsAgo(1), yearsAgo(9))) === 'Nov 2025',
      newestBurnScar(fc(yearsAgo(5), yearsAgo(1), yearsAgo(9))));
    check('one feature names itself',
      newestBurnScar(fc(Date.UTC(2025, 11, 14))) === 'Dec 2025');
    check('an empty collection says nothing', newestBurnScar(fc()) === '');
    check('all undated says nothing', newestBurnScar(fc(null, null)) === '');
    check('a dated feature beside undated ones still counts',
      newestBurnScar(fc(null, Date.UTC(2024, 6, 2), null)) === 'Jul 2024');
    // Local time would print the previous month for a fire just after UTC
    // midnight on the first, west of Greenwich.
    check('the month is read in UTC',
      newestBurnScar(fc(Date.UTC(2025, 2, 1, 2, 0))) === 'Mar 2025',
      newestBurnScar(fc(Date.UTC(2025, 2, 1, 2, 0))));
    check('a zero date is treated as absent', newestBurnScar(fc(0)) === '');
  }

  console.log('\nthe ids the layers are built under:');
  {
    check('one source', BURN_SCAR_SOURCE === 'burn-scars', BURN_SCAR_SOURCE);
    check('a fill and a line on it',
      BURN_SCAR_FILL === 'burn-scar-fill' && BURN_SCAR_LINE === 'burn-scar-line');
    check('the layer ids differ from the source id',
      BURN_SCAR_FILL !== BURN_SCAR_SOURCE && BURN_SCAR_LINE !== BURN_SCAR_SOURCE);
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall burn-scar assertions passed');
  return failures.length ? 1 : 0;
}
