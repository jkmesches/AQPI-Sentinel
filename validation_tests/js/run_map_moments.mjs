// Compiles $lib/map/moments.ts with the esbuild vite ships, then runs the
// assertions against the real vocabulary the picker is built from.
//
// The payloads the assertions use are the ones backend/config.py produces for
// each profile, pasted into the test. validation_tests/test_moment_payload.py
// checks they still match what the backend sends.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const out = join(mkdtempSync(join(tmpdir(), 'moments-')), 'moments.mjs');
execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
  [join(root, 'frontend/src/lib/map/moments.ts'), '--format=esm',
   `--outfile=${out}`],
  { stdio: 'inherit' });

const mod = await import(out);
const { runTests } = await import('./test_map_moments.mjs');
process.exit(runTests(mod));
