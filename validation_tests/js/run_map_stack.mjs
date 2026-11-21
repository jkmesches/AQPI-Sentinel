// Compiles $lib/map/layers/stack.ts with the esbuild vite ships, then runs
// the assertions against the real module the overlays are added through.
//
// Transpile-only: the module imports nothing.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'stk-')), 'stack.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/layers/stack.ts'), '--format=esm',
   `--outfile=${out}`],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_map_stack.mjs');
process.exit(runTests(mod));
