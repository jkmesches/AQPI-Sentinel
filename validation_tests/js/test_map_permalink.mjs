// The console's state in a URL, and back out of one.
//
// Run:  node validation_tests/js/run_map_permalink.mjs
//
// A link is opened by someone who was not there when it was made, often on
// another deployment and often months later. Two failures follow from that
// and most of these assertions are about them.
//
// A value the deployment does not publish must not reach the map. A link
// made on AQPI and opened on XQPI names products, radars and moments that
// do not exist there, and a six-month-old link may name a decommissioned
// radar. Unvalidated, a bad product id is a 404 on every frame and a bad
// radar is an overlay that never paints.
//
// The playback position must travel as a timestamp. A link made at step 30
// of 31 and opened an hour later lands on whatever is 30th by then, which
// is a different minute and not the frame the sender was looking at.

const PROFILE_OVERLAYS = ['watersheds', 'reservoirs',
  'stream_gauges', 'burn_scars'];

const AQPI = {
  products: ['comp_ref', 'qpe_1hr', 'qpe_15min'],
  radars: ['XEBY', 'XSCR', 'XSCV', 'CBAND'],
  moments: ['Reflectivity', 'Velocity', 'Differential Reflectivity'],
  overlays: ['watersheds', 'reservoirs', 'stream-gauges',
    'burn-scars', 'terrain', 'terrain-3d', 'nexrad'],
  units: { qpe_1hr: ['in', 'mm'], qpe_15min: ['in', 'mm'] }
};
const XQPI = {
  products: ['composite_ref'],
  radars: ['FLOW'],
  moments: ['Reflectivity', 'Velocity'],
  overlays: [],
  units: {}
};

