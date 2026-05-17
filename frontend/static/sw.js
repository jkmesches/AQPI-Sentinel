/* AQPI Sentinel — service worker for the /m/* PWA shell.
 *
 * Goals:
 *   1. Make the app installable + launchable as an iOS home-screen PWA.
 *   2. Cache the app shell (HTML + main JS/CSS chunks) so the first paint
 *      after launch is instant, even on a flaky cell connection.
 *   3. NEVER cache /api/* — those endpoints have to be live for status to
 *      mean anything. Pass through to the network unchanged.
 *
 * Push handling (P3) hooks live at the bottom of this file. The
 * subscription endpoint isn't wired yet; we register a push listener
 * anyway so iOS will treat this as a push-capable PWA.
 *
 * Bump CACHE_VERSION whenever you change SHELL_URLS so old clients
 * invalidate on next activation.
 */
const CACHE_VERSION = 'sentinel-shell-v1';
const SHELL_URLS = [
	'/m',
	'/manifest.webmanifest',
	'/icons/icon-192.png',
	'/icons/icon-512.png',
	'/icons/apple-touch-icon.png'
];

self.addEventListener('install', (event) => {
	event.waitUntil(
		caches.open(CACHE_VERSION).then((c) => c.addAll(SHELL_URLS)).catch(() => {})
	);
	self.skipWaiting();
});

self.addEventListener('activate', (event) => {
	event.waitUntil(
		caches.keys().then((keys) =>
			Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)))
		)
	);
	self.clients.claim();
});

self.addEventListener('fetch', (event) => {
	const req = event.request;
	if (req.method !== 'GET') return;
	const url = new URL(req.url);

	// Never cache the API. Live data must round-trip.
	if (url.pathname.startsWith('/api/')) return;

	// SvelteKit JS/CSS hashed chunks under /_app/ are content-addressable
	// and safe to cache aggressively — cache-first with background refresh.
	if (url.pathname.startsWith('/_app/')) {
		event.respondWith(
			caches.match(req).then((hit) => {
				if (hit) return hit;
				return fetch(req).then((resp) => {
					if (resp && resp.ok) {
						const clone = resp.clone();
						caches.open(CACHE_VERSION).then((c) => c.put(req, clone));
					}
					return resp;
				});
			})
		);
		return;
	}

	// /m/* page navigations and other shell assets: network-first with
	// cache fallback (so an offline launch still shows the last shell).
	if (url.pathname === '/m' || url.pathname.startsWith('/m/') || SHELL_URLS.includes(url.pathname)) {
		event.respondWith(
			fetch(req)
				.then((resp) => {
					if (resp && resp.ok) {
						const clone = resp.clone();
						caches.open(CACHE_VERSION).then((c) => c.put(req, clone));
					}
					return resp;
				})
				.catch(() => caches.match(req) || caches.match('/m'))
		);
		return;
	}

	// Everything else: passthrough.
});

/* ---------------- Web Push (P3) ----------------
 * The /api/push/subscribe endpoint will land in a follow-up. iOS only
 * delivers pushes once the PWA is installed AND the user has granted
 * notification permission, so we wire the listeners up front so the
 * install-time capability registration is accurate. */
self.addEventListener('push', (event) => {
	let data = {};
	try {
		data = event.data ? event.data.json() : {};
	} catch {
		data = { title: 'AQPI Sentinel', body: event.data?.text?.() ?? '' };
	}
	const title = data.title || 'AQPI Sentinel';
	const opts = {
		body: data.body || '',
		icon: '/icons/icon-192.png',
		badge: '/icons/icon-192.png',
		data: { url: data.url || '/m/alarms' },
		tag: data.tag || undefined,
		renotify: !!data.tag
	};
	event.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', (event) => {
	event.notification.close();
	const target = (event.notification.data && event.notification.data.url) || '/m';
	event.waitUntil(
		self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
			for (const w of wins) {
				if ('focus' in w && new URL(w.url).pathname.startsWith('/m')) {
					w.navigate(target);
					return w.focus();
				}
			}
			if (self.clients.openWindow) return self.clients.openWindow(target);
		})
	);
});
