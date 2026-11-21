// Where everything goes in an exported image, and what it says.
//
// Run:  node validation_tests/js/run_map_export.mjs
//
// A screenshot of this console pasted into a thread loses the two things
// that make it readable: which minute it is and what the colors mean. So the
// assertions here are mostly about those two never going missing, whatever
// the console was showing when the button was pressed.
//
// The geometry matters for one reason beyond tidiness. The map is drawn at
// its own size, one pixel per rendered pixel, and a layout that scaled it
// would resample a radar frame, which reads as a blurrier product rather
// than a smaller picture.

const RAMP = {
  moment: 'Reflectivity',
  unit: 'dBZ',
  label: 'Z [dBZ]',
  min: -20,
  max: 80,
  source: 'colorbar.png',
  read_on: '2026-11-21',
  stops: [
    { color: '#000000', value: -20, lo: -20, hi: -10 },
    { color: '#0000ff', value: 0, lo: -10, hi: 10 },
    { color: '#ff0000', value: 60, lo: 10, hi: 80 }
  ]
};

const VIEW = {
  site: 'AQPI Sentinel',
  product: 'Total Precip · 1 h QPE',
  moment: 'Reflectivity',
  unit: 'in',
  stepLabel: 'SAT 21-Nov-2026  04:52:00',
  radars: ['XSCV', 'XEBY'],
  source: 'radarca.engr.colostate.edu'
};

