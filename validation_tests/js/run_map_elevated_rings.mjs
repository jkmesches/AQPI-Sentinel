// Compiles $lib/map/layers/elevatedRings.ts with the esbuild vite ships,
// then runs the assertions against the real ring geometry.
//
// The GL layer beside it needs a browser and a context, so the arithmetic
// lives in its own module and this runs that one.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'rings-')), 'rings.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/layers/elevatedRings.ts'),
   '--format=esm', `--outfile=${out}`, '--bundle'],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_map_elevated_rings.mjs');
process.exit(runTests(mod));
