// Compiles $lib/map/compass.ts with the esbuild vite ships, then runs the
// assertions against the real tolerance the control branches on.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'compass-')), 'compass.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/compass.ts'), '--format=esm',
   `--outfile=${out}`, "--bundle"],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_map_compass.mjs');
process.exit(runTests(mod));
