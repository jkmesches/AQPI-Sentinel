// Where the map opens, and which regional overlays it offers.
//
// The map is CONSTRUCTED from this result, so every failure path has to
// degrade to a usable camera rather than to undefined. And the empty-overlay
// case has to survive, because that is a profile saying "I have no Bay Area
// datasets" rather than a missing field — conflating the two is what puts
// Northern California watersheds on a map of Pasadena.

const BAY = { center: [-122.6, 37.95], zoom: 7.2 };
const FLOW = { center: [-118.17081, 34.2048], zoom: 9.5 };

const reply = (body, ok = true) => async () => ({ ok, json: async () => body });
const boom = async () => { throw new Error('network down'); };

export function runTests(mod) {
  const { resolveHomeView, ALL_OVERLAYS } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const run = (body, ok) => resolveHomeView(BAY, reply(body, ok));

  return (async () => {
    console.log('a profile that supplies a home view is honoured:');
    {
      const r = await run({ home_view: FLOW, map_overlays: [] });
      check('centre is the profile\'s', r.home.center[0] === FLOW.center[0]
        && r.home.center[1] === FLOW.center[1], JSON.stringify(r.home.center));
      check('zoom is the profile\'s', r.home.zoom === 9.5, String(r.home.zoom));
      check('an EMPTY overlay list is honoured, not treated as missing',
        r.overlays.length === 0, JSON.stringify(r.overlays));
    }

    console.log('\nAQPI (no home_view in the payload) keeps its tuned view:');
    {
      const r = await run({ version: '0.6.0', map_overlays: [...ALL_OVERLAYS] });
      check('falls back to the Bay Area centre', r.home.center[0] === -122.6);
      check('and its zoom', r.home.zoom === 7.2);
      check('with every overlay offered', r.overlays.length === 3,
        JSON.stringify(r.overlays));
    }
    {
      // An older backend predating the field must not lose its overlays.
      const r = await run({ version: '0.5.6' });
      check('a payload with NO map_overlays key keeps all three',
        r.overlays.length === 3, JSON.stringify(r.overlays));
    }

    console.log('\nevery failure path still yields a usable camera:');
    for (const [label, body, ok] of [
      ['HTTP 500', {}, false],
      ['null home_view', { home_view: null }, true],
      ['home_view not an object', { home_view: 7 }, true],
      ['centre missing', { home_view: { zoom: 9 } }, true],
      ['centre too short', { home_view: { center: [1], zoom: 9 } }, true],
      ['zoom missing', { home_view: { center: [-118, 34] } }, true],
      ['zoom out of range', { home_view: { center: [-118, 34], zoom: 99 } }, true],
      ['NaN in centre', { home_view: { center: [NaN, 34], zoom: 9 } }, true],
    ]) {
      const r = await run(body, ok);
      check(`${label} -> fallback`, r.home.center[0] === -122.6 && r.home.zoom === 7.2,
        JSON.stringify(r.home));
    }
    {
      const r = await resolveHomeView(BAY, boom);
      check('a thrown fetch -> fallback', r.home.zoom === 7.2 && r.overlays.length === 3);
    }

    console.log('\na lat/lon swap is rejected rather than opening mid-ocean:');
    {
      // [34.2, -118.17] instead of [-118.17, 34.2]: latitude -118 is not a
      // place, and MapLibre would silently clamp rather than complain.
      const r = await run({ home_view: { center: [34.2048, -118.17081], zoom: 9.5 } });
      check('swapped pair falls back', r.home.center[0] === -122.6,
        JSON.stringify(r.home.center));
    }
    {
      const r = await run({ home_view: { center: [-118.17081, 34.2048], zoom: 9.5 } });
      check('...while the correct order is accepted', r.home.center[1] === 34.2048);
    }

    console.log('\nthe composite extent, and whether it is sourced:');
    {
      const EXT = { west: -118.609, east: -117.733, south: 33.840, north: 34.570 };
      const r = await run({ comp_extent: EXT, comp_extent_provisional: true });
      check('a valid extent is carried through', r.compExtent?.west === -118.609,
        JSON.stringify(r.compExtent));
      check('and flagged provisional', r.compExtentProvisional === true);
    }
    {
      const r = await run({ version: '0.6.0' });
      check('no extent -> null, so the per-product table is kept',
        r.compExtent === null);
      check('...and nothing is flagged provisional', r.compExtentProvisional === false);
    }
    {
      // An unlabelled box from an unknown source is not evidence of a survey.
      const r = await run({ comp_extent: { west: -1, east: 1, south: -1, north: 1 } });
      check('an extent with NO provisional flag defaults to provisional',
        r.compExtentProvisional === true);
    }
    {
      const r = await run({ comp_extent: { west: -1, east: 1, south: -1, north: 1 },
                            comp_extent_provisional: false });
      check('...and an explicit false is honoured', r.compExtentProvisional === false);
    }
    for (const [label, ext] of [
      ['reversed longitude', { west: 1, east: -1, south: -1, north: 1 }],
      ['reversed latitude',  { west: -1, east: 1, south: 1, north: -1 }],
      ['zero area',          { west: 1, east: 1, south: 1, north: 1 }],
      ['out of range',       { west: -200, east: 1, south: -1, north: 1 }],
      ['NaN edge',           { west: NaN, east: 1, south: -1, north: 1 }],
      ['missing edge',       { west: -1, east: 1, south: -1 }],
      ['not an object',      'everywhere'],
    ]) {
      const r = await run({ comp_extent: ext, comp_extent_provisional: true });
      // A reversed or zero-area box renders mirrored or invisible rather than
      // erroring, so it must be rejected here, and rejecting it must also
      // clear the provisional flag — there is no box left to caveat.
      check(`${label} -> rejected`, r.compExtent === null && !r.compExtentProvisional,
        JSON.stringify(r.compExtent));
    }

    console.log('\nunknown overlay names are dropped, not passed through:');
    {
      const r = await run({ map_overlays: ['watersheds', 'tidal_gauges', 'reservoirs'] });
      check('only known names survive',
        JSON.stringify([...r.overlays]) === JSON.stringify(['watersheds', 'reservoirs']),
        JSON.stringify(r.overlays));
    }

    console.log(failures.length
      ? `\n${failures.length} FAILED: ${failures.join(', ')}`
      : '\nall home-view assertions passed');
    return failures.length ? 1 : 0;
  })();
}
