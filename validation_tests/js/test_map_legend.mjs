// Legend bar geometry.
//
// Run:  node validation_tests/js/run_map_legend.mjs
//
// The legend is the only thing on screen that says what the colors mean, so a
// bar drawn wrong is a bar that teaches the reader the wrong scale. The two
// failures worth guarding are a bar that labels neither end, which can be
// misread by a factor of ten, and labels that overlap, which turn a number
// into a smear.
//
// The spacing invariant is checked by sweeping bar lengths rather than by
// asserting one layout, because the collision only appears at the length
// where a step stops fitting.

export function runTests(mod, ramps) {
  const {
    rampKind, medianAdjacentStep, fractionFor, legendBlocks, legendGradient,
    niceStep, stepDecimals, legendTicks, ticksAtStep, fitTicks, fitsLabels,
    barLayout,
    DISCRETE_MEDIAN_STEP
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const refl = ramps['flow-reflectivity'];
  const vel = ramps['flow-velocity'];
  const zdr = ramps['flow-differential-reflectivity'];
  const all = Object.entries(ramps);
  const threw = (fn, kind = RangeError) => {
    try { fn(); return false; } catch (e) { return e instanceof kind; }
  };

  console.log('a block bar and a gradient bar are told apart:');
  {
    // Every reflectivity bar is painted in blocks and every other moment is
    // a sampled gradient, on both deployments.
    for (const [name, ramp] of all) {
      const want = name.endsWith('-reflectivity')
        && !name.includes('differential') ? 'discrete' : 'continuous';
      check(`${name} is ${want}`, rampKind(ramp) === want, rampKind(ramp));
    }
  }

  console.log('\nthe threshold keeps headroom on both sides:');
  {
    // The regression this guards: a threshold that drifts into the band the
    // sampled ramps occupy would draw a gradient as 61 blocks, or
    // reflectivity's 22 painted bands as one smooth wash.
    for (const [name, ramp] of all) {
      const m = medianAdjacentStep(ramp);
      const discrete = name.endsWith('-reflectivity')
        && !name.includes('differential');
      check(`${name} median ${m} is ${discrete ? 'above' : 'below'} ${DISCRETE_MEDIAN_STEP}`,
        discrete ? m >= DISCRETE_MEDIAN_STEP : m < DISCRETE_MEDIAN_STEP,
        String(m));
    }
    check('a single stop has no step', medianAdjacentStep(
      { ...refl, stops: [refl.stops[0]] }) === 0);
    check('no stops is refused',
      threw(() => medianAdjacentStep({ ...refl, stops: [] })));
  }

  console.log('\na value places itself on the bar:');
  {
    check('the minimum is at the start', fractionFor(refl, refl.min) === 0);
    check('the maximum is at the end', fractionFor(refl, refl.max) === 1);
    check('the middle is halfway',
      Math.abs(fractionFor(refl, 25) - 0.5) < 1e-12,
      String(fractionFor(refl, 25)));
    check('below the ramp clamps to the start', fractionFor(refl, -500) === 0);
    check('above it clamps to the end', fractionFor(refl, 500) === 1);
    check('NaN is refused', threw(() => fractionFor(refl, NaN)));
    check('infinity is refused', threw(() => fractionFor(refl, Infinity)));
    check('a zero-span ramp is refused',
      threw(() => fractionFor({ ...refl, min: 5, max: 5 }, 5)));
    check('an inverted ramp is refused',
      threw(() => fractionFor({ ...refl, min: 80, max: -30 }, 0)));
  }

  console.log('\nthe blocks of a discrete bar cover it end to end:');
  {
    const b = legendBlocks(refl);
    check('one block per stop', b.length === refl.stops.length,
      `${b.length}/${refl.stops.length}`);
    check('the first starts at the start', b[0].from === 0, String(b[0].from));
    check('the last ends at the end', b[b.length - 1].to === 1,
      String(b[b.length - 1].to));
    check('each block has width', b.every((x) => x.to > x.from));
    check('they ascend', b.every((x, i) => i === 0 || x.from >= b[i - 1].from));
    const gaps = b.filter((x, i) => i > 0 && Math.abs(x.from - b[i - 1].to) > 1e-9);
    check('they leave no gap', gaps.length === 0, `${gaps.length} gaps`);
    check('each carries its own bounds',
      b.every((x, i) => x.lo === refl.stops[i].lo && x.hi === refl.stops[i].hi));
  }

  console.log('\nthe gradient stops sit where they were read:');
  {
    const g = legendGradient(vel);
    check('one stop per ramp stop', g.length === vel.stops.length,
      `${g.length}/${vel.stops.length}`);
    check('offsets ascend',
      g.every((x, i) => i === 0 || x.offset >= g[i - 1].offset));
    check('offsets stay on the bar',
      g.every((x) => x.offset >= 0 && x.offset <= 1));
    check('colors come through unchanged',
      g.every((x, i) => x.color === vel.stops[i].color));
  }

  console.log('\nsteps round to the 1-2-5 sequence:');
  {
    const cases = [[0.3, 0.5], [0.6, 1], [1, 1], [1.1, 2], [2, 2], [2.1, 5],
      [5, 5], [5.1, 10], [7, 10], [10, 10], [11, 20], [15.7, 20],
      [0.04, 0.05], [110, 200], [250, 500]];
    for (const [raw, want] of cases) {
      const got = niceStep(raw);
      check(`${raw} rounds to ${want}`, Math.abs(got - want) < want * 1e-9,
        String(got));
    }
    check('zero is refused', threw(() => niceStep(0)));
    check('a negative step is refused', threw(() => niceStep(-5)));
    check('NaN is refused', threw(() => niceStep(NaN)));
  }

  console.log('\nthe label precision follows the step:');
  {
    for (const [step, want] of [[100, 0], [10, 0], [1, 0], [0.5, 1],
      [0.2, 1], [0.1, 1], [0.05, 2], [0.01, 2], [0.002, 3]]) {
      check(`a step of ${step} takes ${want} decimals`,
        stepDecimals(step) === want, String(stepDecimals(step)));
    }
    check('zero is refused', threw(() => stepDecimals(0)));
  }

  console.log('\nboth ends are always labeled:');
  {
    // A bar whose range the reader infers from interior ticks can be misread
    // by a factor of ten, so the ends are never dropped for room.
    for (const [name, ramp] of all) {
      for (const len of [40, 120, 400]) {
        const t = legendTicks(ramp, len, 16);
        const ok = t[0].value === ramp.min
          && t[t.length - 1].value === ramp.max
          && t[0].at === 0 && t[t.length - 1].at === 1;
        check(`${name} at ${len}px labels both ends`, ok,
          `${t[0].label} .. ${t[t.length - 1].label}`);
      }
    }
    const tiny = legendTicks(refl, 10, 16);
    check('a bar with room for nothing still shows its ends',
      tiny.length === 2, `${tiny.length} ticks`);
  }

  console.log('\nno two labels ever crowd each other:');
  {
    // The invariant, swept rather than sampled: the collision appears only at
    // the length where one more step stops fitting.
    let worst = Infinity;
    let worstAt = '';
    let violations = 0;
    for (const [name, ramp] of all) {
      for (let len = 24; len <= 900; len += 3) {
        for (const spacing of [12, 16, 22, 30]) {
          const t = legendTicks(ramp, len, spacing);
          for (let i = 1; i < t.length; i++) {
            const px = (t[i].at - t[i - 1].at) * len;
            // Two ends on a bar shorter than one label's room is the one
            // case that cannot be satisfied, and the ends win there.
            if (t.length === 2) continue;
            if (px / spacing < worst) {
              worst = px / spacing;
              worstAt = `${name} len=${len} spacing=${spacing} ${t[i - 1].label}->${t[i].label}`;
            }
            if (px + 1e-9 < spacing) violations++;
          }
        }
      }
    }
    check('every adjacent pair clears the spacing', violations === 0,
      `${violations} violations, tightest ${worst.toFixed(3)} of spacing at ${worstAt}`);
  }

  console.log('\nticks ascend, do not repeat, and stay on the bar:');
  {
    for (const [name, ramp] of all) {
      const t = legendTicks(ramp, 300, 16);
      check(`${name} ascends`,
        t.every((x, i) => i === 0 || x.value > t[i - 1].value));
      check(`${name} has no repeated label`,
        new Set(t.map((x) => x.label)).size === t.length,
        t.map((x) => x.label).join(' '));
      check(`${name} stays within the bar`,
        t.every((x) => x.at >= 0 && x.at <= 1));
      check(`${name} labels match their values`,
        t.every((x) => Math.abs(Number(x.label) - x.value) < 1e-9),
        t.map((x) => x.label).join(' '));
    }
  }

  console.log('\ninterior ticks land on round values:');
  {
    const t = legendTicks(refl, 300, 16);
    const interior = t.slice(1, -1);
    check('reflectivity ticks are multiples of their step',
      interior.every((x) => Math.abs(x.value % 10) < 1e-9
        || Math.abs(x.value % 20) < 1e-9),
      interior.map((x) => x.label).join(' '));
    const z = legendTicks(zdr, 300, 16).slice(1, -1);
    check('a 6 dB span gets sub-unit ticks without drift',
      z.every((x) => Math.abs(x.value * 10 - Math.round(x.value * 10)) < 1e-9),
      z.map((x) => x.label).join(' '));
  }

  console.log('\nthe fitted ticking never overlaps its own labels:');
  {
    // fitTicks exists because choosing a step from a pixel spacing and then
    // formatting the labels is circular. The invariant is checked against the
    // labels it actually produced, swept over bar lengths, so a step that
    // fits at one width and not the next cannot slip through.
    let violations = 0;
    let finest = '';
    let widest = 0;
    for (const [name, ramp] of all) {
      for (let len = 30; len <= 900; len += 2) {
        for (const charPx of [5.1, 5.7, 6.3]) {
          const t = fitTicks(ramp, len, charPx, 1);
          if (!fitsLabels(t, len, charPx, 1) && t.length > 2) violations++;
          if (t.length > widest) {
            widest = t.length;
            finest = `${name} len=${len} char=${charPx} ${t.length} ticks`;
          }
        }
      }
    }
    check('no fitted tick set overlaps', violations === 0,
      `${violations} violations; densest was ${finest}`);
  }

  console.log('\nfitting gives more ticks as the bar grows:');
  {
    for (const [name, ramp] of all) {
      const counts = [60, 150, 300, 600, 900]
        .map((len) => fitTicks(ramp, len, 5.7, 1).length);
      const rising = counts.every((n, i) => i === 0 || n >= counts[i - 1]);
      check(`${name} never loses ticks on a longer bar`, rising,
        counts.join(' '));
      check(`${name} labels its ends at every length`,
        [60, 150, 300, 900].every((len) => {
          const t = fitTicks(ramp, len, 5.7, 1);
          return t[0].value === ramp.min
            && t[t.length - 1].value === ramp.max;
        }));
    }
    const tiny = fitTicks(refl, 30, 5.7, 1);
    check('a bar with room for nothing keeps its two ends',
      tiny.length === 2, `${tiny.length} ticks`);
  }

  console.log('\nlabel boxes are measured as they are drawn:');
  {
    // The end labels sit inward from the bar's ends. Centering them would
    // hang half of each off the bar, and the fit test has to agree with the
    // drawing or it is checking a different picture.
    // Half of '0' plus half of '100' is 20 px at a 10 px character, and one
    // character of gap makes 30.
    const t = [
      { value: 0, at: 0, label: '0' },
      { value: 5, at: 1, label: '100' }
    ];
    check('two ends on a bar exactly wide enough fit',
      fitsLabels(t, 30, 10, 1));
    check('the same pair one pixel narrower does not',
      !fitsLabels(t, 29, 10, 1));
    check('a zero gap still refuses an overlap',
      !fitsLabels(t, 19, 10, 0));
    check('and allows a touch at exactly the width',
      fitsLabels(t, 20, 10, 0));
    check('a single tick always fits',
      fitsLabels([t[0]], 10, 10, 1));
    check('no ticks always fits', fitsLabels([], 10, 10, 1));
  }

  console.log('\nthe bar reserves room for its end labels:');
  {
    // The two passes exist because the padding depends on the end labels and
    // those depend on the step the remaining width allows.
    for (const [name, ramp] of all) {
      for (const width of [140, 182, 300, 600]) {
        const L = barLayout(ramp, width, 5.853, 1);
        check(`${name} at ${width}px: the bar plus both pads is the width`,
          Math.abs(L.barPx + 2 * L.padPx - width) < 1e-9,
          `${L.barPx.toFixed(2)} + 2*${L.padPx.toFixed(2)}`);
        check(`${name} at ${width}px: its labels clear each other`,
          fitsLabels(L.ticks, L.barPx, 5.853, 1),
          L.ticks.map((t) => t.label).join(' '));
        const half = Math.max(L.ticks[0].label.length,
          L.ticks[L.ticks.length - 1].label.length) * 5.853 / 2;
        check(`${name} at ${width}px: the pad holds half an end label`,
          L.padPx >= half - 1e-9, `${L.padPx.toFixed(2)} vs ${half.toFixed(2)}`);
      }
    }
    // The measured case this was built for.
    const vel182 = barLayout(ramps['aqpi-xband-velocity'], 182, 5.853, 1);
    check('a 182px velocity bar now ticks every 10 m/s',
      vel182.ticks.length === 7,
      vel182.ticks.map((t) => t.label).join(' '));
    const refl182 = barLayout(ramps['aqpi-xband-reflectivity'], 182, 5.853, 1);
    check('a 182px reflectivity bar still ticks every 20 dBZ',
      refl182.ticks.length === 6,
      refl182.ticks.map((t) => t.label).join(' '));
  }

  console.log('\nbad measurements are refused rather than drawn:');
  {
    check('a zero-length bar', threw(() => legendTicks(refl, 0, 16)));
    check('a negative length', threw(() => legendTicks(refl, -300, 16)));
    check('NaN length', threw(() => legendTicks(refl, NaN, 16)));
    check('zero spacing', threw(() => legendTicks(refl, 300, 0)));
    check('negative spacing', threw(() => legendTicks(refl, 300, -16)));
    check('infinite spacing', threw(() => legendTicks(refl, 300, Infinity)));
    check('a zero-span ramp',
      threw(() => legendTicks({ ...refl, min: 5, max: 5 }, 300, 16)));
    check('fitTicks refuses a zero bar', threw(() => fitTicks(refl, 0, 6, 1)));
    check('fitTicks refuses a zero character width',
      threw(() => fitTicks(refl, 300, 0, 1)));
    check('fitTicks refuses a negative gap',
      threw(() => fitTicks(refl, 300, 6, -1)));
    check('fitTicks refuses NaN anywhere',
      threw(() => fitTicks(refl, 300, NaN, 1)));
    check('barLayout refuses a zero width', threw(() => barLayout(refl, 0, 6, 1)));
    check('barLayout refuses a width with no room for a bar',
      threw(() => barLayout(refl, 8, 6, 1)));
  }

  console.log('\na step that wants too many ticks is refused:');
  {
    // The bound exists because fitTicks once walked from the finest 1-2-5
    // step upward, and a step of 1e-6 across differential phase asked for 360
    // million ticks. It filled memory instead of failing.
    const phase = ramps['flow-differential-phase'];
    check('one micro-unit across 360 degrees is refused',
      threw(() => ticksAtStep(phase, 1e-6, 0)));
    check('the limit itself is allowed',
      ticksAtStep(phase, (phase.max - phase.min) / mod.MAX_TICKS, 0).length > 2);
    check('fitTicks never asks for more than the bar can hold',
      fitTicks(phase, 3840, 1, 0).length <= mod.MAX_TICKS,
      String(fitTicks(phase, 3840, 1, 0).length));
    check('a coarse step is still fine', ticksAtStep(phase, 90, 0).length === 5,
      String(ticksAtStep(phase, 90, 0).length));
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall legend assertions passed');
  return failures.length ? 1 : 0;
}
