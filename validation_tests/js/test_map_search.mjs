// Finding a place from the datasets this console already has.
//
// Run:  node validation_tests/js/run_map_search.mjs
//
// There is no geocoder behind this. The CSU deployment's namespace has no
// public internet route, so a search that called out to one would work on
// the laptop and fail on the machine the operators use. Everything matched
// here is a dataset the console loads from its own backend.
//
// Three things are worth asserting rather than reading.
//
// A call sign must beat a place name. "XSCR" is both a radar id and part of
// nothing else, and a matcher that scored "Santa Cruz" above the radar
// actually named XSCR would be worse than no ranking at all.
//
// The order must not move between keystrokes. A list that reorders under
// the reader's finger makes them click the wrong result, so ties break on
// something stable rather than on insertion order.
//
// A broken coordinate must never be flown to. A null or zero pair has
// shipped in these datasets before, and a camera that jumps to the Gulf of
// Guinea reads as the map breaking rather than the data.

const ENTRIES = [
  { id: 'XSCR', name: 'Santa Cruz', kind: 'radar', lng: -121.9786, lat: 36.9847 },
  { id: 'XSCV', name: 'Coyote Valley', kind: 'radar', lng: -121.8334, lat: 37.3989 },
  { id: 'XSCW', name: 'Sonoma', kind: 'radar', lng: -122.8022, lat: 38.5216 },
  { id: 'CBAND', name: 'Mt Barnabe', kind: 'radar', lng: -122.7161, lat: 38.0277 },
  { id: 'res-lex', name: 'Lexington Reservoir', kind: 'reservoir', lng: -121.99, lat: 37.18 },
  { id: 'res-cal', name: 'Calaveras Reservoir', kind: 'reservoir', lng: -121.82, lat: 37.49 },
  { id: 'g-1', name: 'Coyote Creek at Madrone', kind: 'gauge', lng: -121.68, lat: 37.14 },
  { id: 'w-1', name: 'San Francisco Bay', kind: 'watershed', lng: -122.3, lat: 37.8 },
  { id: 'w-2', name: 'Cañada de los Osos', kind: 'watershed', lng: -121.5, lat: 37.2 }
];

