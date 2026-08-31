// Compiles $lib/timelineFill.ts with the esbuild that ships with vite, then
// runs the assertions. Keeps the test honest — it exercises the real module
// the route imports, rather than a re-implementation.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'tf-')), 'timelineFill.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/timelineFill.ts'), '--format=esm', `--outfile=${out}`],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_timeline_fill.mjs');
process.exit(runTests(mod));
