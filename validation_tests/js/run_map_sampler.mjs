// Compiles $lib/map/sampler.ts with the esbuild vite ships, then runs the
// assertions against the real sampler the cursor readout holds frames in.
//
// The canvas is faked, since node has none. Whether a real canvas draws the
// frame correctly is checked in the browser by
// validation_tests/visual/shoot_readout.py.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'sampler-')), 'sampler.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/sampler.ts'), '--format=esm',
   `--outfile=${out}`],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_map_sampler.mjs');
process.exit(runTests(mod));
