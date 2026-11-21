// Compiles $lib/map/permalink.ts with the esbuild vite ships, then runs the
// assertions against the real encoder the console shares links with.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'permalink-')), 'permalink.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/permalink.ts'), '--format=esm',
   `--outfile=${out}`],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_map_permalink.mjs');
process.exit(runTests(mod));
