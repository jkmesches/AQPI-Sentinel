// What a dashboard row says beside its sparkline.
//
// Run:  node validation_tests/js/run_dashboard_readouts.mjs
//
// The rails used to carry a trace with no number on it, so a row said that
// something moved without saying what it is, and an age could not be judged
// without knowing how often the subject is sampled. 55 minutes old is a fault
// on a 2-minute radar and routine on a 30-minute forecast.
//
// The image-QC verdicts are read out of the check's summary line, because the
// rollup carries no payload. That makes the format a contract between
// layer4_image and this parser, and these assertions are where it is written
// down.

export function runTests(mod) {
  const {
    readoutMetric, sparklineMetric, fmtCadence, secondsToNextRun,
    parseImageQc, qcLabel
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  console.log('every rail row asks for a number, not only a shape:');
  {
    // The two radar stages spell the age differently. Asking for the wrong
    // one is silent: the series comes back empty and the cell reads "—".
    check('L2 reads radarca\'s newest scan time',
      readoutMetric('layer2.radar.XSCV') === 'primary_age_s',
      String(readoutMetric('layer2.radar.XSCV')));
    check('LB2 reads the newest volume on disk',
      readoutMetric('layer2.backend.XSCV') === 'age_s',
      String(readoutMetric('layer2.backend.XSCV')));
    check('products keep their age', readoutMetric('layer1.product.qpe_1hr') === 'age_s');
    check('backend products too', readoutMetric('layer1.backend.qpe_1hr') === 'age_s');
    check('image QC reads its self-similarity',
      readoutMetric('layer4.xband.XSCV') === 'autocorr',
      String(readoutMetric('layer4.xband.XSCV')));
    check('...and plots its echo coverage',
      sparklineMetric('layer4.xband.XSCV') === 'coverage_pct',
      String(sparklineMetric('layer4.xband.XSCV')));
    check('a mosaic QC row is the same',
      readoutMetric('layer4.mosaic.comp_ref') === 'autocorr'
      && sparklineMetric('layer4.mosaic.comp_ref') === 'coverage_pct');
    // The two are fetched as a pair, so a check that plots one and reads the
    // other must not collapse to a single series.
    check('the trace and the number are different series',
      sparklineMetric('layer4.xband.XSCV') !== readoutMetric('layer4.xband.XSCV'));
    check('an L0 row asks for neither',
      sparklineMetric('layer0.net.dns') === null
      && readoutMetric('layer0.net.dns') === null);
  }

  console.log('\na cadence says whether an age is late or merely slow:');
  {
    check('seconds stay seconds', fmtCadence(30) === '30s', fmtCadence(30));
    check('two minutes', fmtCadence(120) === '2m', fmtCadence(120));
    check('ten minutes', fmtCadence(600) === '10m', fmtCadence(600));
    check('a whole hour is an hour, not 60m',
      fmtCadence(3600) === '1h', fmtCadence(3600));
    check('six hours', fmtCadence(21600) === '6h', fmtCadence(21600));
    check('an odd span keeps one decimal rather than lying',
      fmtCadence(5400) === '1.5h', fmtCadence(5400));
    for (const bad of [0, -5, null, undefined, NaN, Infinity]) {
      check(`${String(bad)} gives no token`, fmtCadence(bad) === '',
        JSON.stringify(fmtCadence(bad)));
    }
  }

  console.log('\nthe next run is counted from the last finish:');
  {
    const now = new Date('2026-11-22T05:00:00Z');
    const t = (s) => new Date(now.getTime() + s * 1000).toISOString();
    check('a fresh run has its whole cadence left',
      secondsToNextRun(t(0), 120, now) === 120,
      String(secondsToNextRun(t(0), 120, now)));
    check('half a cadence in, half is left',
      secondsToNextRun(t(-60), 120, now) === 60,
      String(secondsToNextRun(t(-60), 120, now)));
    // Overdue is the reading that matters: a check past its cadence is slow
    // or stuck, and both look like a stale row from the age alone.
    check('an overdue run reads negative',
      secondsToNextRun(t(-300), 120, now) === -180,
      String(secondsToNextRun(t(-300), 120, now)));
    check('no finish time gives nothing',
      secondsToNextRun(null, 120, now) === null);
    check('no cadence gives nothing',
      secondsToNextRun(t(0), null, now) === null);
    check('a zero cadence gives nothing',
      secondsToNextRun(t(0), 0, now) === null);
    check('an unparsable stamp gives nothing',
      secondsToNextRun('not a time', 120, now) === null);
  }

  console.log('\nimage QC is read out of the summary the check writes:');
  {
    // Verbatim from layer4.xband.XSCV and layer4.mosaic.comp_now on
    // 2026-11-22. A change to either side breaks this on purpose.
    const clean = 'cov=33.623% autocorr=0.9528  ext=OK  spk=OK  ring=N/A  frozen=OK';
    const qualified = 'cov=2.434% autocorr=0.8735  ext=OK_LOW_COV  spk=OK  ring=OK  frozen=OK';
    const sameFrame = 'cov=14.664% autocorr=0.985  ext=OK  spk=OK  ring=N/A  frozen=OK_SAME_FRAME';

    const v = parseImageQc(clean);
    check('every detector is found', v.length === 4, String(v.length));
    check('...in the order the check writes them',
      v.map((x) => x.name).join(' ') === 'ext spk ring frozen',
      v.map((x) => x.name).join(' '));
    check('a clean line is all OK', v.every((x) => x.ok));
    check('N/A is not a failure',
      v.find((x) => x.name === 'ring').ok, 'ring=N/A');
    check('...and it is a clean row', qcLabel(v) === 'OK', qcLabel(v));

    // The check qualifies a pass rather than demoting it, and a parser that
    // matched only the bare word would turn every qualified pass into a
    // fault on a healthy deployment.
    check('OK_LOW_COV still passes',
      parseImageQc(qualified).every((x) => x.ok), qcLabel(parseImageQc(qualified)));
    check('OK_SAME_FRAME still passes',
      parseImageQc(sameFrame).every((x) => x.ok), qcLabel(parseImageQc(sameFrame)));

    const broken = 'cov=0.1% autocorr=0.99  ext=LOW_COV  spk=SPIKES  ring=OK  frozen=FROZEN';
    const bv = parseImageQc(broken);
    check('a real fault is not OK', !bv.every((x) => x.ok));
    check('...and the label names which detectors fired',
      qcLabel(bv) === 'ext spk frozen', qcLabel(bv));
    check('...leaving out the one that passed', !qcLabel(bv).includes('ring'));
  }

  console.log('\nnothing in a summary is assumed to be there:');
  {
    check('an empty summary yields nothing', parseImageQc('').length === 0);
    check('null yields nothing', parseImageQc(null).length === 0);
    check('undefined yields nothing', parseImageQc(undefined).length === 0);
    check('a summary from another stage yields nothing',
      parseImageQc('resolver ok · google=ok cloudflare=ok').length === 0,
      JSON.stringify(parseImageQc('resolver ok · google=ok cloudflare=ok')));
    check('no verdicts reads as a dash, not as a pass',
      qcLabel([]) === '—', qcLabel([]));
    // A partial line must give back what it has rather than nothing.
    const partial = parseImageQc('cov=5% ext=OK frozen=FROZEN');
    check('a line missing detectors returns the ones present',
      partial.map((x) => x.name).join(' ') === 'ext frozen',
      partial.map((x) => x.name).join(' '));
    check('...and still names the fault', qcLabel(partial) === 'frozen',
      qcLabel(partial));
    // `spk` must not match inside another token, or a summary mentioning
    // "spike" in prose would invent a verdict.
    check('a word that merely contains a detector name is not a verdict',
      parseImageQc('no spikes seen, ring=OK').map((x) => x.name).join(' ') === 'ring',
      parseImageQc('no spikes seen, ring=OK').map((x) => x.name).join(' '));
  }

  if (failures.length) {
    console.log(`\n${failures.length} FAILED: ${failures.join(', ')}`);
    return 1;
  }
  console.log('\nall dashboard readout assertions passed');
  return 0;
}
