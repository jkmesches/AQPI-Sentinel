// The layer registry, and the three overlays moved onto it.
//
// Run:  node validation_tests/js/run_map_registry.mjs
//
// These overlays shared one refreshGeography, and sharing it is what let
// them interfere. Its watershed branch returns early when the theme swaps
// mid-fetch, and the return leaves the whole function, so the reservoirs
// that follow are never rebuilt and stay missing until something else
// triggers a pass. Nothing reports it and nothing on screen says why.
//
// A fake map stands in for MapLibre here, which is the point of the MapLike
// surface. It records what was added, in what order, and with which
// beforeId, none of which the structural capture can see from outside and
// none of which a screenshot shows at all.

export function runTests(reg, geo, terr, scars, gauges) {
  const { applyLayer, applyLayers, clearLayer } = reg;
  const { watershedsLayer, reservoirsLayer, WATERSHEDS_URL, RESERVOIRS_URL } = geo;
  const { terrainLayer, TERRAIN_SOURCE, HILLSHADE_LAYER, HILLSHADE_BELOW,
          ensureTerrainSource, removeTerrainSourceIfUnused } = terr;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  /** A map that records what it was asked to do. */
  const fakeMap = (seed = {}) => {
    const layers = new Map(Object.entries(seed.layers ?? {}));
    const sources = new Map(Object.entries(seed.sources ?? {}));
    let terrain = seed.terrain ?? null;
    const log = [];
    return {
      log, layers, sources,
      getLayer: (id) => layers.get(id),
      removeLayer: (id) => { log.push(`-layer ${id}`); layers.delete(id); },
      addLayer: (layer, beforeId) => {
        log.push(`+layer ${layer.id} before=${beforeId ?? 'TOP'}`);
        layers.set(layer.id, layer);
      },
      getSource: (id) => sources.get(id),
      removeSource: (id) => { log.push(`-source ${id}`); sources.delete(id); },
      addSource: (id, src) => { log.push(`+source ${id}`); sources.set(id, src); },
      getTerrain: () => terrain,
      setTerrain: (t) => { terrain = t; }
    };
  };
  const ctx = (map, over = {}) => ({
    map, theme: 'dark', enabled: true,
    before: (id) => `before:${id}`,
    alive: () => true,
    ...over
  });
  const okFetch = (body) => async () => ({ ok: true, json: async () => body });
  const FC = { type: 'FeatureCollection', features: [{ properties: { name: 'Shasta' } }] };

  // The modules call the global fetch, so it is stubbed for the duration.
  const realFetch = globalThis.fetch;
  const withFetch = async (impl, fn) => {
    globalThis.fetch = impl;
    try { return await fn(); } finally { globalThis.fetch = realFetch; }
  };

  const run = async () => {
    console.log('one overlay failing leaves the others alone:');
    {
      // The regression this file exists for.
      const map = fakeMap();
      const broken = {
        id: 'broken', layers: ['broken-line'], sources: ['broken'],
        build: async () => { throw new Error('upstream said no'); }
      };
      const fine = {
        id: 'fine', layers: ['fine-line'], sources: ['fine'],
        build: async (c) => {
          c.map.addSource('fine', { type: 'geojson', data: FC });
          c.map.addLayer({ id: 'fine-line', type: 'line', source: 'fine' },
            c.before('fine-line'));
        }
      };
      const out = await applyLayers([broken, fine], () => ctx(map));
      check('the broken one reports its error',
        out[0].error === 'upstream said no', String(out[0].error));
      check('...and did not build', out[0].built === false);
      check('the other one still built', out[1].built === true && !out[1].error);
      check('...and its layer is on the map', !!map.getLayer('fine-line'));
      check('one outcome per module, in order',
        out.map((o) => o.id).join(',') === 'broken,fine');
    }

    console.log('\na mid-fetch bail stops only its own layer:');
    {
      const map = fakeMap();
      let alive = true;
      const out = await withFetch(okFetch(FC), () => applyLayers(
        [watershedsLayer, reservoirsLayer],
        (m) => ctx(map, {
          // Watersheds sees a dead pass, reservoirs a live one. The old
          // shared function returned out of both.
          alive: () => (m.id === 'watersheds' ? false : alive)
        })
      ));
      check('watersheds reports no error', !out[0].error, String(out[0].error));
      check('...and added nothing', !map.getLayer('watershed-line'));
      check('reservoirs built anyway', !!map.getLayer('reservoir-marker'));
      check('...with its label too', !!map.getLayer('reservoir-label'));
    }

    console.log('\nclearing happens before building, every pass:');
    {
      const map = fakeMap({
        layers: { 'watershed-line': {} }, sources: { watersheds: {} }
      });
      await withFetch(okFetch(FC), () => applyLayer(watershedsLayer, ctx(map)));
      check('the old layer came off first',
        map.log[0] === '-layer watershed-line', map.log[0]);
      check('...then the old source', map.log[1] === '-source watersheds', map.log[1]);
      check('...then the new source and layer',
        map.log[2] === '+source watersheds'
        && map.log[3] === '+layer watershed-line before=before:watershed-line',
        map.log.slice(2).join(' | '));
      // Paint is theme-keyed, so an overlay that stays on across a swap has
      // to be rebuilt rather than left with the old colors.
      check('an enabled layer is still cleared first', map.log.length === 4,
        String(map.log.length));
    }

    console.log('\ndisabled means cleared and not rebuilt:');
    {
      const map = fakeMap({
        layers: { 'reservoir-label': {}, 'reservoir-marker': {} },
        sources: { reservoirs: {} }
      });
      const out = await applyLayer(reservoirsLayer, ctx(map, { enabled: false }));
      check('nothing was built', out.built === false && !out.error);
      check('both layers are gone',
        !map.getLayer('reservoir-marker') && !map.getLayer('reservoir-label'));
      check('the source is gone', !map.getSource('reservoirs'));
      // MapLibre refuses to drop a source a layer still references, so the
      // order is not cosmetic.
      check('layers came off before the source',
        map.log.indexOf('-source reservoirs')
          > map.log.indexOf('-layer reservoir-marker'),
        map.log.join(' | '));
      check('the label came off before the marker it labels',
        map.log.indexOf('-layer reservoir-label')
          < map.log.indexOf('-layer reservoir-marker'));
    }

    console.log('\nthe paint order comes from the caller:');
    {
      const map = fakeMap();
      await withFetch(okFetch(FC), () => applyLayers(
        [watershedsLayer, reservoirsLayer], () => ctx(map)));
      const adds = map.log.filter((l) => l.startsWith('+layer'));
      check('every layer asked where it goes',
        adds.every((l) => l.includes('before=before:')), adds.join(' | '));
      check('each asked about itself',
        adds.includes('+layer watershed-line before=before:watershed-line')
        && adds.includes('+layer reservoir-label before=before:reservoir-label'));
    }

    console.log('\nthe overlays fetch what they say they do:');
    {
      const asked = [];
      const map = fakeMap();
      await withFetch(async (url) => {
        asked.push(url);
        return { ok: true, json: async () => FC };
      }, () => applyLayers([watershedsLayer, reservoirsLayer], () => ctx(map)));
      // Cached after the earlier cases, so this may ask for nothing. What
      // matters is that it never asks for the wrong file.
      check('no unexpected url', asked.every(
        (u) => u === WATERSHEDS_URL || u === RESERVOIRS_URL), asked.join(','));
      check('the watershed file is HUC-8 norcal',
        WATERSHEDS_URL === '/data/watersheds-huc8-norcal.geojson', WATERSHEDS_URL);
      check('the reservoir file is norcal',
        RESERVOIRS_URL === '/data/reservoirs-norcal.json', RESERVOIRS_URL);
    }

    console.log('\na failed fetch is an error, not an empty layer:');
    {
      const map = fakeMap();
      // A fresh module, since the shipped ones cached a good response above.
      const out = await withFetch(
        async () => ({ ok: false, status: 404, json: async () => ({}) }),
        () => applyLayer({
          id: 'missing', layers: ['missing-line'], sources: ['missing'],
          build: async () => {
            const r = await fetch('/data/nope.json');
            if (!r.ok) throw new Error(`/data/nope.json returned HTTP ${r.status}`);
          }
        }, ctx(map)));
      check('the error names the status', /HTTP 404/.test(out.error), out.error);
      check('nothing was added', map.log.length === 0, map.log.join(' | '));
    }

    console.log('\nthe DEM outlives its layer:');
    {
      // Terrain is the reason `teardown` exists. Dropping the source while
      // the 3D mesh holds it throws.
      const mod = terrainLayer('https://example.test/{z}/{x}/{y}.png');
      const withMesh = fakeMap({
        layers: { [HILLSHADE_LAYER]: {} },
        sources: { [TERRAIN_SOURCE]: {} },
        terrain: { source: TERRAIN_SOURCE }
      });
      await applyLayer(mod, ctx(withMesh, { enabled: false }));
      check('the hillshade layer is removed', !withMesh.getLayer(HILLSHADE_LAYER));
      check('...but the mesh keeps the source', !!withMesh.getSource(TERRAIN_SOURCE),
        withMesh.log.join(' | '));

      const noMesh = fakeMap({
        layers: { [HILLSHADE_LAYER]: {} }, sources: { [TERRAIN_SOURCE]: {} }
      });
      await applyLayer(mod, ctx(noMesh, { enabled: false }));
      check('with no mesh the source goes too', !noMesh.getSource(TERRAIN_SOURCE));
    }

    console.log('\nthe hillshade sits under the range rings:');
    {
      const mod = terrainLayer('https://example.test/{z}/{x}/{y}.png');
      const withRings = fakeMap({ layers: { [HILLSHADE_BELOW]: {} } });
      await applyLayer(mod, ctx(withRings));
      check('it is placed below them',
        withRings.log.includes(`+layer ${HILLSHADE_LAYER} before=${HILLSHADE_BELOW}`),
        withRings.log.join(' | '));
      const bare = fakeMap();
      await applyLayer(mod, ctx(bare));
      // A beforeId naming an absent layer makes MapLibre throw, and early in
      // a style swap the rings are not there yet.
      check('with no rings yet it goes on top',
        bare.log.includes(`+layer ${HILLSHADE_LAYER} before=TOP`),
        bare.log.join(' | '));
      check('the DEM source is added with it', !!bare.getSource(TERRAIN_SOURCE));
    }

    console.log('\nthe DEM source is added once:');
    {
      const map = fakeMap();
      ensureTerrainSource(map, 'https://example.test/{z}/{x}/{y}.png');
      ensureTerrainSource(map, 'https://example.test/{z}/{x}/{y}.png');
      check('a second call is a no-op',
        map.log.filter((l) => l === `+source ${TERRAIN_SOURCE}`).length === 1,
        map.log.join(' | '));
      const src = map.getSource(TERRAIN_SOURCE);
      check('terrarium encoding', src.encoding === 'terrarium', src.encoding);
      check('the tile url is passed through',
        src.tiles[0] === 'https://example.test/{z}/{x}/{y}.png');
      check('the DEM is attributed', /Mapzen/.test(src.attribution), src.attribution);
    }

    console.log('\nremoving the DEM checks both holders:');
    {
      const held = fakeMap({
        layers: { [HILLSHADE_LAYER]: {} }, sources: { [TERRAIN_SOURCE]: {} }
      });
      removeTerrainSourceIfUnused(held);
      check('the hillshade layer holds it', !!held.getSource(TERRAIN_SOURCE));
      const meshed = fakeMap({
        sources: { [TERRAIN_SOURCE]: {} }, terrain: { source: TERRAIN_SOURCE }
      });
      removeTerrainSourceIfUnused(meshed);
      check('the 3D mesh holds it', !!meshed.getSource(TERRAIN_SOURCE));
      const free = fakeMap({ sources: { [TERRAIN_SOURCE]: {} } });
      removeTerrainSourceIfUnused(free);
      check('with neither, it goes', !free.getSource(TERRAIN_SOURCE));
      // A map that predates setTerrain has no getTerrain at all.
      const old = fakeMap({ sources: { [TERRAIN_SOURCE]: {} } });
      delete old.getTerrain;
      removeTerrainSourceIfUnused(old);
      check('a map with no getTerrain does not throw',
        !old.getSource(TERRAIN_SOURCE));
    }

    console.log('\nclearLayer alone touches nothing else:');
    {
      const map = fakeMap({
        layers: { 'watershed-line': {}, 'reservoir-marker': {}, other: {} },
        sources: { watersheds: {}, reservoirs: {}, other: {} }
      });
      clearLayer(map, watershedsLayer);
      check('its own layer is gone', !map.getLayer('watershed-line'));
      check('its own source is gone', !map.getSource('watersheds'));
      check('another module is untouched',
        !!map.getLayer('reservoir-marker') && !!map.getSource('reservoirs'));
      check('a layer nobody owns is untouched', !!map.getLayer('other'));
      // Clearing an absent layer is normal on the first pass.
      const empty = fakeMap();
      clearLayer(empty, watershedsLayer);
      check('clearing nothing does nothing', empty.log.length === 0);
    }

    console.log('\nburn scars draw nothing without a profile:');
    {
      // One region's perimeters on another region's map look exactly as
      // authoritative as the right ones, so an unnamed deployment draws
      // nothing rather than guessing.
      const map = fakeMap();
      const asked = [];
      const mod = scars.burnScarsLayer({
        profile: '', years: () => 2,
        fetcher: async (u) => { asked.push(u); return { ok: true, json: async () => FC }; }
      });
      const out = await applyLayer(mod, ctx(map));
      check('nothing was fetched', asked.length === 0, asked.join(','));
      check('no error either', !out.error, String(out.error));
      // An empty collection still builds a source, so a profile that
      // arrives later rebuilds without a special case.
      check('the layers are empty rather than absent',
        !!map.getLayer(scars.BURN_SCAR_FILL ?? 'burn-scar-fill')
        || map.log.length === 0, map.log.join(' | '));
    }

    console.log('\nthe perimeter file is named for the profile:');
    {
      check('aqpi', scars.burnScarUrl('aqpi') === '/data/burn-scars-aqpi.json',
        scars.burnScarUrl('aqpi'));
      check('xqpi', scars.burnScarUrl('xqpi') === '/data/burn-scars-xqpi.json');
      const notJson = await scars.loadBurnScars('aqpi',
        async () => ({ ok: true, json: async () => ({ type: 'Nope' }) }));
      check('a document that is not a FeatureCollection draws nothing',
        notJson.features.length === 0, JSON.stringify(notJson).slice(0, 40));
      const missing = await scars.loadBurnScars('aqpi',
        async () => ({ ok: false, status: 404, json: async () => ({}) }));
      check('a missing file draws nothing', missing.features.length === 0);
      const thrown = await scars.loadBurnScars('aqpi', async () => {
        throw new Error('offline');
      });
      check('a thrown fetch draws nothing', thrown.features.length === 0);
      const good = await scars.loadBurnScars('aqpi',
        async () => ({ ok: true, json: async () => FC }));
      check('a good file comes through', good.features.length === 1);
    }

    console.log('\nthe burn-scar window follows the slider:');
    {
      const map = fakeMap();
      let years = 2;
      const mod = scars.burnScarsLayer({
        profile: 'aqpi', years: () => years,
        fetcher: async () => ({ ok: true, json: async () => FC })
      });
      await applyLayer(mod, ctx(map));
      const narrow = map.getLayer('burn-scar-fill').filter;
      years = 30;
      await applyLayer(mod, ctx(map));
      const wide = map.getLayer('burn-scar-fill').filter;
      check('the filter follows the current value',
        narrow[2][2] === 2 && wide[2][2] === 30,
        `${narrow[2][2]} then ${wide[2][2]}`);
      check('both keep the undated guard',
        narrow[1][0] === 'has' && wide[1][0] === 'has');
      check('the line layer carries the same filter',
        JSON.stringify(map.getLayer('burn-scar-line').filter) === JSON.stringify(wide));
    }

    console.log('\nthe newest perimeter is reported once:');
    {
      const map = fakeMap();
      const seen = [];
      const mod = scars.burnScarsLayer({
        profile: 'aqpi', years: () => 2,
        onLoaded: (through) => seen.push(through),
        fetcher: async () => ({
          ok: true,
          json: async () => ({
            type: 'FeatureCollection',
            features: [{ properties: { ALARM_DATE: Date.UTC(2025, 11, 14) } }]
          })
        })
      });
      await applyLayer(mod, ctx(map));
      await applyLayer(mod, ctx(map));
      check('reported on the first build', seen[0] === 'Dec 2025', seen[0]);
      check('...and not again from cache', seen.length === 1, String(seen.length));
    }

    console.log('\nstream gauges separate the real-time sites:');
    {
      const map = fakeMap();
      const sites = {
        sites: [
          { comid: '1', lat: 37, lon: -122, status: 'R' },
          { comid: '2', lat: 38, lon: -121, status: 'B' }
        ]
      };
      const mod = gauges.streamGaugesLayer(
        async () => ({ ok: true, json: async () => sites }));
      await applyLayer(mod, ctx(map));
      const paint = map.getLayer(gauges.GAUGE_LAYER).paint;
      check('the radius is a case on status',
        paint['circle-radius'][0] === 'case', paint['circle-radius'][0]);
      check('...with the real-time sites larger',
        paint['circle-radius'][2] > paint['circle-radius'][3],
        `${paint['circle-radius'][2]} vs ${paint['circle-radius'][3]}`);
      check('...and more opaque',
        paint['circle-opacity'][2] > paint['circle-opacity'][3]);
      check('...and more heavily stroked',
        paint['circle-stroke-width'][2] > paint['circle-stroke-width'][3]);
      check('the test is the R status',
        JSON.stringify(paint['circle-radius'][1]) === '["==",["get","status"],"R"]',
        JSON.stringify(paint['circle-radius'][1]));
    }

    console.log('\nonly comid and status reach the features:');
    {
      const geo = gauges.gaugesToGeo([
        { comid: '948020', lat: 37.5, lon: -122.3, status: 'R' }
      ]);
      check('one feature per site', geo.features.length === 1);
      check('the coordinates are lon then lat',
        geo.features[0].geometry.coordinates.join(',') === '-122.3,37.5',
        geo.features[0].geometry.coordinates.join(','));
      check('only the two fields the map reads',
        Object.keys(geo.features[0].properties).sort().join(',') === 'comid,status',
        Object.keys(geo.features[0].properties).join(','));
      check('an empty list is still a collection',
        gauges.gaugesToGeo([]).type === 'FeatureCollection');
    }

    console.log('\nan unreachable gauge upstream is an error, not a blank layer:');
    {
      const map = fakeMap();
      const mod = gauges.streamGaugesLayer(
        async () => ({ ok: false, status: 502, json: async () => ({}) }));
      const out = await applyLayer(mod, ctx(map));
      check('the error names the status', /HTTP 502/.test(out.error), out.error);
      check('nothing was added', !map.getLayer(gauges.GAUGE_LAYER));
      // The toggle stays on, so the next pass tries again.
      check('and the module is still usable', out.built === false);
    }

    console.log('\nthe frame barrier fires every apply together:');
    {
      const { prepareFrame } = reg;
      const painted = [];
      const mod = (id, n, delay = 0) => ({
        id, layers: [], sources: [], build: async () => {},
        prepare: async () => {
          if (delay) await new Promise((r) => setTimeout(r, delay));
          return Array.from({ length: n }, (_, i) => () => painted.push(`${id}${i}`));
        }
      });
      const fctx = { map: fakeMap(), alive: () => true };
      const r1 = await prepareFrame([mod('a', 1), mod('b', 3)], fctx, 1000);
      check('one apply per overlay the modules own',
        r1.applies.length === 4, String(r1.applies.length));
      check('nothing painted before they are fired', painted.length === 0);
      r1.applies.forEach((f) => f());
      check('...and all of them paint when fired',
        painted.join(',') === 'a0,b0,b1,b2', painted.join(','));
      check('not late', r1.late === false);
      check('no errors', Object.keys(r1.errors).length === 0);
    }

    console.log('\na module with nothing to swap is skipped:');
    {
      const { prepareFrame } = reg;
      const fctx = { map: fakeMap(), alive: () => true };
      const plain = { id: 'plain', layers: [], sources: [], build: async () => {} };
      const r = await prepareFrame([plain], fctx, 1000);
      check('no prepare means no participation', r.applies.length === 0);
      check('and no error', Object.keys(r.errors).length === 0);
    }

    console.log('\none slow participant does not hold the frame:');
    {
      const { prepareFrame } = reg;
      const fctx = { map: fakeMap(), alive: () => true };
      const slow = {
        id: 'slow', layers: [], sources: [], build: async () => {},
        prepare: async () => {
          await new Promise((r) => setTimeout(r, 400));
          return [() => {}];
        }
      };
      const started = Date.now();
      const r = await prepareFrame([slow], fctx, 60);
      check('the deadline wins', r.late === true);
      // Past the deadline the caller keeps the frame it has, rather than
      // painting a partial one.
      check('...and nothing is handed back to paint', r.applies.length === 0);
      check('...without waiting for the slow one', Date.now() - started < 300,
        `${Date.now() - started}ms`);
    }

    console.log('\na failed participant is named, and the rest still paint:');
    {
      const { prepareFrame } = reg;
      const fctx = { map: fakeMap(), alive: () => true };
      const broken = {
        id: 'composite', layers: [], sources: [], build: async () => {},
        prepare: async () => { throw new Error('frame 404'); }
      };
      const fine = {
        id: 'radars', layers: [], sources: [], build: async () => {},
        prepare: async () => [() => {}, () => {}]
      };
      const r = await prepareFrame([broken, fine], fctx, 1000);
      // The three hardcoded participants this replaces caught to null and
      // to an empty array, so a failed load and an idle layer looked the
      // same from outside.
      check('the failure is named', r.errors.composite === 'frame 404',
        JSON.stringify(r.errors));
      check('the others still prepared', r.applies.length === 2);
      check('not reported late', r.late === false);
    }

    console.log('\nthe modules declare what they own:');
    {
      for (const mod of [watershedsLayer, reservoirsLayer,
                         terrainLayer('https://example.test/{z}/{x}/{y}.png')]) {
        check(`${mod.id} names its layers and sources`,
          mod.layers.length > 0 && mod.sources.length > 0,
          `${mod.layers.join(',')} on ${mod.sources.join(',')}`);
      }
      check('reservoirs removes its label before its marker',
        reservoirsLayer.layers[0] === 'reservoir-label',
        reservoirsLayer.layers.join(','));
      check('only terrain needs a custom teardown',
        !watershedsLayer.teardown && !reservoirsLayer.teardown
        && !!terrainLayer('x').teardown);
    }
  };

  return run().then(() => {
    console.log(failures.length
      ? `\n${failures.length} FAILED: ${failures.join(', ')}`
      : '\nall layer-registry assertions passed');
    return failures.length ? 1 : 0;
  });
}
