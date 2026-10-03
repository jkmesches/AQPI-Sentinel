/**
 * Sparkline geometry — the arithmetic that decides whether an operator can
 * tell a healthy feed from a dead one.
 *
 * Driven by run_sparkline_geometry.mjs (that wrapper compiles the TS with
 * esbuild first, then imports this), so these assertions run against the real
 * module the component imports.
 *
 * The bug being guarded: with no fixed domain, every trace is rescaled to its
 * own observed min/max. A perfectly steady series and a series with no samples
 * then land on the SAME baseline — identical pixel for pixel — and a 2% wobble
 * draws the same full-height zigzag as a 6x swing. Those three facts are
 * asserted below against the autoscale path, so if someone ever makes the
 * fixed path behave that way again the test says which property broke.
 */
const H = 16;
const W = 72;
const WIN = 60 * 60 * 1000;      // 1 h window
const NOW = 1_800_000_000_000;
const CADENCE = 120;
// floor(windowMs / max(15s, cadence)) clamped to [8, 48]. Asserted below, so
// if the bucketing changes these fixtures fail loudly rather than quietly
// leaving buckets empty — an empty bucket draws at the baseline and would
// make a "flat" fixture look like noise.
const NB = 30;

// Exactly one sample per bucket: ts = left + (i + 0.5) * bucketMs lands
// squarely in bucket i, so each bucket's mean is the value itself and the
// fixture's shape survives bucketing intact.
const series = (values) =>
  values.map((v, i) => ({ ts: NOW - WIN + ((i + 0.5) * WIN) / values.length, value: v }));
const fill = (pattern) => Array.from({ length: NB }, (_, i) => pattern[i % pattern.length]);
const rep = (v, n = NB) => series(new Array(n).fill(v));

export function runTests(mod) {
  const { sparklineGeometry } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`);
    if (!cond) failures.push(label);
  };
  const geo = (data, domain = null, warnAt = null) =>
    sparklineGeometry({ data, now: NOW, windowMs: WIN, cadenceS: CADENCE,
                        width: W, height: H, domain, warnAt });

  const FLOOR = H - 1;        // 15 — where an empty bucket draws
  const CEIL  = 1;            // 1  — a full box

  check('the window buckets as the fixtures assume',
        geo(rep(0.5)).ys.length === NB, `${geo(rep(0.5)).ys.length} buckets`);

  // ---- the defect, pinned ------------------------------------------------
  const flatAuto = geo(rep(0.9));
  const deadAuto = geo([]);
  check('autoscale: a steady series collapses to the floor',
        flatAuto.ys.every((y) => y === FLOOR), `ys[0]=${flatAuto.ys[0]}`);
  check('autoscale: ...which is exactly where a dead one draws',
        JSON.stringify(flatAuto.ys) === JSON.stringify(deadAuto.ys),
        'steady and dead are pixel-identical');

  const wobbleAuto = geo(series(fill([0.11, 0.12, 0.11, 0.13])));
  const swingAuto  = geo(series(fill([0.1, 0.9, 0.15, 0.85])));
  const amp = (g) => Math.max(...g.ys) - Math.min(...g.ys);
  check('autoscale: a 2% wobble is amplified to the same height as a 6x swing',
        Math.abs(amp(wobbleAuto) - amp(swingAuto)) < 0.01,
        `wobble=${amp(wobbleAuto).toFixed(1)}px swing=${amp(swingAuto).toFixed(1)}px`);

  // ---- the fix -----------------------------------------------------------
  const D = [0, 1];
  const flatFixed = geo(rep(0.9), D);
  const deadFixed = geo([], D);
  check('fixed: a steady healthy series sits high, not on the floor',
        flatFixed.ys.every((y) => y < 3), `ys[0]=${flatFixed.ys[0].toFixed(1)}`);
  check('fixed: ...and is no longer confusable with a dead one',
        deadFixed.ys.every((y) => y === FLOOR) &&
        flatFixed.ys[0] !== deadFixed.ys[0]);
  check('fixed: a steady series is still FLAT (height, not noise)',
        amp(flatFixed) === 0);

  const wobbleFixed = geo(series(fill([0.11, 0.12, 0.11, 0.13])), D);
  const swingFixed  = geo(series(fill([0.1, 0.9, 0.15, 0.85])), D);
  check('fixed: the 2% wobble now draws much smaller than the 6x swing',
        amp(wobbleFixed) < amp(swingFixed) / 5,
        `wobble=${amp(wobbleFixed).toFixed(1)}px swing=${amp(swingFixed).toFixed(1)}px`);
  check('fixed: a near-silent feed sits LOW even though it is steady',
        wobbleFixed.ys.every((y) => y > H * 0.7),
        `ys[0]=${wobbleFixed.ys[0].toFixed(1)}`);

  // ---- the ends of the axis are absolute ---------------------------------
  check('fixed: headroom 1.0 reaches the ceiling',
        geo(rep(1), D).ys.every((y) => y === CEIL));
  check('fixed: headroom 0.0 reaches the floor',
        geo(rep(0), D).ys.every((y) => y === FLOOR));
  check('fixed: out-of-range values clamp rather than overshoot',
        geo(rep(1.4), D).ys.every((y) => y === CEIL) &&
        geo(rep(-0.3), D).ys.every((y) => y === FLOOR));
  // Two different windows of the same metric must be comparable — the whole
  // reason for a fixed axis. Under autoscale both of these fill the box.
  check('fixed: a healthy window and a degraded one draw at different heights',
        geo(rep(0.9), D).ys[0] < geo(rep(0.3), D).ys[0],
        `${geo(rep(0.9), D).ys[0].toFixed(1)} vs ${geo(rep(0.3), D).ys[0].toFixed(1)}`);

  // ---- the warn rule -----------------------------------------------------
  check('warn rule is placed on the fixed axis, not on the data',
        geo(rep(0.9), D, 0.2).warnY === (H - 0.2 * (H - 2) - 1).toFixed(1),
        `warnY=${geo(rep(0.9), D, 0.2).warnY}`);
  check('warn rule is suppressed without a domain (it would wander)',
        geo(rep(0.9), null, 0.2).warnY === null);
  check('no warn rule when none was asked for', geo(rep(0.9), D).warnY === null);

  // ---- things that must not have changed ---------------------------------
  check('the sample count still reflects what landed in the window',
        geo(rep(0.9, 24), D).total === 24);
  check('samples outside the window are excluded',
        geo([{ ts: NOW - 2 * WIN, value: 0.5 }, { ts: NOW - WIN / 2, value: 0.5 }], D)
          .total === 1);
  check('an empty series still produces a drawable baseline path',
        /^M0(\.0)?,/.test(deadFixed.strokeD) && deadFixed.fillD.endsWith('Z'));

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall sparkline-geometry assertions passed');
  return failures.length ? 1 : 0;
}
