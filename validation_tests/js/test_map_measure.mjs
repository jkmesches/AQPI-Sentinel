// Distance and bearing between two points a reader clicks on the map.
//
// Run:  node validation_tests/js/run_map_measure.mjs
//
// These are checked against published figures rather than against the
// implementation, because a measurement tool that is self-consistently wrong
// is worse than none: the number looks plausible and nobody re-derives it.
// The reference legs below are real distances between this network's own
// radars and between airports whose separations are documented.
//
// The planar trap is the one to catch. On Web Mercator coordinates a flat
// measurement is long by the secant of the latitude, 27% at 38 degrees
// north, so a tool that measured in projected space would report XSCR to
// XSCW as about 100 km instead of 79.

// Read from /api/radars/meta on 2026-11-21. The first version of this file
// guessed them, which makes a reference leg that only agrees with itself.
const XSCV = { lng: -121.8334, lat: 37.3989 };        // Coyote Valley
const XSCW = { lng: -122.8022, lat: 38.5216 };        // Sonoma
const XSCR = { lng: -121.9786, lat: 36.9847 };        // Santa Cruz
const XEBY = { lng: -122.0620, lat: 37.8156 };        // East Bay
const XSWR = { lng: -122.4229867, lat: 37.5742867 };  // Sawyer Ridge
const CBAND = { lng: -122.7161, lat: 38.0277 };       // Mt Barnabe
const KDAX = { lng: -121.6770, lat: 38.5010 };
const KMUX = { lng: -121.8980, lat: 37.1553 };

// Vincenty's inverse on WGS84, computed 2026-11-21 from those coordinates.
// It is a different algorithm on a different earth model, so agreement is
// evidence, where a haversine checked against a haversine is not. The
// sphere and the ellipsoid differ by a few tenths of a percent over these
// distances. TOLERANCE below allows that much, and no error of sign, unit
// or projection would fit inside it.
const WGS84 = [
  ['XSCV to XSCW', XSCV, XSCW, 150.919, 325.96],
  ['XSCR to XSCW', XSCR, XSCW, 185.380, 337.20],
  ['XSCV to XEBY', XSCV, XEBY, 50.462, 336.49],
  ['XEBY to CBAND', XEBY, CBAND, 62.144, 292.46],
  ['XSWR to KMUX', XSWR, KMUX, 65.765, 134.84],
  ['CBAND to KDAX', CBAND, KDAX, 105.022, 59.66]
];

/** How far the sphere may sit from the ellipsoid, as a fraction. */
const TOLERANCE = 0.005;

/** How far the two models' initial bearings may differ, in degrees. */
const BEARING_TOLERANCE = 0.2;

// Two points one degree of latitude apart: 111.19 km on a sphere of radius
// 6371.0088 km, which is the textbook figure and does not depend on the
// implementation.
const EQ_A = { lng: 0, lat: 0 };
const EQ_B = { lng: 0, lat: 1 };
const EQ_E = { lng: 1, lat: 0 };

