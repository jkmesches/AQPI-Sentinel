// Compiles $lib/map/timeAxis.ts with the esbuild vite ships, then runs the
// assertions against the real geometry the time strip draws from.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'timeaxis-')), 'timeAxis.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/timeAxis.ts'), '--format=esm',
   `--outfile=${out}`],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_map_time_axis.mjs');
process.exit(runTests(mod));
