// Reading an overlay pixel back as a value.
//
// Run:  node validation_tests/js/run_map_ramps.mjs
//
// A readout that is wrong reads exactly like one that is right. A number
// beside a cursor carries more authority than the picture under it, and
// nobody checks a number against a color by eye.
//
// The two failures this guards are both silent. A tolerance wider than the
// gap between two stops matches a color to a stop it does not belong to and
// prints a confident wrong number. A tolerance that admits anything turns
// the map background and the basemap labels into readings.
//
// The ramps themselves are measured separately, against real frames, by
// validation_tests/visual/ramp_coverage.py.

export function runTests(mod, ramps) {
  const {
    invert, parseHex, colorDistance, selfCoverage,
    MATCH_TOLERANCE, TRANSPARENT_ALPHA, toleranceFor, toleranceAt,
    SNAP_BUDGET, WRITE_ERROR, channelError, summedError
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const refl = ramps['flow-reflectivity'];
  const vel = ramps['flow-velocity'];
  const all = Object.entries(ramps);

  console.log('hex parses, or says why not:');
  {
    check('lowercase', parseHex('#ccefff').join(',') === '204,239,255',
      parseHex('#ccefff').join(','));
    check('uppercase', parseHex('#CCEFFF').join(',') === '204,239,255');
    check('without the hash', parseHex('ccefff').join(',') === '204,239,255');
    check('black', parseHex('#000000').join(',') === '0,0,0');
    check('white', parseHex('#ffffff').join(',') === '255,255,255');
    for (const bad of ['#ccc', '#ccefffff', 'not a color', '#gggggg', '']) {
      let threw = false;
      try { parseHex(bad); } catch (e) { threw = e instanceof RangeError; }
      check(`${JSON.stringify(bad)} is refused`, threw);
    }
  }

  console.log('\ndistance is summed over the channels:');
  {
    check('a color is zero from itself', colorDistance([1, 2, 3], [1, 2, 3]) === 0);
    check('black to white is the maximum',
      colorDistance([0, 0, 0], [255, 255, 255]) === 765);
    check('one channel counts once', colorDistance([10, 0, 0], [0, 0, 0]) === 10);
    check('it is symmetric',
      colorDistance([9, 20, 3], [1, 2, 30]) === colorDistance([1, 2, 30], [9, 20, 3]));
    check('every channel counts',
      colorDistance([1, 1, 1], [0, 0, 0]) === 3);
  }

  console.log('\nevery stop reads back as itself:');
  {
    for (const [name, ramp] of all) {
      const c = selfCoverage(ramp);
      check(`${name}: ${c.exact}/${c.total}`, c.exact === c.total,
        `${c.exact}/${c.total}`);
    }
  }

  console.log('\nthe tolerance is under the closest pair in every ramp:');
  {
    // The regression this file exists for. A tolerance at or above the
    // closest stop separation can match a color to the wrong stop, and the
    // readout prints a confident wrong number.
    for (const [name, ramp] of all) {
      let min = Infinity;
      for (let i = 0; i < ramp.stops.length; i++) {
        for (let j = i + 1; j < ramp.stops.length; j++) {
          const d = colorDistance(parseHex(ramp.stops[i].color),
            parseHex(ramp.stops[j].color));
          if (d < min) min = d;
        }
      }
      // Against this ramp's own tolerance. The product bars are not all
      // as loose as the radar ones: the forecast rate bars put two stops
      // 7 apart, where a flat 12 would match a pixel between them to
      // either one.
      const tol = toleranceFor(ramp);
      if (!ramp.built_from) {
        check(`${name}: closest pair ${min} > its tolerance ${tol}`,
          min > tol, `${min} vs ${tol}`);
      }
      check(`${name}: its tolerance is at most the global ${MATCH_TOLERANCE}`,
        tol <= MATCH_TOLERANCE, String(tol));
    }
  }

  console.log('\na generated ramp is bounded by value, not by separation:');
  {
    // A table quantised from a renderer's colormap has neighbors one
    // channel step apart by construction, so the closest-pair rule above
    // returns zero and only an exact pixel reads back. The renderers
    // resample, so most pixels land between two entries: on the water
    // level frames that rule read 18% of the painted area where 95% of
    // it sits within five of a stop.
    //
    // The rule for those is that snapping to the nearer entry may not
    // move the answer by more than SNAP_BUDGET of the ramp's range. That
    // is what this checks, stop by stop, which is also where it has to
    // hold: a colormap can be injective over most of its range and turn
    // back on itself over a little of it, as the water level map does
    // below zero.
    const generated = all.filter(([, r]) => !!r.built_from);
    check('there is a generated ramp to check',
      generated.length > 0, String(generated.length));
    for (const [name, ramp] of generated) {
      const allowed = Math.abs(ramp.max - ramp.min) * SNAP_BUDGET;
      let worst = 0;
      let worstAt = '';
      for (let i = 0; i < ramp.stops.length; i++) {
        const tol = toleranceAt(ramp, i);
        for (let j = 0; j < ramp.stops.length; j++) {
          if (i === j) continue;
          const d = colorDistance(parseHex(ramp.stops[i].color),
            parseHex(ramp.stops[j].color));
          // A color the producer painted twice is not a snap between
          // neighbors. invert reports both bands for it, which the
          // reused-color group below holds it to.
          if (d === 0) continue;
          if (d > tol) continue;
          const gap = Math.abs(ramp.stops[i].value - ramp.stops[j].value);
          if (gap > worst) {
            worst = gap;
            worstAt = `${ramp.stops[i].color} vs ${ramp.stops[j].color}`;
          }
        }
      }
      check(`${name}: no stop admits a value more than ${allowed.toFixed(3)} away`,
        worst <= allowed, `worst ${worst.toFixed(4)} ${worstAt}`);
      // A tolerance of zero everywhere would pass the line above while
      // reading nothing, which is the state this work started from.
      const per = ramp.stops.map((_, i) => toleranceAt(ramp, i));
      const strictest = Math.min(...per);
      const loosest = Math.max(...per);
      check(`${name}: some stop can be matched loosely`,
        loosest > 0, String(loosest));
      // And where a colormap turns back on itself, the stops in that
      // region must stay exact. Asked only of a ramp that has such a
      // region: the QPE and forecast rate maps are injective over their
      // whole range, so demanding a strict stop of them asked for a
      // flaw they do not have.
      const turnsBack = ramp.stops.some((a, i) => ramp.stops.some((b, j) =>
        i !== j
        && colorDistance(parseHex(a.color), parseHex(b.color)) <= MATCH_TOLERANCE
        && Math.abs(a.value - b.value) > allowed));
      if (turnsBack) {
        // Tighter where it turns back than where it does not. The
        // water level map repeats a color exactly and its purples go
        // to zero; the QPE map only comes close and its stops there
        // are merely tighter. Demanding zero of both asked the second
        // one for a flaw it does not have.
        check(`${name}: the stops near its repeat are held tighter`,
          strictest < loosest, `${strictest} vs ${loosest}`);
      } else {
        check(`${name}: is injective, so every stop is equally loose`,
          strictest === loosest && strictest > 0,
          `${strictest} vs ${loosest}`);
      }
    }
  }

  console.log('\na color painted twice reads as both of its bands:');
  {
    // radar-display's reflectivity table lists #646464 first and eighth,
    // for below -25 dBZ and for 5 to 10. Reporting the first match alone
    // put a confident wrong band on every gray pixel of the second kind.
    const tilt = ramps['tilt-reflectivity'];
    const gray = invert(tilt, [100, 100, 100]);
    check('the gray matches', gray !== null && gray.distance === 0,
      JSON.stringify(gray));
    check('...as its first stop, open below',
      gray?.lo === -30 && gray?.hi === -25 && gray?.open === 'below',
      JSON.stringify(gray));
    check('...and names the other band it paints',
      gray?.also?.length === 1 && gray.also[0].lo === 5 && gray.also[0].hi === 10,
      JSON.stringify(gray?.also));
    check('a near-gray matches neither, since the reused stops are exact',
      invert(tilt, [101, 100, 100]) === null,
      JSON.stringify(invert(tilt, [101, 100, 100])));
    const blue = invert(tilt, parseHex('#0300f4'));
    check('a color used once carries no other band',
      blue !== null && blue.also === undefined, JSON.stringify(blue));
    const top = invert(tilt, parseHex('#fdfdfd'));
    check('the top stop says it is open above',
      top?.open === 'above' && top.lo === 80, JSON.stringify(top));
    const c = selfCoverage(tilt);
    check('every stop, the second gray included, reads back',
      c.exact === c.total && c.total === 23, `${c.exact}/${c.total}`);
  }

  console.log('\na ramp with close stops gets a tighter tolerance:');
  {
    const tight = {
      ...refl,
      stops: [
        { color: '#000000', value: 0, lo: 0, hi: 1 },
        { color: '#000007', value: 1, lo: 1, hi: 2 }
      ]
    };
    check('seven apart allows six', toleranceFor(tight) === 6,
      String(toleranceFor(tight)));
    check('a pixel between the two still reads the nearer one',
      invert(tight, [0, 0, 3])?.value === 0,
      String(invert(tight, [0, 0, 3])?.value));
    check('a pixel past the tolerance reads nothing',
      invert(tight, [0, 0, 60]) === null);
    const wide = {
      ...refl,
      stops: [
        { color: '#000000', value: 0, lo: 0, hi: 1 },
        { color: '#ffffff', value: 1, lo: 1, hi: 2 }
      ]
    };
    check('a wide ramp is capped at the global tolerance',
      toleranceFor(wide) === MATCH_TOLERANCE, String(toleranceFor(wide)));
    const one = { ...refl, stops: [refl.stops[0]] };
    check('a single-stop ramp keeps the global tolerance',
      toleranceFor(one) === MATCH_TOLERANCE, String(toleranceFor(one)));
  }

  console.log('\nan exact color reads its own value:');
  {
    const stop = refl.stops[5];
    const r = invert(refl, parseHex(stop.color));
    check('the value comes back', r.value === stop.value, String(r?.value));
    check('with its band', r.lo === stop.lo && r.hi === stop.hi);
    check('at distance zero', r.distance === 0, String(r.distance));
    check('the band is below the value', r.lo <= r.value && r.value <= r.hi);
  }

  console.log('\na color just off a stop still reads it:');
  {
    const stop = refl.stops[8];
    const [rr, gg, bb] = parseHex(stop.color);
    const near = [rr + 3, gg - 3, bb + 2];
    const r = invert(refl, near);
    check('it matches', r !== null && r.value === stop.value, JSON.stringify(r));
    check('and reports how far', r.distance === 8, String(r?.distance));
  }

  console.log('\na color off the ramp reads nothing:');
  {
    // The map background, a basemap label showing through, and the edge of
    // the radar's coverage all land here. A guess would put a confident dBZ
    // on a road name.
    check('mid grey is not on the reflectivity ramp',
      invert(refl, [128, 128, 128]) === null,
      JSON.stringify(invert(refl, [128, 128, 128])));
    check('a landform brown is not', invert(refl, [120, 80, 40]) === null,
      JSON.stringify(invert(refl, [120, 80, 40])));
    const far = invert(refl, [1, 2, 3]);
    check('near-black is not', far === null, JSON.stringify(far));
    // Pure red is not a safe negative. Reflectivity paints 53.2 dBZ as
    // #fd0000, two channels off it, so a test that expected red to be
    // off-ramp would be asserting the ramp is wrong.
    const red = invert(refl, [255, 0, 0]);
    check('pure red is 53.2 dBZ, not off the ramp',
      red !== null && Math.abs(red.value - 53.2) < 0.1, JSON.stringify(red));
  }

  console.log('\na transparent pixel is outside coverage:');
  {
    const stop = refl.stops[3];
    const rgb = parseHex(stop.color);
    check('alpha zero reads nothing', invert(refl, rgb, 0) === null);
    check('at the floor it still reads nothing',
      invert(refl, rgb, TRANSPARENT_ALPHA) === null);
    check('just above the floor it reads',
      invert(refl, rgb, TRANSPARENT_ALPHA + 1) !== null);
    check('opaque reads', invert(refl, rgb, 255) !== null);
    check('the default is opaque', invert(refl, rgb) !== null);
  }

  console.log('\nthe ramps carry their provenance:');
  {
    for (const [name, ramp] of all) {
      // Where the table came from, so a reader can go and check it.
      // Two kinds are legal. A colorbar image, named by a path on the
      // mount for a radar moment or by a URL for a product. Or the
      // renderer's own colormap definition, which has to name the file
      // and line it was read at.
      //
      // The second kind is the stronger evidence and was added when the
      // water-level bar turned out to be a hand-made asset with no code
      // path to the plotter: it verified against its own printed
      // numbers and explained 0.032% of the pixels in real frames.
      const fromBar = /(colorbar|colormap)[a-z0-9_-]*\.png$/;
      // A python file plus a line, or a python file plus the function
      // the definition sits in. Either one takes a reader to the same
      // block, and a renderer's colormap is as often named by its
      // function as by a line that moves with the next edit.
      const fromCode = /\.py(:\d+|\s+\w+)/;
      check(`${name} names where its colors came from`,
        typeof ramp.source === 'string'
          && (fromBar.test(ramp.source) || fromCode.test(ramp.source)),
        ramp.source);
      // A ramp built from code has to carry the definition it was built
      // from, so the file can be rebuilt without going back to the host.
      if (typeof ramp.source === 'string' && !fromBar.test(ramp.source)) {
        check(`${name} carries the colormap it was built from`,
          !!ramp.built_from && Array.isArray(ramp.built_from.anchors)
            && ramp.built_from.anchors.length > 1
            && Number.isFinite(ramp.built_from.vmin)
            && Number.isFinite(ramp.built_from.vmax),
          JSON.stringify(ramp.built_from?.anchors?.length));
      }
      check(`${name} carries the date it was read`,
        /^\d{4}-\d{2}-\d{2}$/.test(ramp.read_on), ramp.read_on);
      // A unit, or a declared absence of one. The correlation
      // coefficient is a ratio and has none, and requiring a truthy
      // string would have had it carry an invented "ratio" or "-".
      // `dimensionless` makes the empty string deliberate, so a unit
      // somebody forgot still fails here.
      check(`${name} carries a unit and a label`,
        !!ramp.label
        && (!!ramp.unit || ramp.dimensionless === true),
        `${JSON.stringify(ramp.unit)} / ${ramp.label} / `
        + `dimensionless=${ramp.dimensionless}`);
    }
  }

  console.log('\nthe stops cover the bar from end to end:');
  {
    for (const [name, ramp] of all) {
      const first = ramp.stops[0];
      const last = ramp.stops[ramp.stops.length - 1];
      check(`${name} starts at its minimum`,
        Math.abs(first.lo - ramp.min) < 0.5, `${first.lo} vs ${ramp.min}`);
      check(`${name} ends at its maximum`,
        Math.abs(last.hi - ramp.max) < 0.5, `${last.hi} vs ${ramp.max}`);
      // A segmented bar reports the band between the numbers printed on
      // it, because the shape between two of them is not recoverable
      // from the picture. Ten of its colors can carry one band, so its
      // stops rise without strictly increasing and repeat a band rather
      // than tiling the axis. Both are the point of that ramp kind.
      const banded = ramp.scale === 'segmented';
      const ascending = ramp.stops.every((s, i) =>
        i === 0 || (banded
          ? s.value >= ramp.stops[i - 1].value
          : s.value > ramp.stops[i - 1].value));
      check(`${name} ascends`, ascending);
      const contiguous = ramp.stops.every((s, i) =>
        i === 0 || (banded
          ? s.lo >= ramp.stops[i - 1].lo
          : Math.abs(s.lo - ramp.stops[i - 1].hi) < ramp.stops.length * 0.1));
      check(`${name} leaves no gap between stops`, contiguous);
    }
  }

  console.log('\nreflectivity is banded and velocity is smooth:');
  {
    // A discrete ramp and a continuous one read back differently, and the
    // legend draws them differently too.
    const banded = refl.stops.every((s) => s.hi - s.lo > 1);
    check('reflectivity bands are wider than a unit', banded);
    check('...about five dBZ each',
      Math.abs((refl.stops[5].hi - refl.stops[5].lo) - 4.4) < 1,
      (refl.stops[5].hi - refl.stops[5].lo).toFixed(2));
    check('velocity resolves near a metre per second',
      (vel.stops[5].hi - vel.stops[5].lo) < 1.5,
      (vel.stops[5].hi - vel.stops[5].lo).toFixed(2));
  }

  // The composite producer paints in bands and then resamples, so where two
  // bands meet a pixel mixes both colors. Measured on 2026-11-24: 33% of
  // comp_ref's solid pixels were such mixes and read "not on this scale".
  const comp = ramps['aqpi-product-reflectivity'];
  const rgbOf = (hex) => parseHex(hex);
  // A mix as the producer writes it: weighted, then truncated to 8 bits.
  const mix = (a, b, w) => a.map((c, i) => Math.floor((1 - w) * c + w * b[i]));
  const band = (r) => (r ? `${r.lo} to ${r.hi}${r.open ? ` ${r.open}` : ''}` : 'null');
  const blocks = (stops, extra = {}) => ({
    moment: 'Test', unit: 'u', label: 'T [u]', min: 0, max: 40,
    source: 'synthetic', read_on: '2026-11-24', blends: 'resampled', stops, ...extra
  });

  console.log('\na pixel between two bands reads as the span of both:');
  {
    const cyan = comp.stops[7];
    const blue = comp.stops[8];
    const half = invert(comp, mix(rgbOf(cyan.color), rgbOf(blue.color), 0.5));
    check('a half mix of 5 to 10 and 10 to 15 dBZ spans both',
      half?.blend === true && half.lo === cyan.lo && half.hi === blue.hi, band(half));
    // The weight says where the pixel sat between two cells, not what
    // either held, so no value inside the span is singled out.
    const third = invert(comp, mix(rgbOf(cyan.color), rgbOf(blue.color), 0.3));
    check('...and so does a mix weighted toward one of them',
      third?.blend === true && third.lo === half.lo && third.hi === half.hi, band(third));
    const slight = invert(comp, mix(rgbOf(cyan.color), rgbOf(blue.color), 0.02));
    check('a trace of the neighbor still reads the band it is mostly',
      slight !== null && !slight.blend && slight.lo === cyan.lo, band(slight));
    // Further apart, a mix is as likely three bands or linework. The red
    // team's case: a blue and red mix passes within 3 of the purples.
    const green = comp.stops[10];
    const wide = invert(comp, mix(rgbOf(blue.color), rgbOf(green.color), 0.5));
    check('a mix of bands further apart reads as nothing', wide === null, band(wide));
    check('a purple near a blue and red mix is not read as 10 to 65 dBZ',
      invert(comp, [107, 63, 107]) === null, band(invert(comp, [107, 63, 107])));
    const plain = { ...comp };
    delete plain.blends;
    check('a ramp not measured to resample reads no mixes',
      invert(plain, mix(rgbOf(cyan.color), rgbOf(blue.color), 0.5)) === null,
      band(invert(plain, mix(rgbOf(cyan.color), rgbOf(blue.color), 0.5))));
  }

  console.log('\na mix is read only inside the write error, and only one way:');
  {
    const s = blocks([
      { color: '#640000', value: 5, lo: 0, hi: 10 },
      { color: '#006400', value: 15, lo: 10, hi: 20 },
      { color: '#646400', value: 25, lo: 20, hi: 30 },
      { color: '#000000', value: 35, lo: 30, hi: 40 }
    ]);
    check('a truncated write moves a mix at most 3 summed',
      summedError(channelError(255, false)) === 3 * WRITE_ERROR,
      String(summedError(channelError(255, false))));
    // #640000 to #006400 and #646400 to #000000 cross at (50, 50, 0).
    check('where two mixes cross, the pixel reads as nothing',
      invert(s, [50, 50, 0]) === null, band(invert(s, [50, 50, 0])));
    // Kept a neighbor of #640000, so the mix is one the rule may read.
    const lone = blocks([s.stops[0], s.stops[1],
      { color: '#ffffff', value: 25, lo: 20, hi: 30 }]);
    check('with one mix there, it reads',
      invert(lone, [50, 50, 0])?.blend === true, band(invert(lone, [50, 50, 0])));
    check('3 off the mix still reads',
      invert(lone, [50, 50, 3]) !== null, band(invert(lone, [50, 50, 3])));
    check('4 off it does not', invert(lone, [50, 50, 4]) === null,
      band(invert(lone, [50, 50, 4])));
    const ends = blocks([
      { color: '#640000', value: -5, lo: -10, hi: 0, open: 'below' },
      { color: '#006400', value: 45, lo: 40, hi: 50, open: 'above' }
    ]);
    check('a mix open at both ends says nothing',
      invert(ends, [50, 50, 0]) === null, band(invert(ends, [50, 50, 0])));
    const open = blocks([ends.stops[0], s.stops[1],
      { color: '#ffffff', value: 25, lo: 20, hi: 30 }]);
    const below = invert(open, [50, 50, 0]);
    check('a mix with the open bottom band keeps it open',
      below?.open === 'below' && below.hi === 20, band(below));
  }

  console.log('\na gradient has no mixes, only its own stops:');
  {
    const a = rgbOf(vel.stops[3].color);
    const b = rgbOf(vel.stops[50].color);
    const m = mix(a, b, 0.5);
    check('a color between two far velocity stops is not read as a span',
      invert(vel, m)?.blend !== true, band(invert(vel, m)));
    // Sixty stops one red step apart, then two lone ones. (0, 100, 100)
    // lies on the mix of the lone pair and no other, so only the ramp's
    // kind keeps it from reading as their span.
    const red = Array.from({ length: 60 }, (_, i) => ({
      color: `#${i.toString(16).padStart(2, '0')}0000`, value: i, lo: i, hi: i + 1
    }));
    const lone = blocks([...red,
      { color: '#00c800', value: 70, lo: 70, hi: 71 },
      { color: '#0000c8', value: 80, lo: 80, hi: 81 }], { max: 81 });
    check('even a color only one mix explains is off a gradient',
      invert(lone, [0, 100, 100]) === null, band(invert(lone, [0, 100, 100])));
  }

  // The radarca products write an edge pixel's color already multiplied by
  // its alpha. Read as stored it is darker than its band, and the composite's
  // 20 to 25 dBZ green there lands on its 25 to 30 green.
  const premult = (rgb, a) => rgb.map((c) => Math.floor((c * a) / 255));
  const unflagged = { ...comp };
  delete unflagged.edges;

  console.log('\na premultiplied edge is divided out, from exact bytes only:');
  {
    const lime = comp.stops[10];
    const edge = premult(rgbOf(lime.color), 200);
    const asStored = invert(unflagged, edge, 200);
    check('read as stored, the lime edge reads one band too high',
      asStored?.lo === comp.stops[11].lo, band(asStored));
    const exact = invert(comp, edge, 200, { exact: true });
    check('divided out, it reads its own band',
      exact?.lo === lime.lo && !exact.blend, band(exact));
    check('from a rounded canvas readback it reads nothing',
      invert(comp, edge, 200) === null, band(invert(comp, edge, 200)));
    const faint = premult(rgbOf(lime.color), 20);
    check('at alpha 20 the error is too wide to name a band',
      invert(comp, faint, 20, { exact: true }) === null,
      band(invert(comp, faint, 20, { exact: true })));
    check('a solid pixel is read the same either way',
      band(invert(comp, rgbOf(lime.color), 255, { exact: true }))
        === band(invert(comp, rgbOf(lime.color), 255)));
    check('dividing by alpha scales both writes by 255 over it',
      Math.abs(channelError(128, true) - 2 * WRITE_ERROR * 255 / 128) < 1e-9,
      String(channelError(128, true)));
  }

  console.log('\na recovered color reads a band only if its whole error does:');
  {
    // Stored (66, 66, 67) at alpha 170 recovers as (99, 99, 100.5), 1.5 from
    // the first stop, with a summed error of 9 either way.
    const near = blocks([
      { color: '#636363', value: 5, lo: 0, hi: 10 },
      { color: '#636370', value: 15, lo: 10, hi: 20 }
    ], { edges: 'premultiplied' });
    const far = blocks([near.stops[0],
      { color: '#63638c', value: 15, lo: 10, hi: 20 }], { edges: 'premultiplied' });
    check('with the next band 13 away, the error could reach it: nothing',
      invert(near, [66, 66, 67], 170, { exact: true }) === null,
      band(invert(near, [66, 66, 67], 170, { exact: true })));
    check('with it 41 away, the band holds',
      invert(far, [66, 66, 67], 170, { exact: true })?.lo === 0,
      band(invert(far, [66, 66, 67], 170, { exact: true })));
  }

  console.log('\nthe premultiplied ramps are the measured ones, and say so:');
  {
    const flagged = all.filter(([, r]) => r.edges).map(([n]) => n).sort();
    const measured = ['aqpi-product-qpe-in', 'aqpi-product-qpe-rate-in',
      'aqpi-product-reflectivity', 'aqpi-product-water-depth',
      'aqpi-product-water-level'];
    // A regenerated ramp file drops a hand-set field, and the readout would
    // go back to reading darkened edges as stored without a word.
    check('exactly the five measured ramps are flagged',
      JSON.stringify(flagged) === JSON.stringify(measured), flagged.join(', '));
    for (const [name, r] of all.filter(([, x]) => x.edges)) {
      check(`${name} says what it was measured on`,
        r.edges === 'premultiplied' && /^\d{4}-\d{2}-\d{2}, /.test(r.edges_measured ?? ''),
        String(r.edges_measured).slice(0, 40));
    }
    // Every other discrete ramp's frames read back in full, so nothing in
    // them lies between two stops but linework.
    const blending = all.filter(([, r]) => r.blends).map(([n]) => n);
    check('only the composite is read for mixes, the one measured to resample',
      blending.join() === 'aqpi-product-reflectivity'
        && comp.blends === 'resampled' && /^\d{4}-\d{2}-\d{2}, /.test(comp.blends_measured ?? ''),
      blending.join(', '));
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall ramp assertions passed');
  return failures.length ? 1 : 0;
}
