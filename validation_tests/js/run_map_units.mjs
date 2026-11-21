// Compiles $lib/map/units.ts with the esbuild vite ships, then runs the
// assertions against the real table the unit control is built from.
//
// The payload in the test is what backend/config.py produces for the aqpi
// profile. validation_tests/test_product_units.py is what checks that
// payload against the mount it was read from.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'units-')), 'units.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/units.ts'), '--format=esm',
   `--outfile=${out}`],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_map_units.mjs');
process.exit(runTests(mod));
