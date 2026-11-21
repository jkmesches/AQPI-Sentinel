// Where each frame sits on the time strip, and where the data is missing.
//
// Run:  node validation_tests/js/run_map_time_axis.mjs
//
// The strip used to space frames evenly, so a product that published every
// two minutes and then stopped for three hours drew that gap as one slot the
// same width as every other. The control that picks the frame was asserting
// that the record was continuous. This places each frame at its own time.
//
// Three failures follow from that and most of what is here is about them.
//
// A bar must not grow to fill a gap. Drawn from one frame to the next, a
// three-hour hole becomes a three-hour bar at the last frame's activity,
// which says the weather held rather than that nothing was published.
//
// A bad time must not lose a frame. The times come from an upstream that has
// served '', nulls and non-ISO strings, and an unsorted or equal-valued list
// produces negative or zero widths, which draw nothing at all. Every one of
// those falls back to even spacing instead.
//
// A click must land on a frame. Inside a gap there is no frame at that
// fraction, so the nearest one has to win.

const MINUTE = 60_000;

// A steady two-minute series, the common case.
const steady = (n, startMs = Date.UTC(2026, 10, 21, 0, 0, 0)) =>
  Array.from({ length: n }, (_, i) =>
    new Date(startMs + i * 2 * MINUTE).toISOString());