export function runTests(mod) {
  const {
    exportLayout, exportCaption, exportFileName, legendRange,
    PADDING, FOOTER_HEIGHT, LEGEND_MAX_WIDTH, LEGEND_WIDTH_FRACTION
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  console.log('the map keeps its own pixels:');
  {
    const l = exportLayout(1200, 800, true);
    check('the map box is the canvas size',
      l.map.width === 1200 && l.map.height === 800,
      `${l.map.width}x${l.map.height}`);
    check('the image is the map plus padding and the caption band',
      l.width === 1200 + PADDING * 2
        && l.height === 800 + PADDING * 2 + FOOTER_HEIGHT,
      `${l.width}x${l.height}`);
    check('the map sits inside the padding',
      l.map.x === PADDING && l.map.y === PADDING);
    check('the caption band is under the map',
      l.footer.y === l.map.y + l.map.height);
    check('nothing runs off the right edge',
      l.map.x + l.map.width <= l.width
        && l.footer.x + l.footer.width <= l.width);
    check('nothing runs off the bottom',
      l.footer.y + l.footer.height <= l.height);
  }

  console.log('\nthe color bar fits whatever width it gets:');
  {
    for (const w of [320, 800, 1200, 2200, 4000]) {
      const l = exportLayout(w, 600, true);
      check(`at ${w} wide the bar is inside the caption band`,
        l.legend.x >= l.footer.x
          && l.legend.x + l.legend.width <= l.footer.x + l.footer.width,
        `${l.legend.x} to ${l.legend.x + l.legend.width} in `
          + `${l.footer.x} to ${l.footer.x + l.footer.width}`);
      check(`...and inside it vertically`,
        l.legend.y >= l.footer.y
          && l.legend.y + l.legend.height <= l.footer.y + l.footer.height);
    }
    check('a wide image does not get an absurd bar',
      exportLayout(4000, 600, true).legend.width === LEGEND_MAX_WIDTH,
      String(exportLayout(4000, 600, true).legend.width));
    check('a narrow one scales the bar down',
      exportLayout(500, 600, true).legend.width
        === Math.round(500 * LEGEND_WIDTH_FRACTION),
      String(exportLayout(500, 600, true).legend.width));
    check('no measured ramp means no bar',
      exportLayout(1200, 800, false).legend === null);
    check('...and the image is the same size either way',
      exportLayout(1200, 800, false).height
        === exportLayout(1200, 800, true).height);
  }

  console.log('\na degenerate canvas does not make a broken image:');
  {
    for (const [w, h] of [[0, 0], [-10, 600], [NaN, 600], [600, NaN],
      [0.4, 0.4]]) {
      const l = exportLayout(w, h, true);
      check(`a ${w} by ${h} canvas still gives a positive image`,
        l.width > 0 && l.height > 0 && Number.isFinite(l.width)
          && Number.isFinite(l.height), `${l.width}x${l.height}`);
      check('...with a whole number of pixels',
        Number.isInteger(l.width) && Number.isInteger(l.height),
        `${l.width}x${l.height}`);
    }
  }

  console.log('\nthe caption never loses the time:');
  {
    const c = exportCaption(VIEW);
    check('the title names the product', c.title.includes('1 h QPE'),
      c.title);
    check('...and the moment', c.title.includes('Reflectivity'));
    check('...and the unit', c.title.includes('in'));
    check('the time line is the frame label',
      c.when === VIEW.stepLabel, c.when);
    check('the credit names the deployment',
      c.credit.includes('AQPI Sentinel'));
    check('...and where the data came from',
      c.credit.includes('radarca.engr.colostate.edu'));
    check('...and which radars were drawn',
      c.credit.includes('XSCV') && c.credit.includes('XEBY'));

    const blank = exportCaption({ ...VIEW, stepLabel: '   ' });
    check('a missing frame label says so rather than leaving a blank',
      blank.when === 'frame time unknown', blank.when);
    const none = exportCaption({ ...VIEW, radars: [] });
    check('no radars says so rather than trailing a comma',
      none.credit.includes('no radar overlays'), none.credit);
    const bare = exportCaption({ ...VIEW, moment: null, unit: null });
    check('a product with no moment still has a title',
      bare.title === VIEW.product, bare.title);
    // The product label carries its own separator, so this uses one that
    // does not, or the check would pass on the label's own character.
    const plain = exportCaption({
      ...VIEW, product: 'Reflectivity', moment: null, unit: null
    });
    check('...and no separator with nothing to separate',
      plain.title === 'Reflectivity', plain.title);
  }

  console.log('\nthe file name survives a file system:');
  {
    const n = exportFileName('qpe_1hr', 'SAT 21-Nov-2026  04:52:00');
    check('it names the product', n.includes('qpe-1hr'), n);
    check('it carries the time', n.includes('04-52-00'), n);
    check('it ends in .png', n.endsWith('.png'), n);
    check('no character needs escaping',
      /^[a-z0-9.-]+$/.test(n), n);
    check('a product id with a slash cannot make a path',
      !exportFileName('a/b/c', 'x').includes('/'),
      exportFileName('a/b/c', 'x'));
    check('no leading or trailing dashes in the parts',
      !exportFileName('  spaced  ', '  also  ').includes('--'),
      exportFileName('  spaced  ', '  also  '));
    const fallback = exportFileName('comp_ref', '',
      new Date('2026-11-21T04:52:00Z'));
    check('no frame label falls back to the clock',
      fallback.includes('2026-11-21t04-52'), fallback);
    check('an empty product still gives a name',
      exportFileName('', '', new Date('2026-11-21T04:52:00Z'))
        .startsWith('sentinel-map-'),
      exportFileName('', '', new Date('2026-11-21T04:52:00Z')));
  }

  console.log('\nthe bar covers the ramp it was drawn from:');
  {
    const r = legendRange(RAMP);
    check('it starts at the lowest stop', r.lo === -20, String(r.lo));
    check('it ends at the highest', r.hi === 80, String(r.hi));
    check('an empty ramp has no range',
      legendRange({ ...RAMP, stops: [] }).hi === 0);
  }

  if (failures.length) {
    console.log(`\n${failures.length} failed:`);
    for (const f of failures) console.log(`  ${f}`);
    return 1;
  }
  console.log('\nall export assertions passed');
  return 0;
}
