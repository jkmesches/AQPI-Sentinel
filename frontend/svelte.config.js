// Production: adapter-node produces a standalone Node server that serves
// the built SvelteKit app on $PORT (default 3000). Used by the production
// Docker image. In dev we still go through `vite dev` which doesn't use
// the adapter at all.
import adapter from '@sveltejs/adapter-node';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	kit: {
		adapter: adapter()
	}
};

export default config;
