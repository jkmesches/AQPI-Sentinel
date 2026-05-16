import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		proxy: {
			// `ws: true` is required for Vite to proxy WebSocket upgrades
			// (e.g. /api/ws). Without it Vite's handling of WS routes is
			// undefined — connections may succeed but tangle with the HMR
			// WebSocket internals, which is implicated in the long-lived
			// memory growth + idle-tab freeze pattern we've been chasing.
			'/api': { target: 'http://127.0.0.1:8000', ws: true }
		}
	}
});
