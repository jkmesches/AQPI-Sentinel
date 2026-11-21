// Radar imagery drawn in the air rather than painted onto the ground.
//
// Run:  node validation_tests/js/run_map_elevated_raster.mjs
//
// MapLibre drapes a raster layer onto the terrain mesh, so a composite
// followed the hillsides and a reader saw rain lying on Mount Diablo.
// The sheet that replaces it is one quad at an altitude, and the ways
// a quad goes wrong are all here: corners in the wrong order, which
// mirrors or shears the imagery; the altitude silently lost, which
// puts it back on the ground; and a degenerate extent drawn as a line.

export function runTests(mod) {
  const {
    clampAltitude, isDrawable, sheetVertices, clearance,
    MIN_ALTITUDE_M, MAX_ALTITUDE_M, DEFAULT_ALTITUDE_M
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  // A square over the Bay, in the ring order an image source takes:
  // top left, top right, bottom right, bottom left.
  const SQUARE = [
    { lng: -123, lat: 39 },
    { lng: -121, lat: 39 },
    { lng: -121, lat: 37 },
    { lng: -123, lat: 37 }
  ];
  // Identity-ish projector: x and y from the ground, z from altitude,
  // so the geometry can be checked without a browser.
  const flat = (lng, lat, alt) => [lng, lat, alt];
  const verts = (corners, alt) => {
    const raw = sheetVertices(corners, alt, flat);
    const out = [];
    for (let i = 0; i < raw.length; i += 5) {
      out.push({ x: raw[i], y: raw[i + 1], z: raw[i + 2],
                 u: raw[i + 3], v: raw[i + 4] });
    }
    return out;
  };

  console.log('the sheet sits at a usable altitude:');
  {
    check('the default is inside the range',
      DEFAULT_ALTITUDE_M >= MIN_ALTITUDE_M
      && DEFAULT_ALTITUDE_M <= MAX_ALTITUDE_M, String(DEFAULT_ALTITUDE_M));
    // Zero is the ground, which is the behavior this replaces, so the
    // floor has to sit above it or the control can undo the fix.
    check('the floor is above the ground', MIN_ALTITUDE_M > 0,
      String(MIN_ALTITUDE_M));
    // Bay Area terrain tops out near 1.2 km, so a default below that
    // would bury the sheet in the hills it is meant to float over.
    check('the default clears this network\'s terrain',
      DEFAULT_ALTITUDE_M > 1200, String(DEFAULT_ALTITUDE_M));
    check('the ceiling is above any echo top here',
      MAX_ALTITUDE_M >= 12000, String(MAX_ALTITUDE_M));
    check('an altitude in range is kept', clampAltitude(3000) === 3000);
    check('too low comes up to the floor',
      clampAltitude(0) === MIN_ALTITUDE_M);
    check('a negative comes up to the floor',
      clampAltitude(-500) === MIN_ALTITUDE_M);
    check('too high comes down to the ceiling',
      clampAltitude(1e9) === MAX_ALTITUDE_M);
    check('NaN falls back to the default',
      clampAltitude(NaN) === DEFAULT_ALTITUDE_M);
    check('infinity falls back to the default',
      clampAltitude(Infinity) === DEFAULT_ALTITUDE_M);
  }

  console.log('\na malformed extent is not drawn:');
  {
    check('a real square is drawable', isDrawable(SQUARE));
    check('a NaN corner is not',
      !isDrawable([{ lng: NaN, lat: 39 }, ...SQUARE.slice(1)]));
    check('a corner past the pole is not',
      !isDrawable([{ lng: -123, lat: 91 }, ...SQUARE.slice(1)]));
    check('a corner past the meridian is not',
      !isDrawable([{ lng: -181, lat: 39 }, ...SQUARE.slice(1)]));
    // A collapsed extent costs a GL call every frame and draws a line
    // where the reader expects imagery.
    check('zero width is not drawable',
      !isDrawable(SQUARE.map((c) => ({ lng: -122, lat: c.lat }))));
    check('zero height is not drawable',
      !isDrawable(SQUARE.map((c) => ({ lng: c.lng, lat: 38 }))));
    check('a single point is not drawable',
      !isDrawable(SQUARE.map(() => ({ lng: -122, lat: 38 }))));
  }

  console.log('\nthe quad covers the extent, once:');
  {
    const v = verts(SQUARE, 2000);
    check('it is two triangles', v.length === 6);
    check('every vertex is at the altitude',
      v.every((p) => p.z === 2000), String(v[0].z));
    // The altitude is the purpose of the layer. A sheet at z = 0 is
    // the draped behavior again, and it looks identical from straight
    // above.
    check('the altitude is never silently dropped',
      v.every((p) => p.z !== 0));
    const corners = new Set(v.map((p) => `${p.x},${p.y}`));
    check('all four corners appear', corners.size === 4,
      [...corners].join(' '));
    check('it spans the full extent',
      Math.min(...v.map((p) => p.x)) === -123
      && Math.max(...v.map((p) => p.x)) === -121
      && Math.min(...v.map((p) => p.y)) === 37
      && Math.max(...v.map((p) => p.y)) === 39);
    // The shared diagonal is drawn twice and no other corner is, which
    // is what says the two triangles tile the quad rather than overlap.
    const seen = new Map();
    for (const p of v) {
      const k = `${p.x},${p.y}`;
      seen.set(k, (seen.get(k) ?? 0) + 1);
    }
    check('exactly two corners are shared by both triangles',
      [...seen.values()].filter((n) => n === 2).length === 2,
      [...seen.values()].join(' '));
  }

  console.log('\nthe texture is not mirrored or sheared:');
  {
    const v = verts(SQUARE, 2000);
    const uvAt = (lng, lat) => {
      const hit = v.find((p) => p.x === lng && p.y === lat);
      return hit ? `${hit.u},${hit.v}` : 'missing';
    };
    // An image runs downward from its top left, so the north-west
    // corner of the extent carries v = 0. Getting this pair wrong
    // flips the imagery and nothing else about the draw changes.
    check('north west is the image top left', uvAt(-123, 39) === '0,0');
    check('north east is the image top right', uvAt(-121, 39) === '1,0');
    check('south east is the image bottom right', uvAt(-121, 37) === '1,1');
    check('south west is the image bottom left', uvAt(-123, 37) === '0,1');
    check('every u and v is a corner value',
      v.every((p) => (p.u === 0 || p.u === 1) && (p.v === 0 || p.v === 1)));
  }

  console.log('\nthe altitude is clamped on the way into the geometry:');
  {
    check('a buried altitude is lifted to the floor',
      verts(SQUARE, -100)[0].z === MIN_ALTITUDE_M);
    check('a NaN altitude becomes the default',
      verts(SQUARE, NaN)[0].z === DEFAULT_ALTITUDE_M);
    check('an absurd altitude is capped',
      verts(SQUARE, 1e12)[0].z === MAX_ALTITUDE_M);
  }

  console.log('\nclearance says when the sheet is buried:');
  {
    check('well above the hills is positive',
      clearance(2000, 1200) === 800);
    check('level with them is zero', clearance(1200, 1200) === 0);
    // A sheet under the terrain is hidden by the hills it should float
    // over, and a reader sees that as the layer having failed.
    check('below them is negative', clearance(500, 1200) === -700);
    check('unknown terrain reports the altitude itself',
      clearance(2000, NaN) === 2000);
    check('it clamps the altitude first',
      clearance(-100, 0) === MIN_ALTITUDE_M);
  }

  console.log(failures.length === 0
    ? '\nall elevated-raster assertions passed'
    : `\n${failures.length} failed: ${failures.join(', ')}`);
  return failures.length === 0 ? 0 : 1;
}
