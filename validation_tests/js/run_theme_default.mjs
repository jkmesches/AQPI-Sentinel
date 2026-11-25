// The theme a first visit gets, decided in two places: the theme store, and the
// pre-paint script in app.html that runs before any module loads. If they
// disagree, the page paints in one theme and switches to the other on mount.
// Both are read as source, since the store uses Svelte runes.
//
// Run:  node validation_tests/js/run_theme_default.mjs
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const root = resolve(new URL('../..', import.meta.url).pathname);
const read = (p) => readFileSync(`${root}/${p}`, 'utf8');
const store = read('frontend/src/lib/stores/theme.svelte.ts');
const html = read('frontend/src/app.html');

const failures = [];
const check = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  (${detail})` : ''}`);
  if (!cond) failures.push(label);
};

const storeDefault = store.match(/DEFAULT_MODE: ThemeMode = '(\w+)'/)?.[1];
const htmlDefault = html.match(/getItem\('sentinel-theme'\) \|\| '(\w+)'/)?.[1];
check('the store defaults to dark', storeDefault === 'dark', storeDefault);
check('the pre-paint script defaults to the same mode', htmlDefault === storeDefault,
      `${htmlDefault} vs ${storeDefault}`);
check('the store starts in DEFAULT_MODE', /mode = \$state<ThemeMode>\(DEFAULT_MODE\)/.test(store));
check('the store falls back to DEFAULT_MODE when nothing is saved',
      /getItem\(KEY\)[^;]*\?\? DEFAULT_MODE/.test(store));

console.log(failures.length ? `\n${failures.length} FAILED`
                            : '\nall theme-default assertions passed');
process.exit(failures.length ? 1 : 0);
