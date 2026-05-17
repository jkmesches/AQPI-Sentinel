import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		proxy: {
			// === DO NOT REMOVE `ws: true` ===
			//
			// Required for Vite's dev proxy to handle WebSocket upgrades on
			// `/api/ws`. Without it Vite's behaviour for WS routes is
			// undefined — connections appear to succeed at handshake but
			// get tangled with Vite's HMR-WebSocket internals, causing slow
			// memory growth in long-lived dev tabs (~10-15 min until the
			// JS thread wedges). This was the actual root cause of a
			// recurring "tab freezes after idle" bug; ~10 layered defensive
			// fixes around the symptom were less effective than this one
			// flag. See docs/MAINTENANCE.md → "Common pitfalls".
			'/api': { target: 'http://127.0.0.1:8000', ws: true }
		}
	}
});
