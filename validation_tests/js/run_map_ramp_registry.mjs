// Compiles $lib/map/ramps/registry.ts with the esbuild vite ships, then runs
// the assertions against the real lookup the legend and readout will use.
//
// --bundle and --loader:.json=json, so the ramps and the coverage record come
// in exactly as vite will give them to the app. A stub of either would let
// the assertions pass against evidence the build does not actually carry.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'rampreg-')), 'registry.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/ramps/registry.ts'), '--bundle',
   '--format=esm', '--loader:.json=json', `--outfile=${out}`],
  { stdio: 'inherit' });

const coverage = JSON.parse(readFileSync(
  join(root, 'frontend/src/lib/map/ramps/data/coverage.json'), 'utf8'));
const products = JSON.parse(readFileSync(
  join(root, 'frontend/src/lib/map/ramps/data/product-coverage.json'), 'utf8'));

const mod = await import(out);
const { runTests } = await import('./test_map_ramp_registry.mjs');
process.exit(runTests(mod, coverage, products));