export function runTests(mod) {
  const {
    encodeLink, decodeLink, emptyLink, hasAny, parseCamera, keepKnown,
    isTimestamp, cameraInRange, stepForTime, STEP_TOLERANCE_MS,
    linkOverlayNames, LINK_OVERLAYS, mergeQuery,
    CAMERA_LIMITS, PARAMS
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const full = {
    product: 'qpe_1hr',
    ts: '2026-11-21T02:32:00',
    radars: ['XEBY', 'XSCR'],
    moment: 'Velocity',
    unit: 'mm',
    overlays: ['watersheds', 'terrain'],
    camera: { lon: -122.6, lat: 37.95, zoom: 7.2, bearing: 0, pitch: 0 }
  };

  console.log('a full state survives the round trip:');
  {
    const back = decodeLink(encodeLink(full), AQPI);
    for (const k of ['product', 'ts', 'moment', 'unit']) {
      check(`${k} comes back`, back[k] === full[k], `${back[k]}`);
    }
    check('radars come back in order',
      back.radars.join(',') === 'XEBY,XSCR', back.radars.join(','));
    check('overlays come back',
      back.overlays.join(',') === 'watersheds,terrain',
      back.overlays.join(','));
    check('the camera comes back',
      JSON.stringify(back.camera) === JSON.stringify(full.camera),
      JSON.stringify(back.camera));
  }

  console.log('\nthe same view always encodes the same way:');
  {
    // So the caller can skip a replaceState that would change nothing.
    check('twice over gives the same string',
      encodeLink(full) === encodeLink(full));
    const reordered = { ...full, overlays: ['watersheds', 'terrain'] };
    check('an equal state gives an equal string',
      encodeLink(reordered) === encodeLink(full));
    check('a different camera gives a different string',
      encodeLink({ ...full, camera: { ...full.camera, zoom: 8 } })
      !== encodeLink(full));
  }

  console.log('\na default view makes a bare link:');
  {
    check('nothing set encodes to nothing', encodeLink(emptyLink()) === '');
    check('and nothing decodes to nothing',
      !hasAny(decodeLink('', AQPI)));
    check('an empty link carries no product',
      decodeLink('', AQPI).product === null);
    // A bare link must not override stored prefs.
    check('a link with only junk carries nothing',
      !hasAny(decodeLink('zzz=1&qqq=2', AQPI)));
    check('one real field is enough to count',
      hasAny(decodeLink('p=qpe_1hr', AQPI)));
  }

  console.log('\nwhat this deployment does not publish is dropped:');
  {
    // The regression. An unvalidated id reaches MapLibre and the request
    // layer, where it is a 404 on every frame.
    const link = encodeLink(full);
    const onXqpi = decodeLink(link, XQPI);
    check('an AQPI product is dropped on XQPI', onXqpi.product === null);
    check('AQPI radars are dropped on XQPI', onXqpi.radars.length === 0);
    check('a moment XQPI does have survives',
      decodeLink('m=Velocity', XQPI).moment === 'Velocity');
    check('AQPI overlays are dropped on XQPI', onXqpi.overlays.length === 0);
    check('but the camera still applies', onXqpi.camera !== null);
    check('and the timestamp still applies', onXqpi.ts === full.ts);
  }

  console.log('\none bad value does not cost the rest of the link:');
  {
    const mixed = decodeLink(
      'p=nosuch&m=Velocity&r=XEBY,GONE,XSCR&o=terrain,atlantis', AQPI);
    check('the bad product is dropped', mixed.product === null);
    check('the good moment survives', mixed.moment === 'Velocity');
    check('the decommissioned radar is dropped, the others kept',
      mixed.radars.join(',') === 'XEBY,XSCR', mixed.radars.join(','));
    check('the invented overlay is dropped',
      mixed.overlays.join(',') === 'terrain', mixed.overlays.join(','));
  }

  console.log('\na list is cleaned rather than trusted:');
  {
    check('duplicates are dropped',
      keepKnown('XEBY,XEBY,XSCR', AQPI.radars).join(',') === 'XEBY,XSCR');
    check('whitespace is trimmed',
      keepKnown(' XEBY , XSCR ', AQPI.radars).join(',') === 'XEBY,XSCR');
    check('empty entries are dropped',
      keepKnown('XEBY,,XSCR,', AQPI.radars).join(',') === 'XEBY,XSCR');
    check('case matters, since these are ids',
      keepKnown('xeby', AQPI.radars).length === 0);
    check('an empty value gives nothing', keepKnown('', AQPI.radars).length === 0);
    check('null gives nothing', keepKnown(null, AQPI.radars).length === 0);
  }

  console.log('\nthe position is a time, never an index:');
  {
    const q = new URLSearchParams(encodeLink(full));
    check('the link carries a timestamp',
      q.get(PARAMS.ts) === '2026-11-21T02:32:00', String(q.get(PARAMS.ts)));
    check('and no step index anywhere',
      !encodeLink(full).includes('step'), encodeLink(full));
    check('a bare integer is not a timestamp', !isTimestamp('30'));
    check('nor is a step-looking string', !isTimestamp('step=30'));
    check('minutes precision is enough',
      isTimestamp('2026-11-21T02:32'));
    check('seconds are fine', isTimestamp('2026-11-21T02:32:00'));
    check('a zone is fine', isTimestamp('2026-11-21T02:32:00Z'));
    check('an offset is fine', isTimestamp('2026-11-21T02:32:00-07:00'));
    check('fractional seconds are fine',
      isTimestamp('2026-11-21T02:32:00.123'));
    check('a date alone is refused', !isTimestamp('2026-11-21'));
    check('a loose date string is refused', !isTimestamp('Nov 21 2026'));
    check('an impossible date is refused', !isTimestamp('2026-13-45T99:99'));
    check('null is refused', !isTimestamp(null));
    check('a bad timestamp drops out of the link',
      decodeLink('t=30', AQPI).ts === null);
  }

  console.log('\na unit only means something with its product:');
  {
    check('with the product, it survives',
      decodeLink('p=qpe_1hr&u=mm', AQPI).unit === 'mm');
    check('without a product, it is dropped',
      decodeLink('u=mm', AQPI).unit === null);
    check('on a product that has no units, it is dropped',
      decodeLink('p=comp_ref&u=mm', AQPI).unit === null);
    check('a unit the product does not have is dropped',
      decodeLink('p=qpe_1hr&u=mm/h', AQPI).unit === null);
    check('a product dropped as unknown takes its unit with it',
      decodeLink('p=nosuch&u=mm', AQPI).unit === null);
  }

  console.log('\nthe camera is parsed and bounded:');
  {
    check('three numbers parse',
      JSON.stringify(parseCamera('-122.6,37.95,7.2'))
      === '{"lon":-122.6,"lat":37.95,"zoom":7.2,"bearing":0,"pitch":0}',
      JSON.stringify(parseCamera('-122.6,37.95,7.2')));
    check('five numbers parse',
      parseCamera('-122.6,37.95,7.2,45,60')?.pitch === 60);
    check('two numbers do not', parseCamera('-122.6,37.95') === null);
    check('four numbers do not', parseCamera('-122.6,37.95,7.2,45') === null);
    check('text does not', parseCamera('here,there,everywhere') === null);
    check('null does not', parseCamera(null) === null);
    check('empty does not', parseCamera('') === null);
    check('NaN does not', parseCamera('NaN,37.95,7.2') === null);
    check('a longitude past 180 does not',
      parseCamera('181,37.95,7.2') === null);
    check('a latitude past the projection limit does not',
      parseCamera(`-122.6,${CAMERA_LIMITS.lat + 0.01},7.2`) === null);
    check('a zoom past 24 does not', parseCamera('-122.6,37.95,25') === null);
    check('a negative zoom does not', parseCamera('-122.6,37.95,-1') === null);
    check('a pitch past 85 does not',
      parseCamera('-122.6,37.95,7.2,0,86') === null);
    check('a negative pitch does not',
      parseCamera('-122.6,37.95,7.2,0,-1') === null);
    check('the limits themselves are allowed',
      parseCamera(`180,${CAMERA_LIMITS.lat},24,0,85`) !== null);
  }

  console.log('\nan out-of-range camera is left out rather than written:');
  {
    const bad = { ...full, camera: { lon: 999, lat: 0, zoom: 7, bearing: 0, pitch: 0 } };
    check('it does not reach the link', !encodeLink(bad).includes('c='),
      encodeLink(bad));
    check('and the rest of the link still encodes',
      encodeLink(bad).includes('p=qpe_1hr'));
    check('cameraInRange agrees', !cameraInRange(bad.camera));
  }

  console.log('\nthe camera is short unless the map is turned:');
  {
    const flat = encodeLink(full);
    check('a level map writes three numbers',
      new URLSearchParams(flat).get(PARAMS.camera)?.split(',').length === 3,
      String(new URLSearchParams(flat).get(PARAMS.camera)));
    const tilted = encodeLink({
      ...full, camera: { ...full.camera, bearing: 45, pitch: 60 }
    });
    check('a turned one writes five',
      new URLSearchParams(tilted).get(PARAMS.camera)?.split(',').length === 5,
      String(new URLSearchParams(tilted).get(PARAMS.camera)));
    const onlyPitch = encodeLink({
      ...full, camera: { ...full.camera, pitch: 60 }
    });
    check('pitch alone is enough to write both',
      new URLSearchParams(onlyPitch).get(PARAMS.camera)?.split(',').length === 5);
    check('trailing zeros are trimmed',
      !new URLSearchParams(flat).get(PARAMS.camera)?.includes('.00'),
      String(new URLSearchParams(flat).get(PARAMS.camera)));
  }

  console.log('\nthe camera keeps enough precision to be the same view:');
  {
    // Five places is about one meter of longitude, well under a map pixel
    // at the deepest zoom this console offers.
    const c = { lon: -122.123456789, lat: 37.987654321, zoom: 11.3456789, bearing: 0, pitch: 0 };
    const back = decodeLink(encodeLink({ ...full, camera: c }), AQPI).camera;
    check('longitude keeps five places',
      Math.abs(back.lon - c.lon) < 1e-5, String(back.lon));
    check('latitude keeps five places',
      Math.abs(back.lat - c.lat) < 1e-5, String(back.lat));
    check('zoom keeps two', Math.abs(back.zoom - c.zoom) < 1e-2,
      String(back.zoom));
  }

  console.log('\na hand-edited link cannot break the reader:');
  {
    for (const q of ['', '?', 'p=', 'r=,,,', 'c=,,', 'c=1,2,3,4,5,6',
      't=', 'u=', 'o=', '%%%', 'p=%E0%A4%A', 'p[]=x']) {
      let threw = null;
      try { decodeLink(q, AQPI); } catch (e) { threw = String(e); }
      check(`${JSON.stringify(q)} is survivable`, threw === null,
        String(threw));
    }
    check('a leading question mark is accepted',
      decodeLink('?p=qpe_1hr', AQPI).product === 'qpe_1hr');
    check('and so is one without',
      decodeLink('p=qpe_1hr', AQPI).product === 'qpe_1hr');
  }

  console.log('\na link\'s time becomes a step, or nothing:');
  {
    // The reason the position travels as a time. A link made at step 30 of
    // 31 and opened an hour later would land on whatever is 30th by then,
    // which is a different minute.
    const steps = [
      { ts: '2026-11-21T02:00:00' },
      { ts: '2026-11-21T02:02:00' },
      { ts: '2026-11-21T02:04:00' },
      { ts: '2026-11-21T02:06:00' }
    ];
    check('an exact time finds its step',
      stepForTime(steps, '2026-11-21T02:04:00', STEP_TOLERANCE_MS) === 2,
      String(stepForTime(steps, '2026-11-21T02:04:00', STEP_TOLERANCE_MS)));
    check('a time between two takes the nearer',
      stepForTime(steps, '2026-11-21T02:04:50', STEP_TOLERANCE_MS) === 2,
      String(stepForTime(steps, '2026-11-21T02:04:50', STEP_TOLERANCE_MS)));
    check('and the other way',
      stepForTime(steps, '2026-11-21T02:05:10', STEP_TOLERANCE_MS) === 3);
    check('the first step is reachable',
      stepForTime(steps, '2026-11-21T01:59:00', STEP_TOLERANCE_MS) === 0);
    check('the last step is reachable',
      stepForTime(steps, '2026-11-21T02:07:00', STEP_TOLERANCE_MS) === 3);
    // A link older than the window lands on nothing rather than on an
    // unrelated hour, which would look like a working link to the wrong
    // weather.
    check('a time a day earlier matches nothing',
      stepForTime(steps, '2026-11-20T02:04:00', STEP_TOLERANCE_MS) === null);
    check('a time a day later matches nothing',
      stepForTime(steps, '2026-11-22T02:04:00', STEP_TOLERANCE_MS) === null);
    check('just inside the tolerance matches',
      stepForTime(steps, '2026-11-21T01:00:01', STEP_TOLERANCE_MS) === 0);
    check('just outside it does not',
      stepForTime(steps, '2026-11-21T00:59:00', STEP_TOLERANCE_MS) === null);
    check('an empty list matches nothing',
      stepForTime([], '2026-11-21T02:04:00', STEP_TOLERANCE_MS) === null);
    check('a null time matches nothing',
      stepForTime(steps, null, STEP_TOLERANCE_MS) === null);
    check('a bad time matches nothing',
      stepForTime(steps, '30', STEP_TOLERANCE_MS) === null);
    check('steps with no time are skipped',
      stepForTime([{ ts: null }, { ts: '2026-11-21T02:04:00' }],
        '2026-11-21T02:04:00', STEP_TOLERANCE_MS) === 1);
    check('a list with no times at all matches nothing',
      stepForTime([{ ts: null }, {}], '2026-11-21T02:04:00',
        STEP_TOLERANCE_MS) === null);
    check('the tolerance is the step window, one hour',
      STEP_TOLERANCE_MS === 3600000, String(STEP_TOLERANCE_MS));
  }

  console.log('\nthe overlay vocabulary is the console\'s, not the profile\'s:');
  {
    // The regression. ALL_OVERLAYS names only the four the profile
    // supplies data for and spells them with underscores, so a link
    // validated against it loses terrain, 3D terrain, NEXRAD and both
    // hyphenated names. A view with five overlays on reopened with two.
    const names = linkOverlayNames(PROFILE_OVERLAYS);
    for (const n of ['watersheds', 'reservoirs', 'stream-gauges',
      'burn-scars', 'terrain', 'terrain-3d', 'nexrad']) {
      check(`${n} is a name a link may carry`, names.includes(n),
        names.join(','));
    }
    check('and the underscored spellings are not',
      !names.includes('stream_gauges') && !names.includes('burn_scars'));
    const link = encodeLink({ ...full, overlays: ['terrain', 'nexrad'] });
    check('terrain and NEXRAD survive a round trip',
      decodeLink(link, { ...AQPI, overlays: names }).overlays.join(',')
      === 'terrain,nexrad',
      decodeLink(link, { ...AQPI, overlays: names }).overlays.join(','));
  }

  console.log('\na profile without an overlay cannot be sent one:');
  {
    const bare = linkOverlayNames([]);
    check('the data overlays drop out', !bare.includes('watersheds')
      && !bare.includes('burn-scars'), bare.join(','));
    check('the ones needing no data stay',
      bare.includes('terrain') && bare.includes('terrain-3d')
      && bare.includes('nexrad'), bare.join(','));
    const only = linkOverlayNames(['burn_scars']);
    check('one supplied overlay is offered', only.includes('burn-scars'));
    check('and its neighbors are not', !only.includes('watersheds'));
    check('every toggle is gated or free',
      Object.values(LINK_OVERLAYS).every(
        (g) => g === null || typeof g === 'string'));
  }

  console.log('\nwriting a link keeps the query it did not write:');
  {
    // ?diag=probe turns on the structural probe the visual baseline
    // reads. Rebuilding the query from the link alone drops it the moment
    // the first view settles, and the harness loses the probe mid-run.
    const link = 'p=qpe_1hr&m=Velocity';
    const merged = mergeQuery('?diag=probe', link);
    check('the foreign parameter survives', merged.includes('diag=probe'),
      merged);
    check('and the link is there too',
      merged.includes('p=qpe_1hr') && merged.includes('m=Velocity'), merged);
    check('an empty link keeps the foreign parameter',
      mergeQuery('?diag=probe', '') === 'diag=probe',
      mergeQuery('?diag=probe', ''));
    check('an empty query gives just the link',
      mergeQuery('', link) === 'p=qpe_1hr&m=Velocity',
      mergeQuery('', link));
    check('both empty gives nothing', mergeQuery('', '') === '');
    // A stale value of ours must not survive, or switching back to a
    // default would leave the old one in the bar forever.
    check('our own stale parameters are replaced',
      mergeQuery('?p=comp_ref&diag=probe', 'p=qpe_1hr')
        .split('&').filter((x) => x.startsWith('p=')).join(',')
      === 'p=qpe_1hr',
      mergeQuery('?p=comp_ref&diag=probe', 'p=qpe_1hr'));
    check('and a parameter we no longer write is dropped',
      !mergeQuery('?p=comp_ref&u=mm', 'p=qpe_1hr').includes('u='),
      mergeQuery('?p=comp_ref&u=mm', 'p=qpe_1hr'));
    check('a question mark is optional',
      mergeQuery('diag=probe', link) === mergeQuery('?diag=probe', link));
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall permalink assertions passed');
  return failures.length ? 1 : 0;
}
