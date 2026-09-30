// Every persisted Route field must survive load -> edit -> save.
//
// The admin alerts page is a form BUILDER, not a text editor: on save it
// discards the stored config and rebuilds each route from form state. A field
// the backend honors but buildConfig() does not emit is therefore silently
// erased the first time anyone opens the page and clicks Save — no error, no
// warning, the config just saves and looks correct.
//
// That happened. `hold_down` was a real Route field (backend/alarms/models.py)
// carrying the per-route persistence thresholds, and the editor never rendered
// it and buildConfig() never wrote it. Opening /admin/alerts and saving reset
// all four per-route hold-downs to the global default, which over the 14 days
// to 2026-09-30 was the difference between 104 and 136 alert emails — and the
// 32 that came back were the flappers the thresholds existed to remove.
//
// This asserts structurally rather than behaviourally, because the function
// lives inline in the .svelte and the failure is about a field being ABSENT.
//
// Run:  node validation_tests/js/test_route_config_roundtrip.mjs
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const src = readFileSync(
  join(root, 'frontend/src/routes/admin/alerts/+page.svelte'), 'utf8');
function join(a, b) { return a.replace(/\/$/, '') + '/' + b; }

const failures = [];
const check = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? '  — ' + detail : ''}`);
  if (!cond) failures.push(label);
};

// Fields that exist only to drive the editor and are deliberately not saved.
const UI_ONLY = new Set(['showWhen', 'showAdvanced', 'testBusy', 'testResult']);

// ---- the Route interface's own field list -------------------------------
const iface = src.match(/interface Route \{([\s\S]*?)\n\t\}/);
check('the Route interface is still parseable', !!iface);
const fields = [...iface[1].matchAll(/^\s*(\w+)\s*[?]?:/gm)].map((m) => m[1]);
check('Route declares fields', fields.length > 5, fields.join(', '));

const persisted = fields.filter((f) => !UI_ONLY.has(f));

// ---- what buildConfig() actually writes ---------------------------------
const build = src.match(/function buildConfig\(\)[\s\S]*?\n\t\}/);
check('buildConfig() is still parseable', !!build);
const routesBlock = build[0].match(/routes: routes\.map\([\s\S]*?\n\t\t\t\}\)/);
check('the routes mapping is still parseable', !!routesBlock);

for (const f of persisted) {
  // Either assigned onto `out` or spread/copied under its own name.
  const emitted = new RegExp(`(out\\.${f}\\b|\\b${f}:\\s)`).test(routesBlock[0]);
  check(`buildConfig() persists Route.${f}`, emitted);
}

// ---- and what the load path reads back ----------------------------------
// A field that is written but never parsed is just as lossy: it survives one
// save and vanishes on the next page load.
const loadBlock = src.match(/const match = \{[\s\S]*?testResult: null,/);
check('the route load path is still parseable', !!loadBlock);
for (const f of persisted) {
  if (f === 'match' || f === 'when') continue;   // built separately above
  // Any shape that reads the stored value counts — `f: r.f`, a ternary, a
  // spread. What must not happen is the field being defaulted from nothing.
  check(`the load path reads Route.${f}`,
        new RegExp(`\\br\\.${f}\\b`).test(loadBlock[0]));
}

// ---- the specific regression -------------------------------------------
check('hold_down has an editor control, not just a field',
      /bind:value=\{r\.hold_down\}/.test(src));
check('...and its placeholder names the global fallback rather than implying none',
      /placeholder="blank = global/.test(src));

console.log(failures.length
  ? `\n${failures.length} FAILED: ${failures.join(', ')}`
  : '\nall route-config round-trip assertions passed');
process.exit(failures.length ? 1 : 0);