export function runTests(mod) {
  const {
    fold, scoreEntry, searchEntries, isPlaceable, buildIndex, stepHit,
    coordinateBounds, boundsCenter,
    SCORE_EXACT, SCORE_PREFIX, SCORE_WORD, SCORE_SUBSTRING, MAX_HITS,
    MIN_QUERY
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const names = (q) => searchEntries(ENTRIES, q).map((h) => h.entry.name);
  const ids = (q) => searchEntries(ENTRIES, q).map((h) => h.entry.id);

  console.log('names are compared without case, accents or punctuation:');
  {
    check('case is folded', fold('Santa CRUZ') === 'santa cruz');
    check('accents are folded', fold('Cañada') === 'canada');
    check('punctuation becomes a space', fold('Mt. Barnabe') === 'mt barnabe');
    check('runs of spaces collapse', fold('a   b') === 'a b');
    check('the ends are trimmed', fold('  x  ') === 'x');
    check('an empty name folds to empty', fold('') === '');
    check('a name of punctuation folds to empty', fold('--- ...') === '');
    check('a reader typing without the tilde finds it',
      ids('canada').includes('w-2'), ids('canada').join(','));
  }

  console.log('\na call sign beats a place with a similar name:');
  {
    check('XSCR is the first hit for its own id',
      ids('xscr')[0] === 'XSCR', ids('xscr').join(','));
    check('...and it scores exact',
      scoreEntry(ENTRIES[0], 'xscr') === SCORE_EXACT);
    check('a full name scores exact too',
      scoreEntry(ENTRIES[0], 'santa cruz') === SCORE_EXACT);
    check('a prefix of the id outranks a word inside a name',
      SCORE_PREFIX > SCORE_WORD);
    check('a word start outranks a bare substring',
      SCORE_WORD > SCORE_SUBSTRING);
    check('"cru" finds Santa Cruz by its second word',
      ids('cru').includes('XSCR'), ids('cru').join(','));
    check('"coyote" finds both the radar and the gauge',
      names('coyote').length === 2, names('coyote').join(' / '));
    check('...with the shorter name first, since both are word matches',
      names('coyote')[0] === 'Coyote Valley', names('coyote').join(' / '));
  }

  console.log('\nthe order does not move between keystrokes:');
  {
    const a = ids('re');
    const b = ids('re');
    check('the same query gives the same order', a.join() === b.join(), a.join());
    const shuffled = [...ENTRIES].reverse();
    check('...whatever order the datasets were added in',
      searchEntries(shuffled, 're').map((h) => h.entry.id).join() === a.join(),
      searchEntries(shuffled, 're').map((h) => h.entry.id).join());
    // Both reservoir names are 19 characters, so this falls through the
    // length rule to the alphabetical one.
    check('equal-length ties break alphabetically',
      names('reservoir')[0] === 'Calaveras Reservoir',
      names('reservoir').join(' / '));
    const lengths = [
      { id: 'a', name: 'Ridge Creek Overlook', kind: 'p', lng: -122, lat: 37 },
      { id: 'b', name: 'Ridge', kind: 'p', lng: -122.1, lat: 37.1 }
    ];
    check('a shorter name comes first where the lengths differ',
      searchEntries(lengths, 'ridge')[0].entry.name === 'Ridge',
      searchEntries(lengths, 'ridge').map((h) => h.entry.name).join(' / '));
  }

  console.log('\na short or empty query returns nothing:');
  {
    check('one character is too short', searchEntries(ENTRIES, 'x').length === 0);
    check(`${MIN_QUERY} characters is enough`,
      searchEntries(ENTRIES, 'xs').length > 0);
    check('empty returns nothing', searchEntries(ENTRIES, '').length === 0);
    check('spaces return nothing', searchEntries(ENTRIES, '   ').length === 0);
    check('punctuation alone returns nothing',
      searchEntries(ENTRIES, '...').length === 0);
    check('a query matching nothing returns nothing',
      searchEntries(ENTRIES, 'zzzz').length === 0);
  }

  console.log('\nthe list is bounded:');
  {
    const many = Array.from({ length: 200 }, (_, i) => ({
      id: `r-${i}`, name: `Ridge ${i}`, kind: 'place',
      lng: -122 - i / 1000, lat: 37 + i / 1000
    }));
    check(`at most ${MAX_HITS} by default`,
      searchEntries(many, 'ridge').length === MAX_HITS,
      String(searchEntries(many, 'ridge').length));
    check('a smaller limit is honored',
      searchEntries(many, 'ridge', 3).length === 3);
    check('a zero limit returns nothing',
      searchEntries(many, 'ridge', 0).length === 0);
    check('a negative limit returns nothing rather than throwing',
      searchEntries(many, 'ridge', -5).length === 0);
  }

  console.log('\na broken coordinate is never offered:');
  {
    const bad = [
      ['a null island pair', { lng: 0, lat: 0 }],
      ['a NaN longitude', { lng: NaN, lat: 37 }],
      ['a NaN latitude', { lng: -122, lat: NaN }],
      ['a latitude past the pole', { lng: -122, lat: 91 }],
      ['a longitude past the meridian', { lng: 181, lat: 37 }],
      ['an infinite pair', { lng: Infinity, lat: Infinity }]
    ];
    for (const [name, coords] of bad) {
      const entry = { id: 'x', name: 'X', kind: 'place', ...coords };
      check(`${name} is not placeable`, isPlaceable(entry) === false);
      check(`...and is dropped from the index`,
        buildIndex([entry]).length === 0);
    }
    check('a real coordinate survives', buildIndex(ENTRIES).length
      === ENTRIES.length, String(buildIndex(ENTRIES).length));
    check('a point at the pole is allowed, since a radar could be there',
      isPlaceable({ id: 'p', name: 'P', kind: 'place', lng: 0, lat: 90 }));
  }

  console.log('\nthe index does not repeat an entry:');
  {
    const doubled = [...ENTRIES, ENTRIES[0], { ...ENTRIES[0], name: 'Other' }];
    check('the same kind and id appears once',
      buildIndex(doubled).length === ENTRIES.length,
      String(buildIndex(doubled).length));
    check('the first one wins',
      buildIndex(doubled).find((e) => e.id === 'XSCR').name === 'Santa Cruz');
    check('the same id in another dataset is kept',
      buildIndex([ENTRIES[0], { ...ENTRIES[0], kind: 'gauge' }]).length === 2);
  }

  console.log('\na shape is reduced to the box the camera should fit:');
  {
    const square = [[[-122, 37], [-121, 37], [-121, 38], [-122, 38],
      [-122, 37]]];
    check('a polygon gives its corners',
      JSON.stringify(coordinateBounds(square)) === '[-122,37,-121,38]',
      JSON.stringify(coordinateBounds(square)));
    const multi = [square, [[[-120, 36], [-119, 36], [-119, 36.5],
      [-120, 36]]]];
    check('a multipolygon covers every part',
      JSON.stringify(coordinateBounds(multi)) === '[-122,36,-119,38]',
      JSON.stringify(coordinateBounds(multi)));
    check('a bare point is its own box',
      JSON.stringify(coordinateBounds([-122, 37])) === '[-122,37,-122,37]');
    check('an empty shape has no box', coordinateBounds([]) === null);
    check('a non-array has no box', coordinateBounds(null) === null);
    check('a shape of nulls has no box',
      coordinateBounds([[null, null]]) === null);
    check('a NaN coordinate is skipped rather than poisoning the box',
      JSON.stringify(coordinateBounds([[-122, 37], [NaN, 5], [-121, 38]]))
        === '[-122,37,-121,38]',
      JSON.stringify(coordinateBounds([[-122, 37], [NaN, 5], [-121, 38]])));
    // Bounded, so a cyclic or absurdly nested value cannot hang the page.
    let deep = [-122, 37];
    for (let i = 0; i < 40; i++) deep = [deep];
    check('an absurdly nested shape returns rather than hanging',
      coordinateBounds(deep) === null);
    const c = boundsCenter([-122, 37, -121, 38]);
    check('the center is the middle of the box',
      c.lng === -121.5 && c.lat === 37.5, `${c.lng} ${c.lat}`);
  }

  console.log('\nthe arrow keys walk the list and stop at its ends:');
  {
    check('down from nothing selects the first', stepHit(-1, 5, 1) === 0);
    check('up from nothing selects the last', stepHit(-1, 5, -1) === 4);
    check('down moves one', stepHit(2, 5, 1) === 3);
    check('up moves one', stepHit(2, 5, -1) === 1);
    check('the last stays put rather than wrapping',
      stepHit(4, 5, 1) === 4);
    check('the first stays put', stepHit(0, 5, -1) === 0);
    check('an empty list selects nothing', stepHit(0, 0, 1) === -1);
    check('a step of zero changes nothing', stepHit(2, 5, 0) === 2);
    check('an index past the end comes back inside',
      stepHit(99, 5, 1) === 4, String(stepHit(99, 5, 1)));
  }

  if (failures.length) {
    console.log(`\n${failures.length} failed:`);
    for (const f of failures) console.log(`  ${f}`);
    return 1;
  }
  console.log('\nall search assertions passed');
  return 0;
}
