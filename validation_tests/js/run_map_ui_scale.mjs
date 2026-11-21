// Compiles $lib/map/uiScale.ts with the esbuild vite ships, then runs the
// assertions against the sizes the settings control actually offers.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'uiscale-')), 'uiScale.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/uiScale.ts'), '--format=esm',
   `--outfile=${out}`, '--bundle'],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_map_ui_scale.mjs');
process.exit(runTests(mod));
