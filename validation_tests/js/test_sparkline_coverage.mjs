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

// ---- the rails draw what the store fetched ------------------------------
// A rail naming a metric literally is how the two drift: the store can fetch
// age_s for a backend row while the rail looks up images_Reflectivity and
// finds nothing.
const literalLookups = home.match(/metrics\[`\$\{r\.check_id\}\|(?!\$\{)[a-z_A-Z]+`\]/g) || [];
check('no rail looks up a hard-coded metric name', literalLookups.length === 0,
      literalLookups.join(', '));
const viaHelper = (home.match(/sparklineMetric\(r\.check_id\)/g) || []).length;
check('both rails resolve their metric through the helper', viaHelper >= 2,
      `${viaHelper} call site(s)`);

console.log(failures.length
  ? `\n${failures.length} FAILED: ${failures.join(', ')}`
  : '\nall sparkline-coverage assertions passed');
process.exit(failures.length ? 1 : 0);
