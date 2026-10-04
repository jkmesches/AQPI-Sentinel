// Pairing a radarca reading with its backend counterpart.
//
// Three surfaces render this -- the desktop timeline grid, the mobile status
// cards, the mobile uptime grid -- and each had its own copy of the rule
// before $lib/pairing. The properties below are the ones a surface actually
// depends on when it draws a group separator or an indent, so a
// reimplementation that gets any of them wrong draws the separator in the
// wrong place rather than failing visibly.

const C = (id, target, stage) => ({ id, target, stage });
const key = (c) => c.target;

export function runTests(mod) {
  const { pairByTarget, endsPairGroup } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const roles = (out) => out.map((p) => p.role).join(',');
  const ids = (out) => out.map((p) => p.item.id).join(',');

  console.log('no backend reading leaves the list exactly as it was:');
  {
    const prim = [C('a', 'X', 'L1'), C('b', 'Y', 'L1')];
    const out = pairByTarget(prim, [], key);
    check('every row is solo', roles(out) === 'solo,solo', roles(out));
    check('order and identity are preserved', ids(out) === 'a,b', ids(out));
    check('...and nothing was copied', out.every((p, i) => p.item === prim[i]));
  }

  console.log('\na matched target yields primary then secondary, adjacently:');
  {
    const out = pairByTarget([C('p', 'X', 'L1')], [C('b', 'X', 'LB1')], key);
    check('two entries', out.length === 2, String(out.length));
    check('primary first', roles(out) === 'primary,secondary', roles(out));
    check('the primary is the radarca row', out[0].item.id === 'p', out[0].item.id);
    check('the secondary is the backend row', out[1].item.id === 'b', out[1].item.id);
  }

  console.log('\nunmatched rows on either side stay solo:');
  {
    // The real shapes: a stream feed with no backend counterpart, and the
    // xband-fleet correlation that lives in L2 but mirrors nothing.
    const out = pairByTarget(
      [C('p1', 'X', 'L1'), C('stream', 'stream', 'L1')],
      [C('b1', 'X', 'LB1')], key);
    check('roles', roles(out) === 'primary,secondary,solo', roles(out));
    check('the lone primary keeps its place in order',
      ids(out) === 'p1,b1,stream', ids(out));
  }
  {
    const out = pairByTarget([], [C('b1', 'FLOW', 'LB2')], key);
    check('a backend row with no primary still gets a row',
      out.length === 1 && out[0].role === 'solo', roles(out));
  }

  console.log('\nthe XQPI shape: every row arrives with no primary at all:');
  {
    // LB1/LB2 registered with no L1/L2. If this dropped rows, that profile's
    // dashboard would be empty rather than wrong, which is worse.
    const backend = ['composite_ref', 'qpe_15min', 'qpe_1hr', 'radar_precip_rate']
      .map((t) => C(`layer1.backend.${t}`, t, 'LB1'));
    const out = pairByTarget([], backend, key);
    check('all four products are present', out.length === 4, String(out.length));
    check('all solo', out.every((p) => p.role === 'solo'), roles(out));
    check('order follows the backend list',
      ids(out) === backend.map((c) => c.id).join(','), ids(out));
  }

  console.log('\na secondary is always immediately preceded by its own primary:');
  {
    // This is the invariant the separator rule rests on: `role !== 'primary'`
    // closes a group, with no lookahead. If a secondary could ever appear
    // without its primary directly before it, the rule would be wrong.
    const prim = ['X', 'Y', 'Z'].map((t) => C(`p${t}`, t, 'L1'));
    const back = ['X', 'Z'].map((t) => C(`b${t}`, t, 'LB1'));
    const out = pairByTarget(prim, back, key);
    let ok = true;
    out.forEach((p, i) => {
      if (p.role === 'secondary') {
        const prev = out[i - 1];
        if (!prev || prev.role !== 'primary' || key(prev.item) !== key(p.item)) ok = false;
      }
    });
    check('invariant holds', ok, roles(out));
    check('a primary never closes a group',
      out.filter((p) => p.role === 'primary').every((p) => !endsPairGroup(p)));
    check('everything else does',
      out.filter((p) => p.role !== 'primary').every((p) => endsPairGroup(p)));
    check('so group count equals target count',
      out.filter(endsPairGroup).length === 3,
      String(out.filter(endsPairGroup).length));
  }

  console.log('\nevery input row appears exactly once:');
  {
    const prim = ['X', 'Y', 'Z'].map((t) => C(`p${t}`, t, 'L1'));
    const back = ['Y', 'W'].map((t) => C(`b${t}`, t, 'LB1'));
    const out = pairByTarget(prim, back, key);
    check('no row is dropped', out.length === prim.length + back.length,
      `${out.length} vs ${prim.length + back.length}`);
    const seen = new Set(out.map((p) => p.item.id));
    check('no row is duplicated', seen.size === out.length,
      `${seen.size} unique of ${out.length}`);
    check('the orphaned backend row is last',
      out[out.length - 1].item.id === 'bW', out[out.length - 1].item.id);
  }

  console.log('\nthe inputs are not mutated:');
  {
    const prim = [C('p', 'X', 'L1')];
    const back = [C('b', 'X', 'LB1')];
    pairByTarget(prim, back, key);
    check('primary array intact', prim.length === 1);
    check('backend array intact', back.length === 1, String(back.length));
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall pairing assertions passed');
  return failures.length ? 1 : 0;
}
