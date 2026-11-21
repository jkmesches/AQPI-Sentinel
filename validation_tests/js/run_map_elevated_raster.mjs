// Compiles $lib/map/layers/elevatedRaster.ts with the esbuild vite ships,
// then runs the assertions against the real altitude bounds the sheet uses.
//
// The GL layer beside it needs a browser and a context, so the arithmetic
// lives in its own module and this runs that one.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'elevated-')), 'elevated.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/layers/elevatedRaster.ts'),
   '--format=esm', `--outfile=${out}`, '--bundle'],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_map_elevated_raster.mjs');
process.exit(runTests(mod));
