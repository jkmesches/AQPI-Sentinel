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

    console.log('\ncomposite extents are PER PRODUCT, not one box per site:');
    {
      // XQPI's real case: composite_ref sits on a sourced UTM grid while the
      // three QPE families render at a different aspect entirely, so applying
      // one box to all four would place three of them from a fourth's
      // geometry.
      const GOOD = { west: -119.564, east: -117.0, south: 33.2877, north: 35.0267 };
      const r = await run({ comp_extent: { composite_ref: GOOD }, comp_extent_provisional: [] });
      check('the named product gets its box', r.compExtent?.composite_ref?.west === -119.564,
        JSON.stringify(r.compExtent));
      check('a product with no entry has none', !r.compExtent?.qpe_15min);
      check('nothing is flagged provisional', r.compExtentProvisional.length === 0);
    }
    {
      const r = await run({
        comp_extent: { a: { west: -1, east: 1, south: -1, north: 1 },
                       b: { west: 1, east: -1, south: -1, north: 1 } },
        comp_extent_provisional: ['a']
      });
      // One bad entry must cost that product its overlay, not the whole table.
      check('a malformed entry is dropped', !r.compExtent?.b, JSON.stringify(r.compExtent));
      check('...while its valid siblings survive', !!r.compExtent?.a);
      check('provisional is a list of product ids',
        r.compExtentProvisional.length === 1 && r.compExtentProvisional[0] === 'a',
        JSON.stringify(r.compExtentProvisional));
    }
    {
      const r = await run({ version: '0.6.1' });
      check('no extents -> null, so the frontend keeps its own table',
        r.compExtent === null);
    }
    for (const [label, ext] of [
      ['an array',       [1, 2]],
      ['a string',       'everywhere'],
      ['all entries bad', { a: { west: 1, east: 1, south: 1, north: 1 } }],
    ]) {
      const r = await run({ comp_extent: ext });
      check(`${label} -> null`, r.compExtent === null, JSON.stringify(r.compExtent));
    }

    console.log('\nthe deployment identifies itself:');
    {
      const r = await run({ site_name: 'XQPI Sentinel', data_source: 'trinity',
                            has_radarca: false,
                            products: [{ id: 'composite_ref', label: 'Reflectivity' },
                                       { id: 'junk' }] });
      check('name', r.name === 'XQPI Sentinel', r.name);
      check('data source', r.dataSource === 'trinity', r.dataSource);
      check('has_radarca false is honoured', r.hasRadarca === false);
      check('malformed product entries are dropped',
        r.products?.length === 1 && r.products[0].id === 'composite_ref',
        JSON.stringify(r.products));
    }
    {
      const r = await run({ version: '0.6.1' });
      check('an older payload keeps the AQPI identity',
        r.name === 'AQPI Sentinel' && r.dataSource === 'radarca.engr.colostate.edu');
      check('...and assumes a display tier exists, as AQPI has one',
        r.hasRadarca === true);
      check('...with no product override', r.products === null);
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
      : '\nall site assertions passed');
    return failures.length ? 1 : 0;
  })();
}
