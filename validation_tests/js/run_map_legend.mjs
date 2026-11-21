// Compiles $lib/map/ramps/legend.ts with the esbuild vite ships, then runs the
// assertions against the real module the legend bar draws from.
//
// --bundle, because legend imports parseHex and colorDistance from invert, and
// the discrete-or-continuous test is a measurement of those distances. A stub
// would decide the question it is supposed to answer.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const rampDir = join(root, 'frontend/src/lib/map/ramps/data');
const out = join(mkdtempSync(join(tmpdir(), 'legend-')), 'legend.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/ramps/legend.ts'), '--bundle',
   '--format=esm', `--outfile=${out}`],
  { stdio: 'inherit' });

const ramps = {};
for (const f of readdirSync(rampDir).sort()) {
  if (!f.endsWith('.json')) continue;
  const parsed = JSON.parse(readFileSync(join(rampDir, f), 'utf8'));
  // coverage.json lives here too and is not a ramp.
  if (Array.isArray(parsed.stops)) ramps[f.slice(0, -5)] = parsed;
}
if (Object.keys(ramps).length === 0) {
  console.error(`no ramps in ${rampDir}`);
  process.exit(2);
}

const mod = await import(out);
const { runTests } = await import('./test_map_legend.mjs');
process.exit(runTests(mod, ramps));
