// Auth store. Bearer-token sessions stored in localStorage.

import { goto } from '$app/navigation';

const TOKEN_KEY = 'sentinel.token';

export interface AuthUser {
	id: number;
	email: string;
	role: 'admin' | 'user';
	display_name: string | null;
}

class AuthStore {
	user = $state<AuthUser | null>(null);
	loading = $state(true);
	error = $state<string | null>(null);
	// Token is read by lib/origin.installFetchPrefix() to attach the
	// Authorization header to every /api/* fetch, and by lib/ws.ts to
	// append ?token=... on the WebSocket URL.
	token = $state<string | null>(null);

	private bootstrapPromise?: Promise<void>;

	bootstrap() {
		if (this.bootstrapPromise) return this.bootstrapPromise;
		this.bootstrapPromise = (async () => {
			try {
				if (typeof localStorage !== 'undefined') {
					this.token = localStorage.getItem(TOKEN_KEY);
				}
				if (!this.token) { this.loading = false; return; }
				const r = await fetch('/api/auth/me');   // installFetchPrefix adds header
				if (r.ok) {
					this.user = (await r.json()) as AuthUser;
				} else {
					this.clearToken();
				}
			} catch {
				this.clearToken();
			} finally {
				this.loading = false;
			}
		})();
		return this.bootstrapPromise;
	}

	async login(email: string, password: string): Promise<{ ok: true } | { ok: false; error: string }> {
		this.error = null;
		try {
			const r = await fetch('/api/auth/login', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ email, password })
			});
			if (!r.ok) {
				let msg = `HTTP ${r.status}`;
				try {
					const j = await r.json();
					msg = j?.detail ?? msg;
				} catch { /* */ }
				this.error = msg;
				return { ok: false, error: msg };
			}
			const j = await r.json();
			this.setToken(j.token);
			this.user = {
				id: j.id, email: j.email, role: j.role, display_name: j.display_name
			};
			return { ok: true };
		} catch (e) {
			const msg = (e as Error).message;
			this.error = msg;
			return { ok: false, error: msg };
		}
	}

	async logout(): Promise<void> {
		try {
			await fetch('/api/auth/logout', { method: 'POST' });
		} catch { /* */ }
		this.clearToken();
		this.user = null;
		this.bootstrapPromise = undefined;
		goto('/');
	}

	private setToken(t: string) {
		this.token = t;
		try { localStorage.setItem(TOKEN_KEY, t); } catch { /* */ }
	}
	private clearToken() {
		this.token = null;
		try { localStorage.removeItem(TOKEN_KEY); } catch { /* */ }
	}

	get isAuthed(): boolean { return this.user !== null; }
	get isAdmin(): boolean  { return this.user?.role === 'admin'; }
}

export const auth = new AuthStore();

if (import.meta.hot) {
	import.meta.hot.dispose(() => { /* nothing to dispose */ });
}
