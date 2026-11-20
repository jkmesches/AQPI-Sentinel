// The stream-gauge hydrograph.
//
// Run:  node validation_tests/js/run_map_gauge_chart.mjs
//
// A chart is read for a number, and a wrong one reads the same as a right
// one. This is the chart someone opens about a rising creek, so the
// assertions are about the claims the drawing makes: that the axis starts at
// zero, that a point sits where its value says, that the rule marking now
// appears only when now is on screen, and that a hole between the two series
// is drawn as absence instead of as blank plot area.
//
// The markup is parsed with regular expressions. There is no DOM here, and
// pulling one in to read six attributes would cost more than it explains.

export function runTests(mod) {
  const {
    gaugeChartSvg, gaugeSummaryHtml, gapNote, gaugeNum, svgEsc, hhmm,
    GC_W, GC_H, GC_PAD_L, GC_PAD_R, GC_PAD_T, GC_PAD_B
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  const T0 = Date.UTC(2026, 10, 19, 0, 0, 0);
  const H = 3600000;
  const pts = (n, start, step, f) =>
    Array.from({ length: n }, (_, i) => ({ t: start + i * step, v: f(i) }));
  // The d attribute of one path, as [x, y] pairs.
  const pathPoints = (svg, cls) => {
    const m = svg.match(new RegExp(`class="${cls}" d="([^"]*)"`));
    if (!m) return [];
    return m[1].split(' ').filter(Boolean).map((tok) => Number(tok.replace(/^[ML]/, '')))
      .reduce((acc, n, i) => {
        if (i % 2 === 0) acc.push([n]);
        else acc[acc.length - 1].push(n);
        return acc;
      }, []);
  };
  const attr = (svg, tag, name) => {
    const m = svg.match(new RegExp(`<${tag}[^>]*\\b${name}="([^"]*)"`));
    return m ? m[1] : null;
  };

  console.log('nothing to draw draws nothing:');
  {
    check('no points at all', gaugeChartSvg([], [], T0) === '');
    check('one point is not a line', gaugeChartSvg(pts(1, T0, H, () => 5), [], T0) === '');
    // Every point at the same instant gives a zero-width domain, and the x
    // scale would divide by zero.
    check('all points at one instant', gaugeChartSvg(pts(4, T0, 0, (i) => i), [], T0) === '',
      JSON.stringify(gaugeChartSvg(pts(4, T0, 0, (i) => i), [], T0).slice(0, 40)));
    check('two points one instant apart, across the two series',
      gaugeChartSvg([{ t: T0, v: 1 }], [{ t: T0, v: 2 }], T0) === '');
  }

  console.log('\nthe axis starts at zero:');
  {
    const svg = gaugeChartSvg(pts(10, T0, H, (i) => 100 + i), [], T0 + 5 * H);
    check('the bottom tick is 0', /text-anchor="end">0<\/text>/.test(svg));
    // Autoscaling to the observed min would draw the 1.1x wobble of a dry
    // creek with the same full-height swing as a storm.
    check('...even when the data never approaches it',
      svg.includes('>0</text>') && !svg.includes('>100.00</text>'));
    const top = Number(svg.match(/text-anchor="end">([\d.]+)<\/text>/)[1]);
    check('the top tick clears the tallest point', top > 109, String(top));
    check('...by the stated 8% headroom', Math.abs(top - 109 * 1.08) < 0.6,
      `${top} vs ${(109 * 1.08).toFixed(2)}`);
  }

  console.log('\na point sits where its value says:');
  {
    const obs = [{ t: T0, v: 0 }, { t: T0 + 10 * H, v: 50 }];
    const svg = gaugeChartSvg(obs, [], T0 - H);
    const p = pathPoints(svg, 'gc-obs');
    check('two points on the path', p.length === 2, String(p.length));
    check('the first is at the left padding', Math.abs(p[0][0] - GC_PAD_L) < 0.1,
      String(p[0][0]));
    check('the last is at the right edge',
      Math.abs(p[1][0] - (GC_W - GC_PAD_R)) < 0.1, String(p[1][0]));
    check('a zero value sits on the baseline',
      Math.abs(p[0][1] - (GC_H - GC_PAD_B)) < 0.1, String(p[0][1]));
    // The peak is below the top tick by the headroom, never on the frame.
    check('the peak is inside the plot area', p[1][1] > GC_PAD_T, String(p[1][1]));
    check('...and above the midline', p[1][1] < (GC_H - GC_PAD_B) / 2);
    check('every point is within the viewBox',
      p.every(([x, y]) => x >= 0 && x <= GC_W && y >= 0 && y <= GC_H));
  }

  console.log('\na flat-zero series still draws an axis:');
  {
    const svg = gaugeChartSvg(pts(6, T0, H, () => 0), [], T0 + 2 * H);
    check('it draws at all', svg.length > 0);
    check('no NaN reaches the markup', !svg.includes('NaN'), svg.slice(0, 60));
    const p = pathPoints(svg, 'gc-obs');
    check('the trace lies on the baseline',
      p.every(([, y]) => Math.abs(y - (GC_H - GC_PAD_B)) < 0.1));
    // The nominal top is 1, and gaugeNum gives a value of 1 two decimals.
    check('the top tick is the nominal 1', svg.includes('>1.00</text>'),
      svg.match(/text-anchor="end">([^<]*)</)?.[1]);
  }

  console.log('\nthe rule marks now, and only when now is on screen:');
  {
    const obs = pts(10, T0, H, (i) => i + 1);
    const inside = gaugeChartSvg(obs, [], T0 + 5 * H);
    check('now inside the window draws the rule', inside.includes('class="gc-now"'));
    const x = Number(attr(inside, 'line class="gc-now"', 'x1'));
    check('...halfway along, for a midpoint now',
      Math.abs(x - (GC_PAD_L + (GC_W - GC_PAD_L - GC_PAD_R) * 5 / 9)) < 0.2, String(x));
    check('before the window draws none',
      !gaugeChartSvg(obs, [], T0 - H).includes('gc-now'));
    check('after the window draws none',
      !gaugeChartSvg(obs, [], T0 + 100 * H).includes('gc-now'));
    // Exactly on an endpoint is outside, since the rule would land on the
    // frame and read as part of the axis.
    check('on the first point draws none',
      !gaugeChartSvg(obs, [], T0).includes('gc-now'));
    check('on the last point draws none',
      !gaugeChartSvg(obs, [], T0 + 9 * H).includes('gc-now'));
  }

  console.log('\na hole between the series is drawn as absence:');
  {
    const obs = pts(6, T0, H, (i) => i + 1);
    const far = pts(6, T0 + 20 * H, H, () => 9);
    const near = pts(6, T0 + 6 * H, H, () => 9);
    const gapped = gaugeChartSvg(obs, far, T0 + 10 * H);
    check('a long hole gets a band', gapped.includes('class="gc-gap"'));
    check('...labeled, rather than left blank', gapped.includes('>no data</text>'));
    check('...and a note under the chart', gapped.includes('no observations for'));
    check('...naming the hours', /no observations for 15 h/.test(gapped),
      gapped.match(/no observations for \d+ h/)?.[0]);
    const met = gaugeChartSvg(obs, near, T0 + 6 * H);
    check('series that meet get no band', !met.includes('gc-gap'));
    check('...and no note', !met.includes('no observations for'));
    // The band is drawn behind the traces, so a trace crossing it stays
    // readable.
    check('the band precedes the paths',
      gapped.indexOf('gc-gap') < gapped.indexOf('gc-obs'));
  }

  console.log('\nthe gap note agrees with the band:');
  {
    const obs = [{ t: T0, v: 1 }];
    const at = (hours) => gapNote(obs, [{ t: T0 + hours * H, v: 2 }]);
    check('an overlap says nothing', gapNote(obs, [{ t: T0 - H, v: 2 }]) === '');
    check('under 1.5 h says nothing', at(1.4) === '');
    check('at 1.5 h it speaks', at(1.5) !== '', at(1.5));
    check('...rounding to whole hours', /for 2 h/.test(at(1.6)), at(1.6));
    check('no observed points says nothing', gapNote([], [{ t: T0, v: 1 }]) === '');
    check('no forecast points says nothing', gapNote(obs, []) === '');
  }

  console.log('\nidentity is never by color alone:');
  {
    const svg = gaugeChartSvg(pts(4, T0, H, () => 1), pts(4, T0 + 3 * H, H, () => 2),
      T0 + 2 * H);
    check('the legend names both series',
      svg.includes('>observed</span>') && svg.includes('>forecast</span>'));
    check('each point carries its series in a title',
      svg.includes(' observed ') && svg.includes(' forecast '));
    check('the chart names its range for a screen reader',
      /aria-label="observed and forecast flow, [^"]+ to [^"]+"/.test(svg),
      attr(svg, 'svg', 'aria-label'));
  }

  console.log('\na single point gets no path, but still gets a hit target:');
  {
    const svg = gaugeChartSvg(pts(5, T0, H, (i) => i), [{ t: T0 + 2 * H, v: 3 }],
      T0 + H);
    check('the observed trace is drawn', svg.includes('class="gc-obs"'));
    check('a one-point forecast draws no line', !svg.includes('class="gc-fcst"'));
    check('...and is still hoverable',
      (svg.match(/class="gc-hit"/g) ?? []).length === 6,
      String((svg.match(/class="gc-hit"/g) ?? []).length));
  }

  console.log('\nvalues are escaped and formatted:');
  {
    check('markup characters are escaped',
      svgEsc('<a & b">') === '&lt;a &amp; b&quot;&gt;', svgEsc('<a & b">'));
    check('plain text is untouched', svgEsc('20 1430Z') === '20 1430Z');
    check('zero is bare', gaugeNum(0) === '0');
    check('hundreds lose the decimals', gaugeNum(1234.56) === '1235', gaugeNum(1234.56));
    check('ones keep two', gaugeNum(4.5678) === '4.57', gaugeNum(4.5678));
    check('hundredths keep three', gaugeNum(0.04567) === '0.046', gaugeNum(0.04567));
    check('below a hundredth goes exponential',
      gaugeNum(0.000123) === '1.2e-4', gaugeNum(0.000123));
    check('negatives follow the same bands',
      gaugeNum(-250) === '-250' && gaugeNum(-4.5) === '-4.50');
    check('the label is UTC day and time',
      hhmm(Date.UTC(2026, 10, 9, 7, 5)) === '09 0705Z',
      hhmm(Date.UTC(2026, 10, 9, 7, 5)));
  }

  console.log('\nthe popup summary skips the COMID echo:');
  {
    check('no payload says so', gaugeSummaryHtml(null).includes('no data'));
    check('headers with only the echo says so',
      gaugeSummaryHtml({ headers: ['COMID'] }).includes('no data'));
    const s = gaugeSummaryHtml({ headers: ['COMID', 'flow', 'note'], values: [1, 4.5678, 'ok'] });
    check('the echo column is dropped', !s.includes('COMID'), s);
    check('numbers go to 2dp', s.includes('4.57'), s);
    check('strings pass through', s.includes('ok'));
    check('a short values array stops early',
      !gaugeSummaryHtml({ headers: ['COMID', 'a', 'b'], values: [1, 2] }).includes('b:'));
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall gauge-chart assertions passed');
  return failures.length ? 1 : 0;
}
