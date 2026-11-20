// Compiles $lib/api.ts with the esbuild that ships with vite, then runs the
// assertions against the real module the dashboard imports.
//
// --bundle, because api.ts imports `url` from $lib/origin and a
// transpile-only build would leave that specifier to resolve against the temp
// directory and fail.
//
// origin.ts reads import.meta.env at module scope, which Vite substitutes at
// build time and Node does not provide at all, so importing the bundle throws
// before a single assertion runs. Defining it as an empty object gives the
// same result as an unset variable, and the batching helpers under test do
// not read the API base at all.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'mb-')), 'api.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/api.ts'), '--bundle', '--format=esm',
   '--define:import.meta.env={}', `--outfile=${out}`],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_metrics_batching.mjs');
process.exit(runTests(mod));
