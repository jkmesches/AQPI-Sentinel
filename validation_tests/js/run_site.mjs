// Compiles $lib/site.ts with the esbuild vite ships, then runs the
// assertions against the real module both map components import.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'site-')), 'site.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/site.ts'), '--format=esm', `--outfile=${out}`],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_site.mjs');
// runTests is async here (resolveHomeView awaits a fetch), unlike the sync
// runners beside it — await it or process.exit gets a Promise.
process.exit(await runTests(mod));
