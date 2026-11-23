// Cursor position to overlay pixel, and the text that goes beside it.
//
// Run:  node validation_tests/js/run_map_readout.mjs
//
// A readout is the most trusted thing on the map. Nobody checks a number
// against a color by eye, and a number half a cell from where the cursor is
// reads exactly like one that is right.
//
// The failure these assertions are built around is interpolating in latitude
// instead of Web Mercator. MapLibre places an image by its corners and
// interpolates between them in Mercator, so a latitude-linear inverse drifts
// toward the middle of the overlay and is worst there. Over a Bay Area box
// that drift is kilometers, which is several cells.

export function runTests(mod, ramps) {
  const {
    overlayFraction, overlayPixel, readoutText, bandDecimals,
    readoutFor, readoutHint, OFF_SCALE,
    MERCATOR_LIMIT_DEG, RECTANGLE_TOLERANCE_M
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const threw = (fn) => {
    try { fn(); return false; } catch (e) { return e instanceof RangeError; }
  };
  // AQPI's composite box, near enough: a Bay Area overlay.
  const box = (w, s, e, n) => [[w, n], [e, n], [e, s], [w, s]];
  const BAY = box(-123.5, 36.5, -121.0, 39.0);

  console.log('the corners map to the corners:');
  {
    const nw = overlayFraction(BAY, -123.5, 39.0);
    const se = overlayFraction(BAY, -121.0, 36.5);
    check('north-west is 0,0',
      Math.abs(nw.u) < 1e-9 && Math.abs(nw.v) < 1e-9,
      `${nw.u} ${nw.v}`);
    check('south-east is 1,1',
      Math.abs(se.u - 1) < 1e-9 && Math.abs(se.v - 1) < 1e-9,
      `${se.u} ${se.v}`);
    const ne = overlayFraction(BAY, -121.0, 39.0);
    const sw = overlayFraction(BAY, -123.5, 36.5);
    check('north-east is 1,0',
      Math.abs(ne.u - 1) < 1e-9 && Math.abs(ne.v) < 1e-9);
    check('south-west is 0,1',
      Math.abs(sw.u) < 1e-9 && Math.abs(sw.v - 1) < 1e-9);
  }

  console.log('\nlongitude is linear and latitude is not:');
  {
    // Longitude maps linearly to Mercator x, so the midpoint is 0.5 exactly.
    const midLon = overlayFraction(BAY, -122.25, 39.0);
    check('the longitude midpoint is halfway across',
      Math.abs(midLon.u - 0.5) < 1e-12, String(midLon.u));
    // Latitude does not. This is the whole reason the inverse runs in
    // Mercator: a latitude-linear reading would put the midpoint at 0.5.
    const midLat = overlayFraction(BAY, -123.5, 37.75);
    check('the latitude midpoint is not halfway down',
      Math.abs(midLat.v - 0.5) > 1e-4, String(midLat.v));
    // v grows southward, so v > 0.5 puts the mid-latitude point below the
    // overlay's vertical center. Mercator stretches poleward, so the
    // northern half of the box takes more of the image than the southern.
    check('and it sits below center, since the northern half is stretched',
      midLat.v > 0.5, String(midLat.v));
  }

  console.log('\nthe Mercator drift is worth the arithmetic:');
  {
    // How far a latitude-linear inverse would be wrong, in overlay pixels
    // and on the ground. If this ever came out small, the simpler inverse
    // would be defensible; it does not.
    const span = 39.0 - 36.5;
    let worstFrac = 0;
    let worstLat = 0;
    for (let lat = 36.5; lat <= 39.0; lat += 0.01) {
      const merc = overlayFraction(BAY, -123.5, lat).v;
      const naive = (39.0 - lat) / span;
      if (Math.abs(merc - naive) > worstFrac) {
        worstFrac = Math.abs(merc - naive);
        worstLat = lat;
      }
    }
    // 2.5 degrees of latitude is about 278 km.
    const km = worstFrac * 278;
    check('a latitude-linear inverse drifts by kilometers',
      km > 1, `${km.toFixed(2)} km at ${worstLat.toFixed(2)}N ` +
      `(${(worstFrac * 100).toFixed(3)}% of the overlay)`);
    const edgeN = overlayFraction(BAY, -123.5, 39.0).v;
    const edgeS = overlayFraction(BAY, -123.5, 36.5).v;
    check('and is exact at both edges, which is why it looks right',
      Math.abs(edgeN - 0) < 1e-12 && Math.abs(edgeS - 1) < 1e-12);
  }

  console.log('\noutside the overlay reads nothing:');
  {
    check('west of it', overlayFraction(BAY, -124.0, 37.5) === null);
    check('east of it', overlayFraction(BAY, -120.0, 37.5) === null);
    check('north of it', overlayFraction(BAY, -122.0, 40.0) === null);
    check('south of it', overlayFraction(BAY, -122.0, 35.0) === null);
    check('just outside the west edge',
      overlayFraction(BAY, -123.5001, 37.5) === null);
    check('just inside it',
      overlayFraction(BAY, -123.4999, 37.5) !== null);
  }

  console.log('\nan unprojectable point reads nothing:');
  {
    check('NaN longitude', overlayFraction(BAY, NaN, 37.5) === null);
    check('NaN latitude', overlayFraction(BAY, -122, NaN) === null);
    check('infinite longitude', overlayFraction(BAY, Infinity, 37.5) === null);
    check('past the Mercator limit',
      overlayFraction(BAY, -122, MERCATOR_LIMIT_DEG + 0.001) === null);
    check('a point just inside the limit still projects',
      overlayFraction(box(-123.5, 80, -121, MERCATOR_LIMIT_DEG), -122, 85)
      !== null);
    check('and a box reaching past it is refused outright',
      threw(() => overlayFraction(box(-123.5, 80, -121, 86), -122, 84)));
  }

  console.log('\na box with no area is refused, not guessed at:');
  {
    check('zero width', threw(() => overlayFraction(
      box(-122, 36.5, -122, 39), -122, 37.5)));
    check('zero height', threw(() => overlayFraction(
      box(-123.5, 37, -121, 37), -122, 37)));
    check('west and east swapped', threw(() => overlayFraction(
      box(-121, 36.5, -123.5, 39), -122, 37.5)));
    check('north and south swapped', threw(() => overlayFraction(
      box(-123.5, 39, -121, 36.5), -122, 37.5)));
    check('an unprojectable corner', threw(() => overlayFraction(
      box(-123.5, 36.5, -121, 89), -122, 37.5)));
    check('a NaN corner', threw(() => overlayFraction(
      box(NaN, 36.5, -121, 39), -122, 37.5)));
  }

  console.log('\na quad that is not a rectangle reads nothing:');
  {
    // A projective inverse would be needed. Answering with the rectangular
    // one gives a plausible wrong pixel, which is the worst outcome here.
    const sheared = [[-123.5, 39.0], [-121.0, 39.0], [-120.5, 36.5],
      [-123.5, 36.5]];
    check('a sheared quad', overlayFraction(sheared, -122, 37.5) === null);
    const tilted = [[-123.5, 39.0], [-121.0, 38.9], [-121.0, 36.5],
      [-123.5, 36.5]];
    check('a tilted top edge', overlayFraction(tilted, -122, 37.5) === null);
    check('the tolerance is tight enough to be meaningful',
      RECTANGLE_TOLERANCE_M <= 1, String(RECTANGLE_TOLERANCE_M));
  }

  console.log('\na fraction lands on a real pixel:');
  {
    check('the north-west corner is 0,0',
      JSON.stringify(overlayPixel({ u: 0, v: 0 }, 800, 600)) === '{"x":0,"y":0}');
    check('the south-east corner is the last pixel',
      JSON.stringify(overlayPixel({ u: 1, v: 1 }, 800, 600))
      === '{"x":799,"y":599}',
      JSON.stringify(overlayPixel({ u: 1, v: 1 }, 800, 600)));
    check('the middle is the middle',
      JSON.stringify(overlayPixel({ u: 0.5, v: 0.5 }, 800, 600))
      === '{"x":400,"y":300}');
    check('just inside the last pixel stays inside',
      overlayPixel({ u: 0.99999, v: 0.99999 }, 800, 600).x === 799);
    check('a one-pixel image has one pixel',
      JSON.stringify(overlayPixel({ u: 1, v: 1 }, 1, 1)) === '{"x":0,"y":0}');
    check('a zero width is refused',
      threw(() => overlayPixel({ u: 0.5, v: 0.5 }, 0, 600)));
    check('a fractional size is refused',
      threw(() => overlayPixel({ u: 0.5, v: 0.5 }, 800.5, 600)));
    check('a negative size is refused',
      threw(() => overlayPixel({ u: 0.5, v: 0.5 }, 800, -600)));
  }

  console.log('\nthe band precision follows the band:');
  {
    for (const [w, want] of [[20, 0], [10, 0], [5, 1], [4.55, 1], [1, 1],
      [0.5, 2], [0.1, 2]]) {
      check(`a band ${w} wide takes ${want} decimals`,
        bandDecimals(w) === want, String(bandDecimals(w)));
    }
    check('a zero band does not crash', bandDecimals(0) === 1);
    check('NaN does not crash', bandDecimals(NaN) === 1);
  }

  // The forecast accumulation bands run as narrow as 0.001 in, and at two
  // decimals 0.010 to 0.011 in printed as "0.01 to 0.01 in". Held over
  // every shipped ramp, since any narrow band anywhere fails the same way.
  console.log('\nevery discrete band prints two different bounds:');
  {
    const bad = [];
    for (const [name, ramp] of Object.entries(ramps)) {
      if (ramp.scale !== 'segmented' && !/reflectivity|velocity|copolar/.test(name)) continue;
      for (const s of ramp.stops) {
        if (s.open) continue;
        const text = readoutText(ramp, { value: s.value, lo: s.lo, hi: s.hi, distance: 0 });
        const m = /^(-?[\d.]+) to (-?[\d.]+)/.exec(text ?? '');
        if (m && m[1] === m[2]) bad.push(`${name}: ${text}`);
      }
    }
    check('no band prints the same number at both ends', bad.length === 0,
      bad.slice(0, 4).join('; ') + (bad.length > 4 ? ` (+${bad.length - 4})` : ''));
    const accum = ramps['aqpi-product-forecast-accum-mm'];
    if (accum) {
      const first = accum.stops[0];
      check('the masked floor prints as itself, not rounded down',
        readoutText(accum, { value: first.value, lo: first.lo, hi: first.hi, distance: 0 })
          === '0.075 to 0.125 mm',
        String(readoutText(accum, { value: first.value, lo: first.lo, hi: first.hi,
          distance: 0 })));
      const top = accum.stops[accum.stops.length - 1];
      const topText = readoutText(accum, { value: top.value, lo: top.lo, hi: top.hi,
        distance: 0, open: top.open });
      check('the top band is open, from its own edge and not a rounded one',
        topText === '230.6 mm or more', String(topText));
    }
  }

  console.log('\na discrete ramp reports its band, honestly:');
  {
    // AQPI's frames are painted through BoundaryNorm(-25..75 by 5), so a
    // color is a 5 dBZ bin on round bounds. The bar-derived ramp this
    // replaced printed "-15.6 to -11.3 dBZ" for #cabfcc, which is -25 to -20.
    const aqpi = ramps['aqpi-xband-reflectivity'];
    const s = aqpi.stops[1];
    const text = readoutText(aqpi, { value: s.value, lo: s.lo, hi: s.hi, distance: 0 });
    check('it reads as a range', /to/.test(text), text);
    check('it carries the unit', text.endsWith(' dBZ'), text);
    check('it prints the bin the norm painted',
      text === '-25.0 to -20.0 dBZ', text);
    const flow = ramps['flow-reflectivity'];
    const f = flow.stops[2];
    check("FLOW's bins are the round ones",
      readoutText(flow, { value: f.value, lo: f.lo, hi: f.hi, distance: 0 })
      === '-20.0 to -15.2 dBZ',
      readoutText(flow, { value: f.value, lo: f.lo, hi: f.hi, distance: 0 }));
  }

  // radar-display clips at both ends and paints #646464 for two bands,
  // so a single closed range would be wrong on the ends and on half the
  // gray pixels. Readings built the way invert returns them for those
  // stops, which test_map_ramps checks separately.
  console.log('\nan open end and a reused color read as what they cover:');
  {
    const tilt = ramps['tilt-reflectivity'];
    const top = tilt.stops[tilt.stops.length - 1];
    check('the top stop reads as everything above its bound',
      readoutText(tilt, { value: top.value, lo: top.lo, hi: top.hi, distance: 0,
        open: 'above' }) === '80.0 dBZ or more',
      readoutText(tilt, { value: top.value, lo: top.lo, hi: top.hi, distance: 0,
        open: 'above' }));
    const gray = tilt.stops[0];
    const both = readoutText(tilt, {
      value: gray.value, lo: gray.lo, hi: gray.hi, distance: 0, open: 'below',
      also: [{ lo: 5, hi: 10 }]
    });
    check('a reused gray names both of its bands',
      both === 'below -25.0 dBZ or 5.0 to 10.0 dBZ', both);
    const order = readoutText(tilt, {
      value: 7.5, lo: 5, hi: 10, distance: 0,
      also: [{ lo: -30, hi: -25, open: 'below' }]
    });
    check('...in ascending order whichever stop matched first',
      order === both, order);
    const vel = ramps['tilt-velocity'];
    const low = vel.stops[0];
    check('a velocity end band keeps its own precision',
      readoutText(vel, { value: low.value, lo: low.lo, hi: low.hi, distance: 0,
        open: 'below' }) === 'below -30.3 m/s',
      readoutText(vel, { value: low.value, lo: low.lo, hi: low.hi, distance: 0,
        open: 'below' }));
    const flow = ramps['flow-reflectivity'];
    const f = flow.stops[0];
    check('a ramp with no open ends reads as before',
      readoutText(flow, { value: f.value, lo: f.lo, hi: f.hi, distance: 0 })
      === '-30.0 to -25.6 dBZ',
      readoutText(flow, { value: f.value, lo: f.lo, hi: f.hi, distance: 0 }));
  }

  console.log('\na dimensionless ramp prints no unit and no gap:');
  {
    // The correlation coefficient is a ratio. Appending an empty unit
    // left "0.52 " with a trailing space inside the readout chip, which
    // shows as an off-center number against the chip's padding.
    const rho = ramps['aqpi-cband-rhohv'];
    const r = rho.stops[128];
    const text = readoutText(rho, {
      value: r.value, lo: r.lo, hi: r.hi, distance: 0
    });
    check('it has no unit to print', rho.unit === '', JSON.stringify(rho.unit));
    check('and says that is deliberate', rho.dimensionless === true);
    check('so the reading ends in a digit', /\d$/.test(text), text);
    check('and carries no trailing space', text === text.trim(),
      JSON.stringify(text));
  }

  console.log('\na continuous ramp reports one value:');
  {
    const vel = ramps['aqpi-xband-velocity'];
    const s = vel.stops[20];
    const text = readoutText(vel, { value: s.value, lo: s.lo, hi: s.hi, distance: 3 });
    check('no range', !/to/.test(text), text);
    check('it carries the unit', text.endsWith(' m/s'), text);
    // The precision is the ramp's own resolution, its span over its stop
    // count, not a fixed fraction of the span. Velocity resolves to about
    // 1.07 m/s, so a second decimal would be invented.
    check('one decimal, since velocity resolves to about 1 m/s',
      /^-?\d+\.\d m\/s$/.test(text), text);
    const zdr = ramps['aqpi-xband-differential-reflectivity'];
    const z = zdr.stops[30];
    const ztext = readoutText(zdr, { value: z.value, lo: z.lo, hi: z.hi, distance: 0 });
    check('two decimals, since ZDR resolves to about 0.1 dB',
      /^-?\d+\.\d\d dB$/.test(ztext), ztext);
    const phi = ramps['aqpi-xband-differential-phase'];
    const ph = phi.stops[30];
    const ptext = readoutText(phi, { value: ph.value, lo: ph.lo, hi: ph.hi, distance: 0 });
    check('one decimal, since PhiDP resolves to about 6 degrees',
      /^-?\d+\.\d deg$/.test(ptext), ptext);
  }

  console.log('\nnothing under the cursor prints nothing:');
  {
    check('a null reading', readoutText(ramps['flow-reflectivity'], null) === null);
  }

  console.log('\nno data and nothing readable are different answers:');
  {
    // Three outcomes rather than two. A transparent pixel is outside
    // the product's coverage. An opaque one that matches no stop is a
    // painted pixel that is not on this table, and a composite frame is
    // mostly that: they carry their own basemap linework and a
    // near-black background at full alpha. Reporting nothing for both
    // made the readout look broken, because a reader cannot tell
    // "nothing here" from "I cannot read this" and blames their aim.
    const blocks = {
      moment: 'Reflectivity', unit: 'dBZ', label: 'Z [dBZ]',
      min: 0, max: 40,
      stops: [
        { color: '#000080', value: 5, lo: 0, hi: 10 },
        { color: '#00ff00', value: 15, lo: 10, hi: 20 },
        { color: '#ff0000', value: 25, lo: 20, hi: 30 },
        { color: '#ffffff', value: 35, lo: 30, hi: 40 }
      ]
    };
    const hit = { value: 15, lo: 10, hi: 20, distance: 0 };

    check('a transparent pixel says nothing at all',
      readoutFor(blocks, [0, 0, 0, 0], null) === null);
    check('a nearly transparent pixel says nothing',
      readoutFor(blocks, [1, 2, 3, 4], null) === null);
    // The black background of a composite frame is painted, so the
    // reader gets told it is painted and unreadable.
    check('an opaque pixel off the table says so',
      readoutFor(blocks, [1, 1, 1, 255], null) === OFF_SCALE,
      String(readoutFor(blocks, [1, 1, 1, 255], null)));
    // No decimals, because the band is ten wide: bandDecimals keeps
    // the printed precision under the band's own.
    check('a matched pixel reports its band',
      readoutFor(blocks, [0, 255, 0, 255], hit) === '10 to 20 dBZ',
      String(readoutFor(blocks, [0, 255, 0, 255], hit)));
    // A reading that arrived with a transparent pixel is a
    // contradiction, and coverage wins: nothing is there to report.
    check('coverage beats a reading',
      readoutFor(blocks, [0, 255, 0, 0], hit) === null);
    check('the off-scale text cannot be mistaken for a value',
      !/\d/.test(OFF_SCALE), OFF_SCALE);
  }

  console.log('\na banded scale says why it reports a band:');
  {
    const blocks = {
      moment: 'Reflectivity', unit: 'dBZ', label: 'Z [dBZ]',
      min: 0, max: 40,
      stops: [
        { color: '#000080', value: 5, lo: 0, hi: 10 },
        { color: '#00ff00', value: 15, lo: 10, hi: 20 },
        { color: '#ff0000', value: 25, lo: 20, hi: 30 },
        { color: '#ffffff', value: 35, lo: 30, hi: 40 }
      ]
    };
    // A gradient reports one value, so it has nothing to explain.
    const gradient = {
      moment: 'Velocity', unit: 'm/s', label: 'V [m/s]',
      min: -30, max: 30,
      stops: Array.from({ length: 60 }, (_, i) => ({
        color: `#${i.toString(16).padStart(2, '0')}0000`,
        value: -30 + i, lo: -30 + i, hi: -29 + i
      }))
    };
    const hint = readoutHint(blocks);
    check('a block scale explains the band', typeof hint === 'string');
    check('and says how many bands there are',
      hint.includes(String(blocks.stops.length)), hint);
    // The reason matters more than the fact: the producer quantized the
    // field before publishing, so a single number would be invented.
    check('and says the quantizing was the producer\'s',
      /quantis|quantiz/.test(hint), hint);
    check('a gradient scale has nothing to explain',
      readoutHint(gradient) === null);
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall readout assertions passed');
  return failures.length ? 1 : 0;
}
