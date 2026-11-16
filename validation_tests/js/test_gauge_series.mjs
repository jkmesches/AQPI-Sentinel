// Reading the upstream's unmarked timestamps.
//
// product_steps publishes UTC without a `Z` or an offset, and ECMAScript
// reads such a date-time as the VIEWER's local time. Parsing them straight
// shifted every age by the viewer's offset: west of UTC a stale product
// read as fresh, and more than three hours east of it every live product
// read as stale, so the map put a "not current" banner over good data.
// Measured at UTC+5:30, a six-minute-old composite reported as three hours.
//
// The assertions below compare against absolute instants rather than
// against formatted strings, so they fail in whichever zone the runner
// happens to use.

const HOUR = 3_600_000;

export function runTests(mod) {
  const { parseStepTs } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  const tz = process.env.TZ ?? 'unset';
  console.log(`a step timestamp is UTC, whatever zone the viewer is in (TZ=${tz}):`);
  {
    const want = Date.UTC(2026, 10, 16, 22, 14, 0);
    check('an unmarked timestamp is read as UTC',
      parseStepTs('2026-11-16T22:14:00') === want,
      new Date(parseStepTs('2026-11-16T22:14:00')).toISOString());
    check('...and is NOT shifted by the local offset',
      parseStepTs('2026-11-16T22:14:00') !== Date.parse('2026-11-16T22:14:00')
        || new Date().getTimezoneOffset() === 0);
    check('an explicit Z is left alone',
      parseStepTs('2026-11-16T22:14:00Z') === want);
    check('an explicit +00:00 is left alone',
      parseStepTs('2026-11-16T22:14:00+00:00') === want);
    check('a real offset is honoured, not overridden',
      parseStepTs('2026-11-16T17:14:00-05:00') === want);
    // fcst_temp publishes nanosecond precision; Date.parse tolerates it but
    // only once the zone is appended, so this is the case that would throw
    // the forecast products off if the suffix logic were naive.
    check('nanosecond fractions still parse',
      parseStepTs('2026-11-22T00:00:00.000000000') === Date.UTC(2026, 10, 22),
      String(parseStepTs('2026-11-22T00:00:00.000000000')));
    check('garbage yields NaN rather than a plausible date',
      Number.isNaN(parseStepTs('not a time')));
  }

  console.log('\nan age is measured from the real instant:');
  {
    // The deciding case. A product whose newest step is 20 minutes old must
    // read as 20 minutes old everywhere, so no viewer sees a stale banner
    // over live data and no viewer misses one over dead data.
    const newest = '2026-11-16T22:14:00';
    const ageH = (Date.UTC(2026, 10, 16, 22, 34) - parseStepTs(newest)) / HOUR;
    check('20 minutes reads as 0.33 h, not as the local offset',
      Math.abs(ageH - 1 / 3) < 1e-9, `${ageH.toFixed(4)} h`);
    check('...so it is nowhere near the 3 h stale threshold', ageH < 3);
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall gauge-series assertions passed');
  return failures.length ? 1 : 0;
}
