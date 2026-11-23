// The range ring's geometry: where it sits and how far around it goes.
//
// A ring bounds the imagery and has to sit at the height the imagery
// reaches at that range. Every point of it is the same distance from
// the dish, which makes the ring one horizontal circle where the beam
// surface it bounds is a cone.

/**
 * Run the assertions against the compiled module.
 *
 * @param {object} mod The module under test.
 * @returns {number} 0 when every assertion held, 1 otherwise.
 */
export function runTests(mod) {
  const { ringHeight, ringPoints, ringStrip, ringFan, RING_SEGMENTS } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  // Mercator is not needed here, so the projector is the identity on
  // the ground and passes the height straight through. That keeps the
  // arithmetic under test rather than MapLibre's projection.
  const flat = (lng, lat, h) => [lng, lat, h];
  const site = { lng: -122, lat: 38, radiusM: 40000, siteM: 0 };
  // The strips are Float32Array, because that is what goes to GL, and
  // float32 keeps about seven digits. A height near 1,841 m lands
  // within 0.0002 of itself and an arc length near 251,327 m within
  // 0.016, so a tolerance of 1e-6 would be testing the buffer's format
  // rather than the geometry.
  const F32_HEIGHT = 1e-2;
  const F32_ARC = 0.05;

  console.log('a ring sits where its beam is at that range:');
  {
    const nominal = ringHeight({ ...site, elevationDeg: null }, 2000);
    check('with no tilt it takes the nominal sheet altitude',
      nominal === 2000, String(nominal));
    const low = ringHeight({ ...site, elevationDeg: 1.5 }, 2000);
    const high = ringHeight({ ...site, elevationDeg: 4.5 }, 2000);
    check('with a tilt it ignores the sheet altitude', low !== 2000,
      String(Math.round(low)));
    // 40000 * tan(1.5) = 1048, plus 94 of curvature under 4/3 earth.
    check('and reads the beam at the ring radius',
      Math.abs(low - 1142) < 2, String(Math.round(low)));
    check('a higher tilt puts the ring higher', high > low,
      `${Math.round(low)} then ${Math.round(high)}`);
    // The curvature term does not depend on the angle, so it is the
    // same addend in both and the ratio falls short of tan's 3.00.
    check('by the ratio the two angles give',
      Math.abs(high / low - 2.84) < 0.02, (high / low).toFixed(2));
    const up = ringHeight({ ...site, siteM: 500, elevationDeg: 1.5 }, 2000);
    check('a radar on a hill lifts its own ring with it',
      Math.abs(up - low - 500) < 1, String(Math.round(up - low)));
  }

  console.log('\nthe circle closes and is a circle:');
  {
    const pts = ringPoints({ ...site, elevationDeg: null });
    check('it has one point per segment plus the repeat',
      pts.length === RING_SEGMENTS + 1, String(pts.length));
    const [fx, fy] = pts[0];
    const [lx, ly] = pts[pts.length - 1];
    check('the last point is the first', Math.abs(fx - lx) < 1e-9
      && Math.abs(fy - ly) < 1e-9, `${fx},${fy} then ${lx},${ly}`);
    // Every point the same distance out, measured on the ground in
    // meters so latitude's squeeze on longitude has to have been
    // applied. A ring built without it is an ellipse.
    const R = 6371000, rad = Math.PI / 180;
    const far = pts.map(([lng, lat]) => {
      const dLat = (lat - site.lat) * rad;
      const dLng = (lng - site.lng) * rad * Math.cos(site.lat * rad);
      return R * Math.hypot(dLat, dLng);
    });
    const lo = Math.min(...far), hi = Math.max(...far);
    check('every point is the ring radius from the dish',
      Math.abs(lo - 40000) < 60 && Math.abs(hi - 40000) < 60,
      `${Math.round(lo)} to ${Math.round(hi)} m`);
    check('so it is round rather than an ellipse', hi - lo < 60,
      `${Math.round(hi - lo)} m across the circle`);
    const north = pts[0];
    check('and it starts at the top of the circle',
      north[1] > site.lat && Math.abs(north[0] - site.lng) < 1e-9,
      JSON.stringify(north));
  }

  console.log('\nthe strip carries the arc length the dashes need:');
  {
    const ring = { ...site, elevationDeg: 2.5 };
    const strip = ringStrip(ring, 2000, flat);
    const n = strip.length / 4;
    check('four numbers per vertex', strip.length % 4 === 0, String(n));
    check('one vertex per point', n === RING_SEGMENTS + 1, String(n));
    const h = ringHeight(ring, 2000);
    const heights = [];
    for (let i = 0; i < n; i++) heights.push(strip[i * 4 + 2]);
    check('every vertex is at the one ring height',
      heights.every((v) => Math.abs(v - h) < F32_HEIGHT),
      String(Math.round(h)));
    const arcs = [];
    for (let i = 0; i < n; i++) arcs.push(strip[i * 4 + 3]);
    check('the arc starts at zero', arcs[0] === 0, String(arcs[0]));
    check('and ends at the circumference',
      Math.abs(arcs[n - 1] - 2 * Math.PI * 40000) < 1,
      `${Math.round(arcs[n - 1])} m`);
    const steps = arcs.slice(1).map((v, i) => v - arcs[i]);
    const spread = Math.max(...steps) - Math.min(...steps);
    check('and climbs evenly, so the dashes are even too',
      spread < F32_ARC, String(spread));
  }

  console.log('\nthe fill is a fan about the dish:');
  {
    const ring = { ...site, elevationDeg: 2.5 };
    const fan = ringFan(ring, 2000, flat);
    const n = fan.length / 3;
    check('a hub plus every rim point', n === RING_SEGMENTS + 2, String(n));
    check('the hub is the radar', fan[0] === site.lng && fan[1] === site.lat,
      `${fan[0]},${fan[1]}`);
    const h = ringHeight(ring, 2000);
    const flatZ = [];
    for (let i = 0; i < n; i++) flatZ.push(fan[i * 3 + 2]);
    // The hub included: the ring is one horizontal circle, so the disc
    // it bounds is horizontal and the hub is not on the ground.
    check('the whole disc is at the ring height',
      flatZ.every((v) => Math.abs(v - h) < F32_HEIGHT),
      String(Math.round(h)));
  }

  console.log('\nthe segment count is held to something drawable:');
  {
    const r = { ...site, elevationDeg: null };
    check('a silly count is clamped up', ringPoints(r, 2).length === 9,
      String(ringPoints(r, 2).length));
    check('and clamped down', ringPoints(r, 99999).length === 1025,
      String(ringPoints(r, 99999).length));
    check('and a missing one falls back to the default',
      ringPoints(r, Number.NaN).length === RING_SEGMENTS + 1);
  }

  console.log('\na ring around a frame placed in Mercator is a Mercator circle:');
  {
    // KDAX at its 200 km range. The NEXRAD frame is a square centered in
    // Web Mercator, so its ring has to be a circle there to meet its edge.
    const merc = { lng: -121.67783, lat: 38.50111, radiusM: 200_000, siteM: 0,
                   elevationDeg: null, mercator: true };
    const y = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
    const pts = ringPoints(merc);
    const x0 = (merc.lng * Math.PI) / 180;
    const radii = pts.map(([lng, lat]) => Math.hypot((lng * Math.PI) / 180 - x0,
                                                       y(lat) - y(merc.lat)));
    const spread = Math.max(...radii) - Math.min(...radii);
    check('every point is one Mercator distance from the dish', spread < 1e-12,
      spread.toExponential(2));
    check('...and that distance is the range at the dish\'s scale',
      Math.abs(radii[0] - 200 / 6371 / Math.cos((merc.lat * Math.PI) / 180)) < 1e-12);
    check('it closes exactly', pts[0][0] === pts.at(-1)[0] && pts[0][1] === pts.at(-1)[1]);
    const flat = ringPoints({ ...merc, mercator: false });
    const northKm = (Math.max(...pts.map((p) => p[1]))
                     - Math.max(...flat.map((p) => p[1]))) * 111.19;
    check('the small-angle circle would sit 2.5 km off its north edge',
      Math.abs(Math.abs(northKm) - 2.5) < 0.05, `${northKm.toFixed(2)} km`);
  }

  if (failures.length) {
    console.log(`\n${failures.length} FAILED: ${failures.join(', ')}`);
    return 1;
  }
  console.log('\nall elevated-ring assertions passed');
  return 0;
}
