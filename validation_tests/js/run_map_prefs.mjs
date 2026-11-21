// Compiles $lib/mapPrefs.ts with the esbuild that ships with vite, then runs
// the assertions against the real module MapView imports.
//
// Bundled, unlike most of these runners. mapPrefs validates its stored
// opacities and its playback speed through the modules that own those
// vocabularies, so compiling the file alone leaves bare `$lib/...` imports
// that Node cannot resolve. The alias is the one vite's config sets.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'mp-')), 'mapPrefs.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/mapPrefs.ts'), '--format=esm', '--bundle',
   `--alias:$lib=${join(root, 'frontend/src/lib')}`, `--outfile=${out}`],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_map_prefs.mjs');
process.exit(runTests(mod));