export function runTests(mod) {
  const {
    buildAxis, parseTimes, medianInterval, nearestStep, gapLabel,
    GAP_FACTOR, MIN_BAR_FRACTION
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const close = (a, b, eps = 1e-9) => Math.abs(a - b) <= eps;

  console.log('a steady series reads as one, with no gaps:');
  {
    const axis = buildAxis(steady(31));
    check('it is measured, not a fallback', axis.even === false);
    check('the cadence is the publish interval',
      axis.cadenceMs === 2 * MINUTE, String(axis.cadenceMs));
    check('no gaps', axis.gaps.length === 0, String(axis.gaps.length));
    check('the first frame is at the left', close(axis.positions[0], 0));
    check('the last is at the right', close(axis.positions[30], 1));
    check('the frames are evenly placed, because the data is even',
      axis.positions.every((p, i) => close(p, i / 30)));
    check('one bar per frame', axis.bars.length === 31);
    check('every bar has the same width',
      new Set(axis.bars.map((b) => b.width.toFixed(9))).size === 1,
      String(axis.bars[0].width));
    check('the bars tile the axis without overlapping',
      axis.bars.every((b, i) =>
        i === 0 || b.start >= axis.bars[i - 1].start + axis.bars[i - 1].width
          - 1e-9));
  }

  console.log('\na publisher that stopped shows the hole:');
  {
    // Twelve frames two minutes apart, then nothing for three hours, then
    // eight more. Exactly the shape of the CoSMoS outage on 2026-11-21.
    const base = Date.UTC(2026, 10, 21, 0, 0, 0);
    const times = [
      ...Array.from({ length: 12 }, (_, i) =>
        new Date(base + i * 2 * MINUTE).toISOString()),
      ...Array.from({ length: 8 }, (_, i) =>
        new Date(base + 11 * 2 * MINUTE + 180 * MINUTE + i * 2 * MINUTE)
          .toISOString())
    ];
    const axis = buildAxis(times);
    check('the gap is found', axis.gaps.length === 1,
      String(axis.gaps.length));
    const gap = axis.gaps[0];
    check('it sits after the last frame before it', gap.after === 11,
      String(gap.after));
    check('its length is the silence, not the whole interval',
      close(gap.ms, (180 - 2) * MINUTE), gapLabel(gap.ms));
    check('it covers most of the axis, because it was most of the time',
      gap.width > 0.7, gap.width.toFixed(3));
    check('no bar grows into it',
      axis.bars.every((b) => b.width <= axis.cadenceMs
        / (Date.parse(times[times.length - 1]) - Date.parse(times[0])) + 1e-9),
      String(Math.max(...axis.bars.map((b) => b.width))));
    check('the bar before the gap stops at the cadence',
      close(axis.bars[11].start + axis.bars[11].width, gap.start));
    check('the gap ends where the next frame starts',
      close(gap.start + gap.width, axis.positions[12]));
    check('the cadence is still two minutes, since the median ignores it',
      axis.cadenceMs === 2 * MINUTE, String(axis.cadenceMs));
  }

  console.log('\nan ordinary late frame is not a gap:');
  {
    const base = Date.UTC(2026, 10, 21, 0, 0, 0);
    // One interval of 2.5 minutes against a 2-minute cadence: 1.25x, under
    // the factor. A strip that flagged this would flag every day.
    const times = [0, 2, 4.5, 6.5, 8.5, 10.5, 12.5, 14.5]
      .map((m) => new Date(base + m * MINUTE).toISOString());
    const axis = buildAxis(times);
    check(`1.25x the cadence is not a gap at ${GAP_FACTOR}x`,
      axis.gaps.length === 0, String(axis.gaps.length));
    const wide = buildAxis(times, 1.1);
    check('...and a tighter factor does flag it', wide.gaps.length === 1);
  }

  console.log('\nnothing unreadable loses a frame:');
  {
    const bad = [
      ['a null in the list', [...steady(3), null]],
      ['an empty string', [...steady(3), '']],
      ['whitespace', [...steady(3), '   ']],
      ['a non-date string', [...steady(3), 'soon']],
      ['undefined', [...steady(3), undefined]],
      ['a number', [...steady(3), 12345]],
      ['every time the same', Array(5).fill(steady(1)[0])],
      ['out of order', [...steady(3)].reverse()]
    ];
    for (const [name, times] of bad) {
      const axis = buildAxis(times);
      check(`${name} falls back to even spacing`, axis.even === true);
      check(`...and keeps all ${times.length} frames`,
        axis.bars.length === times.length, String(axis.bars.length));
      check('...with no negative or zero width',
        axis.bars.every((b) => b.width > 0),
        String(Math.min(...axis.bars.map((b) => b.width))));
      check('...and no bar running off the end',
        axis.bars.every((b) => b.start + b.width <= 1 + 1e-9),
        String(Math.max(...axis.bars.map((b) => b.start + b.width))));
    }
  }

  console.log('\nthe degenerate lists do not throw:');
  {
    const none = buildAxis([]);
    check('no steps gives no bars', none.bars.length === 0);
    check('...and no positions', none.positions.length === 0);
    const one = buildAxis(steady(1));
    check('one step gives one bar', one.bars.length === 1);
    check('...which covers the axis', close(one.bars[0].width, 1));
    check('...at the left edge', close(one.positions[0], 0));
    const two = buildAxis(steady(2));
    check('two steps span the axis', close(two.positions[1], 1));
    check('every bar clears the minimum width',
      buildAxis(steady(500)).bars.every((b) => b.width >= MIN_BAR_FRACTION));
  }

  console.log('\nthe median ignores the outage it exists to find:');
  {
    check('an odd count takes the middle',
      medianInterval([0, 1, 2, 3, 100]) === 1);
    check('an even count averages the two',
      medianInterval([0, 2, 6, 10]) === 4);
    check('no intervals gives zero', medianInterval([5]) === 0);
    check('an empty list gives zero', medianInterval([]) === 0);
    check('a flat list gives zero, since no interval is positive',
      medianInterval([7, 7, 7]) === 0);
  }

  console.log('\nparsing reports what it could not read:');
  {
    const got = parseTimes(['2026-11-21T00:00:00Z', '', null, 'soon',
      undefined, '2026-11-21T00:02:00Z']);
    check('a good time parses', got[0] === Date.UTC(2026, 10, 21));
    check('each bad one is null',
      [got[1], got[2], got[3], got[4]].every((t) => t === null),
      JSON.stringify(got));
    check('a later good time parses too', got[5] > got[0]);
  }

  console.log('\na click inside a gap lands on a frame:');
  {
    const axis = buildAxis([
      ...steady(5),
      new Date(Date.UTC(2026, 10, 21, 3, 0, 0)).toISOString()
    ]);
    check('the left edge is the first frame',
      nearestStep(axis.positions, 0) === 0);
    check('the right edge is the last',
      nearestStep(axis.positions, 1) === axis.positions.length - 1);
    check('a point just past the dense group stays in it',
      nearestStep(axis.positions, 0.1) === 4, String(
        nearestStep(axis.positions, 0.1)));
    check('a point most of the way across takes the far frame',
      nearestStep(axis.positions, 0.9) === 5);
    check('outside the axis clamps to an end',
      nearestStep(axis.positions, -3) === 0
        && nearestStep(axis.positions, 4) === 5);
    check('a NaN fraction does not throw and picks the first',
      nearestStep(axis.positions, NaN) === 0);
    check('an empty axis has no step', nearestStep([], 0.5) === -1);
  }

  console.log('\na gap reads in units a person uses:');
  {
    check('minutes under an hour', gapLabel(40 * MINUTE) === '40m');
    check('whole hours drop the minutes', gapLabel(120 * MINUTE) === '2h');
    check('hours and minutes together',
      gapLabel(135 * MINUTE) === '2h 15m');
    check('a day and over', gapLabel(30 * 60 * MINUTE) === '1d 6h');
    check('nothing is blank rather than "0m"', gapLabel(0) === '');
    check('a negative is blank', gapLabel(-5) === '');
    check('a NaN is blank', gapLabel(NaN) === '');
  }

  if (failures.length) {
    console.log(`\n${failures.length} failed:`);
    for (const f of failures) console.log(`  ${f}`);
    return 1;
  }
  console.log('\nall time-axis assertions passed');
  return 0;
}
