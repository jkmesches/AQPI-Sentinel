// Compiles $lib/format.ts with the esbuild vite ships, then runs the
// assertions against the real readout, cadence and image-QC helpers.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'readouts-')), 'format.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/format.ts'),
   '--format=esm', `--outfile=${out}`, '--bundle'],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_dashboard_readouts.mjs');
process.exit(runTests(mod));
