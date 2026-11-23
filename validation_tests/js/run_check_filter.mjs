// Compiles $lib/checkFilter.ts with the esbuild that ships with vite, then
// runs the assertions against the real module the dashboard imports.
//
// Transpile-only: checkFilter imports nothing, so no bundle is needed.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'cf-')), 'checkFilter.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/checkFilter.ts'), '--format=esm', `--outfile=${out}`],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_check_filter.mjs');
process.exit(runTests(mod));
