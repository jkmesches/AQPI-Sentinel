// Compiles $lib/map/selection.ts with the esbuild vite ships, then runs the
// assertions against the real module the console selects with.
//
// --bundle, because selection imports radarExtent from $lib/radarGeo. The
// bundle pulls in the real geometry, so the box assertions exercise the
// cos(latitude) correction as shipped rather than a stub of it.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'sel-')), 'selection.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/selection.ts'), '--bundle', '--format=esm',
   `--alias:$lib=${join(root, 'frontend/src/lib')}`, `--outfile=${out}`],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_map_selection.mjs');
process.exit(runTests(mod));
