// Compiles $lib/radarStatus.ts with the esbuild vite ships, then runs the
// assertions against the real module both maps import.
//
// --bundle, unlike its siblings here: radarStatus imports isFleetCheck from
// $lib/format and worstOf from $lib/grouping, and a transpile-only build would
// leave those specifiers to resolve against the temp directory and fail. The
// bundle pulls in the real dependencies, so the fleet-check exclusions and the
// `skip`-below-`pass` ranking are exercised as shipped rather than stubbed —
// which matters, because both are assertions in the test.
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const dir = mkdtempSync(join(tmpdir(), 'rs-'));
const build = (src, name) => {
  const out = join(dir, name);
  execFileSync(join(root, 'frontend/node_modules/.bin/esbuild'),
    [join(root, 'frontend/src/lib', src), '--bundle', '--format=esm',
     `--outfile=${out}`],
    { stdio: 'inherit' });
  return out;
};

const mod = await import(build('radarStatus.ts', 'radarStatus.mjs'));
const { runTests } = await import('./test_radar_status.mjs');
process.exit(runTests(mod));
