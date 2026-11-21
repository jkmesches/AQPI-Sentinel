// Frame URLs and image placement.
//
// Run:  node validation_tests/js/run_map_frames.mjs
//
// Every rule here is a cache rule, and a wrong one draws a map that looks
// correct. A per-render cache-buster made every render a unique URL, so
// nothing was ever reused: revisiting five already-seen composite steps
// cost five more server hits and dropped the scrub to about 17 fps with
// 1.2 s main-thread blocks.
//
// The subtler rule is keying a frame on its timestamp instead of its index.
// The window rolls, so index 30 means a different frame an hour later, and
// a URL built from the index alone serves a stale frame under a shifted
// index. Nothing on screen says it happened.

export function runTests(mod) {
  const {
    compositeStepsPath,
    extentCorners, compositeImagePath, scanImagePath, tiltImagePath
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const qs = (path) => new URLSearchParams(path.split('?')[1]);

  console.log('corners run clockwise from the north-west:');
  {
    const e = { west: -124.005, east: -121.195, south: 36.5, north: 39.505 };
    const c = extentCorners(e);
    check('four corners', c.length === 4, String(c.length));
    check('north-west first', c[0][0] === e.west && c[0][1] === e.north, String(c[0]));
    check('north-east second', c[1][0] === e.east && c[1][1] === e.north);
    check('south-east third', c[2][0] === e.east && c[2][1] === e.south);
    check('south-west last', c[3][0] === e.west && c[3][1] === e.south);
    // A counter-clockwise or transposed order places the image mirrored,
    // and rainfall mirrored across a valley still reads as rainfall.
    check('each pair is lon then lat',
      c.every(([lon, lat]) => lon < 0 && lat > 0), JSON.stringify(c[0]));
    check('the top edge shares a latitude', c[0][1] === c[1][1]);
    check('the left edge shares a longitude', c[0][0] === c[3][0]);
  }

  console.log('\na composite frame is keyed on its timestamp:');
  {
    const base = { product: 'comp_ref', step: 30, ts: '2026-11-20T22:06:00Z' };
    const p = compositeImagePath(base);
    check('the product id is sent', qs(p).get('product_id') === 'comp_ref');
    check('the step index is sent', qs(p).get('step') === '30');
    check('the timestamp is sent', qs(p).get('ts') === base.ts, qs(p).get('ts'));
    // The regression this file exists for.
    check('two renders of one frame give one URL',
      compositeImagePath(base) === compositeImagePath({ ...base }));
    check('a different step is a different URL',
      compositeImagePath(base) !== compositeImagePath({ ...base, step: 29 }));
    // Index 30 an hour later is a different frame, and the URL has to say so.
    check('the same index with a new timestamp is a different URL',
      compositeImagePath(base)
        !== compositeImagePath({ ...base, ts: '2026-11-20T23:06:00Z' }));
    check('a different product is a different URL',
      compositeImagePath(base) !== compositeImagePath({ ...base, product: 'comp_now' }));
  }

  console.log('\nthe force-refresh is quantized, not a clock read:');
  {
    const base = { product: 'comp_ref', step: 30, ts: '2026-11-20T22:06:00Z' };
    check('absent by default', !qs(compositeImagePath(base)).has('_t'));
    check('zero is still absent',
      !qs(compositeImagePath({ ...base, buster: 0 })).has('_t'));
    check('a value is sent',
      qs(compositeImagePath({ ...base, buster: 59861530 })).get('_t') === '59861530');
    // Several force-refreshes inside one 30 s window share a cache hit,
    // which is the reason for quantizing rather than using Date.now().
    check('the same quantum gives the same URL',
      compositeImagePath({ ...base, buster: 59861530 })
        === compositeImagePath({ ...base, buster: 59861530 }));
    check('the next quantum gives another',
      compositeImagePath({ ...base, buster: 59861530 })
        !== compositeImagePath({ ...base, buster: 59861531 }));
  }

  console.log('\na scan is keyed on radar, moment and time:');
  {
    const base = { radar: 'XEBY', moment: 'Reflectivity' };
    const p = scanImagePath(base);
    check('the radar is sent', qs(p).get('radar') === 'XEBY');
    check('the moment is sent', qs(p).get('moment') === 'Reflectivity');
    check('no buster by default', !qs(p).has('_t'), p);
    check('no time when there is none', !qs(p).has('time'));
    check('a time is sent when there is one',
      qs(scanImagePath({ ...base, time: '2026-11-20T22:00:00Z' })).get('time')
        === '2026-11-20T22:00:00Z');
    // null is what the component holds when the live frame is showing, and
    // it must drop the parameter rather than send the string "null".
    check('a null time drops the parameter',
      !qs(scanImagePath({ ...base, time: null })).has('time'),
      scanImagePath({ ...base, time: null }));
    check('a different moment is a different URL',
      scanImagePath(base) !== scanImagePath({ ...base, moment: 'Velocity' }));
    check('a different radar is a different URL',
      scanImagePath(base) !== scanImagePath({ ...base, radar: 'XSCR' }));
    check('the same scan twice is one URL',
      scanImagePath(base) === scanImagePath({ radar: 'XEBY', moment: 'Reflectivity' }));
  }

  console.log('\nmoments with spaces survive the query string:');
  {
    // "Differential Reflectivity" unencoded would truncate the parameter.
    const p = scanImagePath({ radar: 'XEBY', moment: 'Differential Reflectivity' });
    check('the space is encoded', !/ /.test(p), p);
    check('...and decodes back',
      qs(p).get('moment') === 'Differential Reflectivity', qs(p).get('moment'));
  }

  console.log('\na tilt frame carries its elevation:');
  {
    const p = tiltImagePath({ radar: 'XEBY', elevation: 1.5, moment: 'DBZH', frame: 0 });
    check('the radar is sent', qs(p).get('radar') === 'XEBY');
    check('the elevation is sent', qs(p).get('el') === '1.5', qs(p).get('el'));
    check('the display tier moment is sent', qs(p).get('moment') === 'DBZH');
    check('frame zero is sent, not dropped', qs(p).get('frame') === '0', qs(p).get('frame'));
    check('a different elevation is a different URL',
      p !== tiltImagePath({ radar: 'XEBY', elevation: 2.5, moment: 'DBZH', frame: 0 }));
    check('a different frame is a different URL',
      p !== tiltImagePath({ radar: 'XEBY', elevation: 1.5, moment: 'DBZH', frame: 1 }));
    check('a whole-number elevation has no decimal point',
      qs(tiltImagePath({ radar: 'XEBY', elevation: 2, moment: 'DBZH', frame: 0 }))
        .get('el') === '2');
  }

  console.log('\nthe paths are relative to the API origin:');
  {
    // The component prefixes these with apiUrl, which resolves the backend
    // at runtime. An absolute URL here would bypass that and break any
    // deployment whose backend is not on the page's own origin.
    const all = [
      compositeImagePath({ product: 'comp_ref', step: 0 }),
      scanImagePath({ radar: 'XEBY', moment: 'Reflectivity' }),
      tiltImagePath({ radar: 'XEBY', elevation: 1, moment: 'DBZH', frame: 0 })
    ];
    check('each starts at /api/upstream',
      all.every((p) => p.startsWith('/api/upstream/')), all.join(' | '));
    check('none names a host', all.every((p) => !p.includes('://')));
  }

  console.log('\nthe unit rides on both the frames and the step list:');
  {
    // A millimeter manifest lists different frames at different times from
    // an inch one, so taking the step list from one and the frames from the
    // other picks a step by an index that means something else. On AQPI the
    // millimeter subtree also stopped updating on 2026-09-30, which makes
    // that mismatch look like a seven-week jump rather than an error.
    const base = { product: 'qpe_1hr', step: 3 };
    const plain = compositeImagePath(base);
    check('no unit leaves the parameter off', !plain.includes('unit='), plain);
    const mm = compositeImagePath({ ...base, unit: 'mm' });
    check('a unit is sent', mm.includes('unit=mm'), mm);
    check('and the rest is unchanged',
      mm.replace('&unit=mm', '') === plain, mm);
    check('a null unit is left off',
      !compositeImagePath({ ...base, unit: null }).includes('unit='));
    check('an empty unit is left off',
      !compositeImagePath({ ...base, unit: '' }).includes('unit='));
    const rate = compositeImagePath({ ...base, unit: 'mm/h' });
    check('a rate unit is escaped', rate.includes('unit=mm%2Fh'), rate);

    const steps = compositeStepsPath('qpe_1hr', 'mm');
    check('the step list takes the same unit', steps.includes('unit=mm'),
      steps);
    check('and names the same product', steps.includes('product_id=qpe_1hr'));
    check('the step list without a unit sends none',
      !compositeStepsPath('qpe_1hr').includes('unit='),
      compositeStepsPath('qpe_1hr'));
    check('the step list defaults to no unit',
      compositeStepsPath('qpe_1hr', null) === compositeStepsPath('qpe_1hr'));
    // The pair has to agree, which is the intended result of threading
    // the unit through both calls.
    for (const u of [null, 'in', 'mm', 'mm/h']) {
      const a = new URL(compositeImagePath({ ...base, unit: u }),
        'http://x').searchParams.get('unit');
      const b = new URL(compositeStepsPath('qpe_1hr', u),
        'http://x').searchParams.get('unit');
      check(`unit ${String(u)} matches on both calls`, a === b,
        `${a} vs ${b}`);
    }
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall frame-url assertions passed');
  return failures.length ? 1 : 0;
}
