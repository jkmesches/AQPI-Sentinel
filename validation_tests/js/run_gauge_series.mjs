// Compiles $lib/gaugeSeries.ts with the esbuild vite ships, then runs the
// assertions against the real module MapView imports.
//
// Runs the suite once per zone instead of once in whatever zone the machine
// happens to use. The fault these assertions guard against is reading an
// unmarked timestamp as local time, which is CORRECT at UTC and wrong
// everywhere else, so a suite run only at UTC passes against the bug. That
// was checked by reverting the fix: the assertions fail under America/Denver
// and Asia/Kolkata and pass under UTC.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const self = new URL(import.meta.url).pathname;

// West of UTC, at UTC, and two offsets east, including one on a half hour.
const ZONES = ['America/Denver', 'UTC', 'Europe/Berlin', 'Asia/Kolkata',
               'Pacific/Auckland'];

if (process.env.GAUGE_TZ_CHILD) {
  const out = join(mkdtempSync(join(tmpdir(), 'gauge-')), 'gaugeSeries.mjs');
  execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
    [join(root, 'frontend/src/lib/gaugeSeries.ts'), '--format=esm', `--outfile=${out}`],
    { stdio: 'inherit' });
  const mod = await import(out);
  const { runTests } = await import('./test_gauge_series.mjs');
  process.exit(runTests(mod));
}

let failed = 0;
for (const tz of ZONES) {
  console.log(`\n===== TZ=${tz} =====`);
  try {
    execFileSync(process.execPath, [self], {
      stdio: 'inherit',
      env: { ...process.env, TZ: tz, GAUGE_TZ_CHILD: '1' }
    });
  } catch {
    failed += 1;
  }
}
console.log(failed
  ? `\n${failed}/${ZONES.length} zones FAILED`
  : `\nall gauge-series assertions passed in ${ZONES.length} zones`);
process.exit(failed ? 1 : 0);
