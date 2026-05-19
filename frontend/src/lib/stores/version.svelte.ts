// Sentinel version string for footer display.
//
// Fetched once from /api/version on first import. Single source of
// truth lives in backend/_version.py — bump there at release time
// and the footer picks it up automatically on the next page load.
//
// Wrapped in a $state holder so Svelte 5 reactivity surfaces the
// async fetch result without callers needing to manage the load.

import { url } from '$lib/origin';

export const version = $state<{ value: string | null }>({ value: null });

let _fetched = false;

export async function loadVersion(): Promise<void> {
	if (_fetched) return;
	_fetched = true;
	try {
		const r = await fetch(url('/api/version'));
		if (!r.ok) return;
		const j = (await r.json()) as { version?: string };
		if (j.version) version.value = j.version;
	} catch {
		/* swallow — footer just won't render the version */
	}
}
