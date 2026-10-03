// Every check family that plots a sparkline must be reachable from one place.
//
// The sparkline answer used to live in three: the store enumerated stages L1
// and L2 to decide what to FETCH, and each home-page rail hard-coded a metric
// name to decide what to DRAW. The backend stages were missing from the
// store's enumeration — LB2 absent from the loop, LB1 excluded twice since the
// L1 loop also guarded on `layer1.product.` — so those rows requested no
// series and rendered an empty cell while their samples sat in metric_samples.
//
// That was the NINTH hand-rolled stage enumeration. v0.5.1 found eight and
// routed them through ALL_STAGES; all eight were in .svelte pages and this one
// was in a store, so the sweep missed it. A sweep that depends on where code
// happens to live will miss the next one too, hence an assertion.
//
// Run:  node validation_tests/js/test_sparkline_coverage.mjs
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const read = (p) => readFileSync(root.replace(/\/$/, '') + '/' + p, 'utf8');

const fmt   = read('frontend/src/lib/format.ts');
const store = read('frontend/src/lib/stores/state.svelte.ts');
const home  = read('frontend/src/routes/+page.svelte');

const failures = [];
const check = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`);
  if (!cond) failures.push(label);
};

// ---- one source of truth exists ----------------------------------------
const fn = fmt.match(/export function sparklineMetric\(checkId: string\)[\s\S]*?\n\}/);
check('format.ts exports sparklineMetric', !!fn);

// Both halves of each monitored pair must be answerable. A family that plots
// nothing is fine, but it has to be a decision, not an omission.
for (const prefix of ['layer2.radar.', 'layer2.backend.',
                      'layer1.product.', 'layer1.backend.']) {
  check(`sparklineMetric answers for ${prefix}*`, fn[0].includes(`'${prefix}'`));
}

// ---- the store derives, it does not enumerate ---------------------------
const series = store.match(/private sparklineSeries\(\)[\s\S]*?\n\t\}/);
check('the store still has sparklineSeries()', !!series);
check('...and asks sparklineMetric rather than naming metrics itself',
      series[0].includes('sparklineMetric'));
check('...and does not reach for individual stages by name',
      !/stages\?\.(L\d|LB\d|L4)/.test(series[0]),
      (series[0].match(/stages\?\.\w+/g) || []).join(', '));
check('...and walks whatever stages the rollup actually has',
      /Object\.values\(this\.rollup\?\.stages/.test(series[0]));

// All four plot the SAME metric now, which is the point: two shapes in one
// rail meant a reader had to know which family a row was before the trace
// meant anything. If someone reintroduces a per-family metric, the fixed axis
// below stops being meaningful and this says so.
check('every family plots one comparable metric',
      (fn[0].match(/return '(\w+)'/g) || []).every((m) => m === "return 'headroom'"),
      [...new Set(fn[0].match(/return '\w+'/g) || [])].join(', '));

// ---- the fixed axis is wired, not just available ------------------------
// headroom without a fixed domain is pointless: autoscale would rescale 0..1
// back to the window's own extremes and restore the exact bug it was added to
// fix (a steady series and a dead one both on the baseline).
check('format.ts exports sparklineDomain', /export function sparklineDomain/.test(fmt));
check('...and gives headroom a fixed range',
      /sparklineDomain[\s\S]*?metric === 'headroom' \? \[0, 1\]/.test(fmt));
check('format.ts exports sparklineWarnAt', /export function sparklineWarnAt/.test(fmt));

const spark = read('frontend/src/lib/components/Sparkline.svelte');
check('the component accepts a domain', /domain\?:/.test(spark));
check('...and delegates its geometry rather than inlining the scale',
      /sparklineGeometry\(/.test(spark) && !/Math\.min\(\.\.\.nonEmpty\)/.test(spark));

// ---- the rails draw what the store fetched ------------------------------
// A rail naming a metric literally is how the two drift: the store can fetch
// headroom for a row while the rail looks up age_s and finds nothing.
const literalLookups = home.match(/metrics\[`\$\{\w+\.check_id\}\|(?!\$\{)[a-z_A-Z]+`\]/g) || [];
check('no rail looks up a hard-coded metric name', literalLookups.length === 0,
      literalLookups.join(', '));
check('the rails resolve their metric through the helper',
      /sparklineMetric\(\w+\.check_id\)/.test(home));
// Passing the metric but not its domain is the silent half-failure: the right
// series is fetched and then drawn on the wrong axis.
check('...and pass its domain to the sparkline',
      /domain=\{sparklineDomain\(/.test(home));
check('...and its warn rule', /warnAt=\{sparklineWarnAt\(/.test(home));

// ---- numbers the rails print must be fetched too -------------------------
// Same failure as the sparkline one, wearing a different hat: the products
// rail prints an age beside each row, which used to arrive free because the
// trace plotted age_s. The moment the trace became headroom, nothing fetched
// age_s and that column read "—" on every row. A displayed metric that nobody
// fetches is silent — no error, just a dash.
const readout = fmt.match(/export function readoutMetric\(checkId: string\)[\s\S]*?\n\}/);
check('format.ts exports readoutMetric', !!readout);
const printed = [...home.matchAll(/ageFromMetrics\([^,]+,\s*'([^']+)'\)/g)].map((m) => m[1]);
for (const metric of new Set(printed)) {
  check(`the rails print '${metric}', so something must fetch it`,
        !!readout && readout[0].includes(`'${metric}'`));
}
check('the store fetches readouts alongside traces',
      /readoutMetric\(/.test(store));

// ---- deployment awareness ------------------------------------------------
// The backend checks are registered behind `if SETTINGS.backend_root:`, so a
// deployment without the mount has none. Deciding the layout from the rollup
// instead of the catalog renders the one-column form until the first LB run
// lands and then jumps — correct eventually, wrong on every first paint.
const srcBlock = home.match(/const sources = \$derived\.by\([\s\S]*?\n\t\}\);/);
check('the layout derives its sources from somewhere', !!srcBlock);
check('...from the checks catalog, not the rollup',
      !!srcBlock && /sentinel\.checks/.test(srcBlock[0]) && !/rollup/.test(srcBlock[0]));
check('...and not by counting an empty rollup stage',
      !/stages\?\.LB\d\s*\?\?\s*\[\]\)\.length/.test(home));
// Stage presence is too coarse a question. One unrelated check sitting in L1
// or L2 — the xband-fleet row, the NWM stream feed — would otherwise flip the
// whole rail into the paired layout and print "not mounted" on every row with
// no radarca counterpart, which is what an XQPI deployment looks like.
check('...keyed on TARGETS, so a stray check cannot flip the layout',
      !!srcBlock && /\.add\(c\.target\)/.test(srcBlock[0]) &&
      /some\(\(t\) => rc\.has\(t\)\)/.test(srcBlock[0]));
check('the rails branch on "two readings exist", not "the backend exists"',
      /pairedRadars/.test(home) && /pairedProducts/.test(home) &&
      !/hasBackend/.test(home));

console.log(failures.length
  ? `\n${failures.length} FAILED: ${failures.join(', ')}`
  : '\nall sparkline-coverage assertions passed');
process.exit(failures.length ? 1 : 0);