export function runTests(mod) {
  const {
    haversineKm, initialBearingDeg, compassPoint, segments, totalKm,
    formatDistance, formatBearing, measureLine, midpoint, isUsable,
    EARTH_RADIUS_KM, KM_PER_MILE, KM_PER_NM, COMPASS
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const near = (a, b, tol) => Number.isFinite(a) && Math.abs(a - b) <= tol;

  console.log('distances match the figures, not just each other:');
  {
    // One degree of latitude on this sphere is pi * R / 180.
    const oneDeg = (Math.PI * EARTH_RADIUS_KM) / 180;
    check('a degree of latitude is 111.19 km',
      near(haversineKm(EQ_A, EQ_B), oneDeg, 1e-6),
      haversineKm(EQ_A, EQ_B).toFixed(4));
    check('a degree of longitude at the equator is the same',
      near(haversineKm(EQ_A, EQ_E), oneDeg, 1e-6),
      haversineKm(EQ_A, EQ_E).toFixed(4));
    // At 38 N a degree of longitude is cos(38) times as long. A planar
    // measurement on Mercator coordinates would return the equator figure.
    const at38 = haversineKm({ lng: 0, lat: 38 }, { lng: 1, lat: 38 });
    check('...and shorter by cos(lat) at 38 north',
      near(at38, oneDeg * Math.cos(38 * Math.PI / 180), 0.02),
      `${at38.toFixed(2)} km against ${oneDeg.toFixed(2)} at the equator`);
    check('a planar measurement would be 27% long there, and is not',
      at38 < oneDeg * 0.8, `${(at38 / oneDeg).toFixed(3)} of the equator`);
  }

  console.log('\nthe network measures as WGS84 says it is laid out:');
  for (const [name, a, b, km, bearing] of WGS84) {
    const got = haversineKm(a, b);
    check(`${name} is ${km} km on the ellipsoid`,
      near(got, km, km * TOLERANCE),
      `${got.toFixed(3)} on the sphere, ${((got / km - 1) * 100).toFixed(2)}%`);
    check(`...and the same measured backwards`,
      near(got, haversineKm(b, a), 1e-9));
    const heading = initialBearingDeg(a, b);
    check(`...leaving on ${bearing} degrees true`,
      near(heading, bearing, BEARING_TOLERANCE),
      heading === null ? 'null' : heading.toFixed(2));
  }

  console.log('\nbearings point the right way:');
  {
    check('due north is 0', near(initialBearingDeg(EQ_A, EQ_B), 0, 1e-9));
    check('due east is 90', near(initialBearingDeg(EQ_A, EQ_E), 90, 1e-9));
    check('due south is 180',
      near(initialBearingDeg(EQ_B, EQ_A), 180, 1e-9));
    check('due west is 270',
      near(initialBearingDeg(EQ_E, EQ_A), 270, 1e-9));
    check('every bearing is in [0, 360)',
      [[XSCV, XSCW], [XSCW, XSCV], [XSCR, XEBY], [XEBY, XSCR]]
        .every(([a, b]) => {
          const d = initialBearingDeg(a, b);
          return d >= 0 && d < 360;
        }));
    // XSCW is north and west of XSCV, so the bearing is in the fourth
    // quadrant. A sign error in the atan2 would put it in the second.
    const nw = initialBearingDeg(XSCV, XSCW);
    check('XSCV to XSCW reads northwest', nw > 270 && nw < 360,
      nw.toFixed(1));
    check('...and the return leg reads southeast',
      initialBearingDeg(XSCW, XSCV) > 90
        && initialBearingDeg(XSCW, XSCV) < 180,
      initialBearingDeg(XSCW, XSCV).toFixed(1));
    // Great-circle bearings are not reciprocal away from the equator, so a
    // test asserting that they are would be asserting a flat earth.
    const out = initialBearingDeg(XSCV, XSCW);
    const back = initialBearingDeg(XSCW, XSCV);
    check('the two legs differ from reciprocal, as a great circle does',
      Math.abs(((out + 180) % 360) - back) > 1e-6,
      `${out.toFixed(2)} out, ${back.toFixed(2)} back`);
  }

  console.log('\ntwo clicks in the same place have no bearing:');
  {
    check('the same point', initialBearingDeg(XSCV, { ...XSCV }) === null);
    check('...and no distance', haversineKm(XSCV, { ...XSCV }) === 0);
    check('a point a centimeter away is still the same click',
      initialBearingDeg(XSCV, { lng: XSCV.lng + 1e-10, lat: XSCV.lat })
        === null);
    check('a point 20 m away does have one',
      initialBearingDeg(XSCV, { lng: XSCV.lng, lat: XSCV.lat + 0.0002 })
        !== null);
    check('no bearing formats as nothing', formatBearing(null) === '');
  }

  console.log('\na bad click cannot poison the total:');
  {
    const bad = [
      { lng: NaN, lat: 37 }, { lng: -122, lat: NaN },
      { lng: Infinity, lat: 37 }, { lng: -122, lat: 95 },
      { lng: -122, lat: -91 }, null, undefined
    ];
    for (const p of bad) {
      check(`${JSON.stringify(p)} is not usable`, isUsable(p) === false);
    }
    check('a good point is usable', isUsable(XSCV) === true);
    check('the poles are usable', isUsable({ lng: 0, lat: 90 }) === true);
    const path = [XSCV, { lng: NaN, lat: 1 }, XSCW];
    check('a bad point is dropped rather than making the total NaN',
      Number.isFinite(totalKm(path)) && totalKm(path) > 100,
      String(totalKm(path)));
    check('...and leaves one segment, not two',
      segments(path).length === 1, String(segments(path).length));
    check('an unusable pair measures as NaN rather than zero',
      Number.isNaN(haversineKm({ lng: NaN, lat: 0 }, XSCV)));
  }

  console.log('\na path adds up:');
  {
    const path = [XSCR, XSCV, XEBY, XSCW];
    const legs = segments(path);
    check('three clicks after the first give three legs',
      legs.length === 3, String(legs.length));
    check('the total is the sum of the legs',
      near(totalKm(path), legs.reduce((s, l) => s + l.km, 0), 1e-9));
    check('each leg carries its own ends',
      legs.every((l, i) => l.from === path[i] && l.to === path[i + 1]));
    check('one point is no distance', totalKm([XSCV]) === 0);
    check('no points is no distance', totalKm([]) === 0);
    check('one point is no segments', segments([XSCV]).length === 0);
  }

  console.log('\nthe labels say what the number is:');
  {
    check('a leg reads in all three units',
      formatDistance(100) === '100 km · 62 mi · 54 nm',
      formatDistance(100));
    check('the mile figure is the kilometers over 1.609344',
      near(100 / KM_PER_MILE, 62.137, 0.001));
    check('the nautical figure is over 1.852',
      near(100 / KM_PER_NM, 53.996, 0.001));
    check('under a hundred keeps a tenth',
      formatDistance(12.34) === '12.3 km · 7.7 mi · 6.7 nm',
      formatDistance(12.34));
    check('under a kilometer keeps two places',
      formatDistance(0.456).startsWith('0.46 km'),
      formatDistance(0.456));
    // One screen pixel at zoom 7 is about 600 m, so three decimals on a
    // 140 km leg would claim an accuracy the click does not have.
    check('above a hundred drops the decimals',
      formatDistance(140.26) === '140 km · 87 mi · 76 nm',
      formatDistance(140.26));
    check('a NaN distance formats as nothing', formatDistance(NaN) === '');
    check('a negative formats as nothing', formatDistance(-5) === '');
    check('zero still formats', formatDistance(0) === '0.00 km · 0.00 mi · 0.00 nm',
      formatDistance(0));
  }

  console.log('\na bearing is three digits and a compass point:');
  {
    check('43 degrees reads as 043', formatBearing(43) === '043° true NE',
      formatBearing(43));
    check('0 reads as 000', formatBearing(0) === '000° true N');
    check('359 keeps its digits', formatBearing(359).startsWith('359°'));
    check('360 wraps to 000', formatBearing(360) === '000° true N',
      formatBearing(360));
    check('359.7 rounds to 000 rather than 360',
      formatBearing(359.7) === '000° true N', formatBearing(359.7));
    check('every compass point is three letters or fewer',
      COMPASS.every((c) => c.length <= 3));
    for (const [deg, want] of [[0, 'N'], [22.5, 'NNE'], [45, 'NE'],
      [90, 'E'], [135, 'SE'], [180, 'S'], [225, 'SW'], [270, 'W'],
      [315, 'NW'], [348.74, 'NNW'], [349, 'N'], [11, 'N'], [12, 'NNE']]) {
      check(`${deg} is ${want}`, compassPoint(deg) === want,
        compassPoint(deg));
    }
    // The sector boundary rounds up, so 348.75 belongs to N and not NNW.
    check('a sector boundary goes to the clockwise point',
      compassPoint(348.75) === 'N', compassPoint(348.75));
    check('a negative bearing still lands on a point',
      compassPoint(-90) === 'W', compassPoint(-90));
    check('no bearing has no point', compassPoint(null) === '');
  }

  console.log('\nthe drawn line matches the measured path:');
  {
    check('two points give a line',
      measureLine([XSCV, XSCW])?.geometry.type === 'LineString');
    check('...with their coordinates in order',
      JSON.stringify(measureLine([XSCV, XSCW]).geometry.coordinates)
        === JSON.stringify([[XSCV.lng, XSCV.lat], [XSCW.lng, XSCW.lat]]));
    check('one point draws nothing', measureLine([XSCV]) === null);
    check('no points draw nothing', measureLine([]) === null);
    check('a bad point is left out of the line',
      measureLine([XSCV, { lng: NaN, lat: 0 }, XSCW])
        .geometry.coordinates.length === 2);
    check('...and one good point left after that draws nothing',
      measureLine([XSCV, { lng: NaN, lat: 0 }]) === null);
  }

  console.log('\na label sits on the line it describes:');
  {
    const m = midpoint(EQ_A, EQ_B);
    check('the midpoint of a meridian leg is halfway up it',
      near(m.lat, 0.5, 1e-9) && near(m.lng, 0, 1e-9),
      `${m.lat} ${m.lng}`);
    const r = midpoint(XSCV, XSCW);
    check('it lies between the ends in latitude',
      r.lat > Math.min(XSCV.lat, XSCW.lat)
        && r.lat < Math.max(XSCV.lat, XSCW.lat));
    check('...and in longitude',
      r.lng > Math.min(XSCV.lng, XSCW.lng)
        && r.lng < Math.max(XSCV.lng, XSCW.lng));
    check('it is the same distance from each end',
      near(haversineKm(XSCV, r), haversineKm(r, XSCW), 1e-6),
      `${haversineKm(XSCV, r).toFixed(4)} and ${haversineKm(r, XSCW).toFixed(4)}`);
    check('a bad end has no midpoint',
      midpoint(XSCV, { lng: NaN, lat: 0 }) === null);
    const anti = midpoint({ lng: 179, lat: 0 }, { lng: -179, lat: 0 });
    check('a leg across the antimeridian stays on it',
      Math.abs(Math.abs(anti.lng) - 180) < 1e-6, String(anti.lng));
  }

  if (failures.length) {
    console.log(`\n${failures.length} failed:`);
    for (const f of failures) console.log(`  ${f}`);
    return 1;
  }
  console.log('\nall measure assertions passed');
  return 0;
}
