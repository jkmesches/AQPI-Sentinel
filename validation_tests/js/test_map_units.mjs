// Which units a product offers, and which of them are current.
//
// Run:  node validation_tests/js/run_map_units.mjs
//
// The hazard here is not picking the wrong unit, it is picking a unit whose
// subtree nobody writes any more. Three of AQPI's six multi-unit products
// stopped writing millimeters on 2026-09-30 while still writing inches every
// two minutes, so a toggle that treated the two alike would hand the reader
// rainfall seven weeks old looking exactly like this hour's.
//
// The payload below is what backend/config.py produces for the aqpi profile;
// validation_tests/test_product_units.py checks it against the mount.

const TABLE = {
  qpe_15min: {
    units: ['in', 'mm'],
    state: { in: 'live', mm: 'stale:2026-09-30' }
  },
  qpe_1hr: {
    units: ['in', 'mm'],
    state: { in: 'live', mm: 'stale:2026-09-30' }
  },
  precip_rate_radar: {
    units: ['in/h', 'mm/h'],
    state: { 'in/h': 'live', 'mm/h': 'stale:2026-09-30' }
  },
  fcst_total_precip: {
    units: ['in', 'mm'],
    state: { in: 'live', mm: 'live' }
  },
  fcst_precip_rate: {
    units: ['in/h', 'mm/h'],
    state: { 'in/h': 'live', 'mm/h': 'live' }
  }
};

export function runTests(mod) {
  const { unitChoices, defaultUnit, unitNote, resolveUnit } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  console.log('a product offers the units it publishes:');
  {
    const q = unitChoices(TABLE, 'qpe_1hr').map((c) => c.key);
    check('qpe_1hr offers in and mm', q.join(',') === 'in,mm', q.join(','));
    const r = unitChoices(TABLE, 'precip_rate_radar').map((c) => c.key);
    check('a rate product offers the rate labels',
      r.join(',') === 'in/h,mm/h', r.join(','));
    check('the canonical unit comes first',
      unitChoices(TABLE, 'qpe_1hr')[0].key === 'in');
  }

  console.log('\na product with no choice offers no control:');
  {
    // A single-option toggle suggests there is something to pick.
    check('an unknown product', unitChoices(TABLE, 'comp_ref').length === 0);
    check('a null table', unitChoices(null, 'qpe_1hr').length === 0);
    check('an empty table', unitChoices({}, 'qpe_1hr').length === 0);
    check('a one-unit entry',
      unitChoices({ x: { units: ['in'], state: { in: 'live' } } }, 'x')
        .length === 0);
    check('an entry with no units',
      unitChoices({ x: { units: [], state: {} } }, 'x').length === 0);
  }

  console.log('\nthe stale units are the ones the mount said:');
  {
    // The regression. A state that stops travelling with the choice turns
    // every unit into one that looks current.
    for (const [pid, want] of [['qpe_15min', false], ['qpe_1hr', false],
      ['precip_rate_radar', false], ['fcst_total_precip', true],
      ['fcst_precip_rate', true]]) {
      const second = unitChoices(TABLE, pid)[1];
      check(`${pid}: its second unit is ${want ? 'live' : 'stale'}`,
        second.live === want, `live=${second.live} since=${second.since}`);
    }
    check('every canonical unit is live',
      Object.keys(TABLE).every((p) => unitChoices(TABLE, p)[0].live));
    const mm = unitChoices(TABLE, 'qpe_1hr')[1];
    check('a stale unit carries the date it stopped',
      mm.since === '2026-09-30', String(mm.since));
    check('a live unit carries no date',
      unitChoices(TABLE, 'fcst_total_precip')[1].since === null);
  }

  console.log('\nan unrecorded state is treated as not live:');
  {
    // A unit nobody checked is not a unit anybody can vouch for.
    const t = { x: { units: ['in', 'mm'], state: { in: 'live' } } };
    const mm = unitChoices(t, 'x')[1];
    check('it is not live', mm.live === false);
    check('and it has no date to show', mm.since === null);
    check('so it still warns', unitNote(unitChoices(t, 'x'), 'mm')
      === 'not published', String(unitNote(unitChoices(t, 'x'), 'mm')));
  }

  console.log('\nthe default is the canonical unit:');
  {
    // The L1 thresholds were characterized against it, so defaulting
    // elsewhere would move what the checks measure.
    for (const pid of Object.keys(TABLE)) {
      check(`${pid} defaults to ${TABLE[pid].units[0]}`,
        defaultUnit(unitChoices(TABLE, pid)) === TABLE[pid].units[0]);
    }
    check('no choices means no unit on the request',
      defaultUnit([]) === null);
    check('and an unknown product sends none',
      defaultUnit(unitChoices(TABLE, 'comp_ref')) === null);
  }

  console.log('\nthe warning appears only where it should:');
  {
    const q = unitChoices(TABLE, 'qpe_1hr');
    check('the live unit says nothing', unitNote(q, 'in') === null);
    check('the stale one names the date',
      unitNote(q, 'mm') === 'not published since 2026-09-30',
      String(unitNote(q, 'mm')));
    check('a null unit says nothing', unitNote(q, null) === null);
    check('a unit the product does not have says nothing',
      unitNote(q, 'mm/h') === null);
    const f = unitChoices(TABLE, 'fcst_total_precip');
    check('a product whose units are both live says nothing',
      unitNote(f, 'in') === null && unitNote(f, 'mm') === null);
  }

  console.log('\na unit does not survive a change of product:');
  {
    // Carrying "mm/h" to a product whose units are "in" and "mm" sends a
    // label the backend answers 400 to.
    const depth = unitChoices(TABLE, 'qpe_1hr');
    const rate = unitChoices(TABLE, 'precip_rate_radar');
    check('a rate unit on a depth product falls back',
      resolveUnit(depth, 'mm/h') === 'in', String(resolveUnit(depth, 'mm/h')));
    check('a depth unit on a rate product falls back',
      resolveUnit(rate, 'mm') === 'in/h', String(resolveUnit(rate, 'mm')));
    check('a unit the product does have is kept',
      resolveUnit(depth, 'mm') === 'mm');
    check('null takes the default', resolveUnit(depth, null) === 'in');
    check('a product with no units takes none',
      resolveUnit([], 'mm') === null);
    check('an invented unit falls back',
      resolveUnit(depth, 'furlongs') === 'in');
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall unit assertions passed');
  return failures.length ? 1 : 0;
}
