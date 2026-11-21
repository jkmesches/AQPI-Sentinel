// Compiles $lib/map/readout.ts with the esbuild vite ships, then runs the
// assertions against the real geometry the cursor readout uses.
//
// --bundle, because readout imports the Mercator projection from
// $lib/map/nexrad and the discrete-or-continuous test from ramps/legend. The
// Mercator half is the point of most of these assertions, so a stub would be
// checking arithmetic this file does not ship.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const rampDir = join(root, 'frontend/src/lib/map/ramps/data');
const out = join(mkdtempSync(join(tmpdir(), 'readout-')), 'readout.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/readout.ts'), '--bundle', '--format=esm',
   `--alias:$lib=${join(root, 'frontend/src/lib')}`, `--outfile=${out}`],
  { stdio: 'inherit' });

const ramps = {};
for (const f of readdirSync(rampDir).sort()) {
  if (!f.endsWith('.json')) continue;
  const parsed = JSON.parse(readFileSync(join(rampDir, f), 'utf8'));
  // coverage.json lives here too and is not a ramp.
  if (Array.isArray(parsed.stops)) ramps[f.slice(0, -5)] = parsed;
}

const mod = await import(out);
const { runTests } = await import('./test_map_readout.mjs');
process.exit(runTests(mod, ramps));
