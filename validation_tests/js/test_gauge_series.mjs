// Reading the upstream's unmarked timestamps.
//
// Both endpoints publish UTC without a `Z` or an offset, and ECMAScript
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
  const { parseStepTs, gaugeSeries } = mod;
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

  console.log('\na stream reply becomes a series:');
  {
    const j = {
      headers: ['COMID', '2026-11-15-0000', '2026-11-15-0100', '2026-11-15-0200'],
      values: ['8922761', '0.0028316847', '0.0025485162', '0.0022653477']
    };
    const s = gaugeSeries(j);
    check('every hour parses', s.length === 3, String(s.length));
    check('the first column (COMID echo) is dropped',
      s.every((p) => p.v < 1), JSON.stringify(s.map((p) => p.v)));
    check('headers are read as UTC',
      s[0].t === Date.UTC(2026, 10, 15, 0, 0), new Date(s[0].t).toISOString());
    check('the hour field is not read as minutes',
      s[1].t - s[0].t === HOUR, String((s[1].t - s[0].t) / HOUR));
    check('values survive as numbers', Math.abs(s[0].v - 0.0028316847) < 1e-12);
  }
  {
    // A forecast crossing midnight must stay in order, because the chart
    // draws the points in array order and a swap would fold the line back.
    const j = {
      headers: ['COMID', '2026-11-17-0000', '2026-11-16-2300', '2026-11-16-2200'],
      values: ['1', '3', '2', '1']
    };
    const s = gaugeSeries(j);
    check('points come back oldest-first across a date boundary',
      s[0].v === 1 && s[1].v === 2 && s[2].v === 3,
      JSON.stringify(s.map((p) => p.v)));
  }

  console.log('\na bad column costs that column, not the series:');
  for (const [label, j, want] of [
    ['a null reply', null, 0],
    ['no headers', { values: ['1', '2'] }, 0],
    ['no values', { headers: ['COMID', '2026-11-15-0000'] }, 0],
    ['headers only (COMID echo alone)', { headers: ['COMID'], values: ['1'] }, 0],
    ['an unparseable header',
      { headers: ['COMID', 'yesterday', '2026-11-15-0100'], values: ['1', '5', '6'] }, 1],
    ['a non-numeric value',
      { headers: ['COMID', '2026-11-15-0000', '2026-11-15-0100'], values: ['1', 'n/a', '6'] }, 1],
    ['more headers than values',
      { headers: ['COMID', '2026-11-15-0000', '2026-11-15-0100'], values: ['1', '5'] }, 1],
  ]) {
    check(`${label} -> ${want} point(s)`, gaugeSeries(j).length === want,
      String(gaugeSeries(j).length));
  }
  {
    // An empty cell is a missing reading. Number('') is 0, and on a flow
    // chart "no reading" and "no water" are opposite claims.
    const j = {
      headers: ['COMID', '2026-11-15-0000', '2026-11-15-0100'],
      values: ['1', '', '0.5']
    };
    const s = gaugeSeries(j);
    check('an empty cell is dropped, not read as zero flow',
      s.length === 1 && s[0].v === 0.5, JSON.stringify(s.map((p) => p.v)));
  }
  {
    // A measured zero IS a reading and must survive, or a dry creek would
    // vanish from the chart instead of sitting on the baseline.
    const j = {
      headers: ['COMID', '2026-11-15-0000', '2026-11-15-0100'],
      values: ['1', '0.0', '0']
    };
    check('a measured zero is kept', gaugeSeries(j).length === 2,
      JSON.stringify(gaugeSeries(j)));
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall gauge-series assertions passed');
  return failures.length ? 1 : 0;
}
