// Compiles $lib/map/nexrad.ts with the esbuild vite ships, then runs the
// assertions against the real module the map imports.
//
// Transpile-only, unlike run_radar_status.mjs: this module imports nothing,
// which is the property that let it come out of MapView first.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'nx-')), 'nexrad.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/nexrad.ts'), '--format=esm', `--outfile=${out}`],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_map_nexrad.mjs');
process.exit(runTests(mod));
