/**
 * API origin resolver.
 *
 * Dev: backend is at the same origin as the page (Vite proxies /api/* and
 * /api/ws to localhost:8000), so we return '' and every fetch path stays
 * relative like '/api/status'.
 *
 * Prod (Docker, no reverse proxy): backend is at port 8000 on the same
 * host. We can't tell at build time what hostname the user will browse
 * to, so we resolve it at runtime from `window.location.hostname`. The
 * resulting absolute URL is something like 'http://192.168.1.50:8000'.
 *
 * Override via the `PUBLIC_SENTINEL_API_BASE` env var at build time if
 * the backend lives somewhere else (different host, reverse proxy, etc.).
 */

// `import.meta.env.PUBLIC_*` tolerates an undefined var (returns
// undefined). SvelteKit's `$env/static/public` would throw at build
// time if PUBLIC_SENTINEL_API_BASE wasn't explicitly defined.
const _envBase = (import.meta.env.PUBLIC_SENTINEL_API_BASE as string | undefined) ?? '';

function detect(): string {
	if (_envBase) return _envBase.replace(/\/$/, '');
	if (typeof window === 'undefined') return '';
	// Port-3000 = "raw docker compose, no proxy" — backend lives at the
	// sibling port 8000 on the same host. Anything else (5173 dev with
	// Vite proxy, 80/443/empty behind a reverse proxy like Traefik that
	// routes /api/*) is same-origin and we leave URLs relative.
	if (window.location.port === '3000') {
		return `${window.location.protocol}//${window.location.hostname}:8000`;
	}
	return '';
}

export const API_BASE = detect();
export const WS_BASE = API_BASE
	? API_BASE.replace(/^http/, 'ws')
	: '';   // empty → use same-origin in dev

export function url(path: string): string {
	if (path.startsWith('http://') || path.startsWith('https://')) return path;
	return API_BASE + path;
}

export function wsUrl(path: string): string {
	const tok = readToken();
	const base = WS_BASE
		? WS_BASE
		: `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}`;
	const sep = path.includes('?') ? '&' : '?';
	const auth = tok ? `${sep}token=${encodeURIComponent(tok)}` : '';
	return `${base}${path}${auth}`;
}

const TOKEN_KEY = 'sentinel.token';
function readToken(): string | null {
	try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}

/**
 * Install a global fetch interceptor that:
 *   1. Rewrites `/api/...` URLs to `${API_BASE}/api/...` so the browser
 *      reaches the backend regardless of how it found the frontend.
 *   2. Attaches `Authorization: Bearer <token>` from localStorage to
 *      every `/api/*` request, if a token is stored.
 *
 * Idempotent. No-op for non-API URLs. Catches only `fetch()` — for
 * `<img src>`, `window.open`, and MapLibre source URLs we use the
 * explicit `url(...)` / `wsUrl(...)` helpers, which is fine because
 * those endpoints (image proxy, scan PNGs) don't require auth.
 */
export function installFetchPrefix() {
	if (typeof window === 'undefined') return;
	const w = window as typeof window & { __sentinelFetchPatched?: boolean };
	if (w.__sentinelFetchPatched) return;
	w.__sentinelFetchPatched = true;
	const orig = window.fetch.bind(window);

	function withAuth(init: RequestInit | undefined): RequestInit {
		const tok = readToken();
		if (!tok) return init ?? {};
		const headers = new Headers(init?.headers);
		if (!headers.has('authorization')) headers.set('authorization', `Bearer ${tok}`);
		return { ...(init ?? {}), headers };
	}

	window.fetch = (input, init) => {
		// String URL
		if (typeof input === 'string') {
			if (input.startsWith('/api/')) {
				return orig(API_BASE + input, withAuth(init));
			}
			return orig(input, init);
		}
		// URL object
		if (input instanceof URL) {
			if (input.pathname.startsWith('/api/')) {
				return orig(API_BASE + input.pathname + input.search, withAuth(init));
			}
			return orig(input, init);
		}
		// Request object
		if (input instanceof Request) {
			const u = new URL(input.url);
			if (u.pathname.startsWith('/api/') && u.origin === window.location.origin) {
				return orig(API_BASE + u.pathname + u.search, withAuth(init ?? input));
			}
			return orig(input, init);
		}
		return orig(input as RequestInfo, init);
	};
}
