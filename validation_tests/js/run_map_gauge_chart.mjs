// Compiles $lib/map/gaugeChart.ts with the esbuild vite ships, then runs the
// assertions against the real module the popup renders from.
//
// The module's only import is a type, which esbuild erases, so this is
// transpile-only and the `$lib` alias never has to resolve.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'gc-')), 'gaugeChart.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/gaugeChart.ts'), '--format=esm', `--outfile=${out}`],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_map_gauge_chart.mjs');
process.exit(runTests(mod));
