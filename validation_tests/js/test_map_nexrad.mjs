// NEXRAD mosaic placement and frame selection.
//
// Run:  node validation_tests/js/run_map_nexrad.mjs
//
// This code drew the one layer an operator treats as ground truth, and it
// had two faults that a screenshot cannot show. A GetMap with no TIME
// returns 200 with a near-empty PNG rather than the newest slice, so the
// live view read as "NEXRAD sees nothing here" while scrubbing back one step
// made the weather appear. And the Mercator latitude term is a logarithm, so
// a linear stand-in still paints something that looks like weather.
//
// The assertions below are about those two: TIME is present on every URL,
// and the projection matches the closed form at known latitudes.

export function runTests(mod) {
  const {
    lon2x, lat2y, unionExtent, nexradLatestIso, nexradImageUrl,
    NEXRAD_WMS, NEXRAD_WIDTH, NEXRAD_HEIGHT, NEXRAD_STEP_MS
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  // AQPI's union box, as config.COMP_EXTENT reports it.
  const BAY = { west: -124.005, east: -121.195, south: 36.5, north: 39.505 };
  const qs = (url) => new URLSearchParams(url.split('?')[1]);

  console.log('the projection is Mercator, not linear:');
  {
    // tan(PI/4) lands one ulp below 1, so the log is -1.1e-16 and the
    // equator comes out about 2 nanometers south of zero. A tolerance
    // rather than an exact zero, because the alternative is a special case
    // in the projection to make a test pass.
    check('the equator is the origin', Math.abs(lat2y(0)) < 1e-6,
      `${lat2y(0).toExponential(2)} m`);
    check('longitude is linear', Math.abs(lon2x(90) - 20037508.34 / 2) < 1e-6,
      String(lon2x(90)));
    check('...and antisymmetric', lon2x(-45) === -lon2x(45));
    check('latitude is antisymmetric', Math.abs(lat2y(-38) + lat2y(38)) < 1e-6);
    // The closed form at 38N. A linear scale would give 38/90 of the half
    // world, 8.46e6, against Mercator's 4.58e6: a 46% error that still
    // renders as a plausible mosaic.
    const linear = (38 / 90) * 20037508.34;
    check('...and curves away from the linear value at 38N',
      Math.abs(lat2y(38) / linear - 0.5413) < 0.001,
      `${lat2y(38).toFixed(0)} vs linear ${linear.toFixed(0)}`);
    check('...growing without bound toward the pole',
      lat2y(85) > lat2y(80) && lat2y(80) > lat2y(38));
  }

  console.log('\nthe union covers every composite:');
  {
    const a = { west: -123, east: -122, south: 37, north: 38 };
    const b = { west: -124.005, east: -121.195, south: 36.5, north: 39.505 };
    const u = unionExtent([a, b], BAY);
    check('west is the smallest', u.west === -124.005, String(u.west));
    check('east is the largest', u.east === -121.195, String(u.east));
    check('south is the smallest', u.south === 36.5, String(u.south));
    check('north is the largest', u.north === 39.505, String(u.north));
    check('one box is its own union',
      JSON.stringify(unionExtent([a], BAY)) === JSON.stringify(a));
    // A profile with no composite extents still has to place the mosaic.
    check('no boxes falls back', unionExtent([], BAY) === BAY);
  }

  console.log('\nTIME is on every request:');
  {
    // The regression this file exists for. `null` means "newest", NOT
    // "omit the parameter".
    const live = nexradImageUrl(BAY, null, Date.UTC(2026, 10, 20, 23, 17, 42));
    check('a null time still sends TIME', qs(live).has('TIME'));
    check('...snapped down to the 5-minute mark',
      qs(live).get('TIME') === '2026-11-20T23:15:00Z', qs(live).get('TIME'));
    const explicit = nexradImageUrl(BAY, '2026-11-20T22:00:00Z');
    check('an explicit time is passed through',
      qs(explicit).get('TIME') === '2026-11-20T22:00:00Z');
    check('...and is not re-snapped',
      qs(nexradImageUrl(BAY, '2026-11-20T22:03:00Z')).get('TIME')
        === '2026-11-20T22:03:00Z');
  }

  console.log('\nthe 5-minute snap:');
  {
    const at = (h, m, s) => nexradLatestIso(Date.UTC(2026, 10, 20, h, m, s));
    check('exactly on a mark stays there', at(23, 15, 0) === '2026-11-20T23:15:00Z',
      at(23, 15, 0));
    check('one second past rounds down', at(23, 15, 1) === '2026-11-20T23:15:00Z');
    check('one second before rounds down', at(23, 14, 59) === '2026-11-20T23:10:00Z');
    check('midnight is a mark', at(0, 0, 0) === '2026-11-20T00:00:00Z', at(0, 0, 0));
    check('just before midnight stays on the old day',
      at(23, 59, 59) === '2026-11-20T23:55:00Z');
    check('the step is five minutes', NEXRAD_STEP_MS === 300000);
    // Never a fractional-second or local-time string: the service indexes
    // slices by this exact spelling.
    check('the shape is ISO 8601 UTC to the second',
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(at(12, 34, 56)), at(12, 34, 56));
  }

  console.log('\nthe GetMap parameters the service needs:');
  {
    const p = qs(nexradImageUrl(BAY, '2026-11-20T22:00:00Z'));
    check('the WMS-T endpoint', nexradImageUrl(BAY, null).startsWith(NEXRAD_WMS));
    check('SERVICE=WMS', p.get('SERVICE') === 'WMS');
    check('VERSION 1.1.1', p.get('VERSION') === '1.1.1', p.get('VERSION'));
    check('REQUEST=GetMap', p.get('REQUEST') === 'GetMap');
    check('the base reflectivity layer',
      p.get('LAYERS') === 'nexrad-n0q-wmst', p.get('LAYERS'));
    check('transparent PNG, so the composite shows through',
      p.get('FORMAT') === 'image/png' && p.get('TRANSPARENT') === 'true');
    // 1.1.1 spells the CRS parameter SRS. VERSION 1.3.0 would need CRS and
    // would also swap the bbox axis order.
    check('SRS, as 1.1.1 spells it', p.get('SRS') === 'EPSG:3857' && !p.has('CRS'));
    check('the requested pixel size',
      p.get('WIDTH') === String(NEXRAD_WIDTH) && p.get('HEIGHT') === String(NEXRAD_HEIGHT),
      `${p.get('WIDTH')}x${p.get('HEIGHT')}`);
  }

  console.log('\nthe bbox is projected, in the order 1.1.1 wants:');
  {
    const p = qs(nexradImageUrl(BAY, '2026-11-20T22:00:00Z'));
    const bbox = p.get('BBOX').split(',').map(Number);
    check('four numbers', bbox.length === 4 && bbox.every(Number.isFinite),
      p.get('BBOX'));
    check('minx, miny, maxx, maxy', bbox[0] < bbox[2] && bbox[1] < bbox[3]);
    check('x from the west edge', Math.abs(bbox[0] - lon2x(BAY.west)) < 1e-6);
    check('y from the south edge', Math.abs(bbox[1] - lat2y(BAY.south)) < 1e-6);
    check('x from the east edge', Math.abs(bbox[2] - lon2x(BAY.east)) < 1e-6);
    check('y from the north edge', Math.abs(bbox[3] - lat2y(BAY.north)) < 1e-6);
    // Degrees in a 3857 bbox would ask for a box a few meters across and
    // return an empty tile, which is the same symptom as the TIME bug.
    check('meters, not degrees', Math.abs(bbox[0]) > 1e6, String(bbox[0]));
  }

  console.log('\nthe URL is stable while the slice is:');
  {
    const t0 = Date.UTC(2026, 10, 20, 23, 15, 1);
    const t1 = Date.UTC(2026, 10, 20, 23, 19, 59);
    check('two times in one 5-minute window give one URL',
      nexradImageUrl(BAY, null, t0) === nexradImageUrl(BAY, null, t1));
    check('...and the next window gives another',
      nexradImageUrl(BAY, null, t1) !== nexradImageUrl(BAY, null, t1 + 1000));
    check('a different extent gives a different URL',
      nexradImageUrl(BAY, null, t0)
        !== nexradImageUrl({ ...BAY, north: 40 }, null, t0));
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall nexrad assertions passed');
  return failures.length ? 1 : 0;
}
