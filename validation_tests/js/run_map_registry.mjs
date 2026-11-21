// Compiles the layer registry and the three overlays on it with the esbuild
// vite ships, then runs the assertions against the real modules.
//
// Transpile-only. The registry's only import is a type, and the overlays
// import only from the registry, which is the property that lets a fake map
// stand in for MapLibre here.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const dir = mkdtempSync(join(tmpdir(), 'reg-'));
const build = (name) => {
  const out = join(dir, `${name}.mjs`);
  execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
    [join(root, 'frontend/src/lib/map/layers', `${name}.ts`),
     '--bundle', '--format=esm',
     `--alias:$lib=${join(root, 'frontend/src/lib')}`, `--outfile=${out}`],
    { stdio: 'inherit' });
  return out;
};

const reg = await import(build('registry'));
const geo = await import(build('geography'));
const terr = await import(build('terrain'));
const scars = await import(build('burnScars'));
const gauges = await import(build('streamGauges'));
const { runTests } = await import('./test_map_registry.mjs');
process.exit(await runTests(reg, geo, terr, scars, gauges));
