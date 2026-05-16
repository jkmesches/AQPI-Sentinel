// Auth store. Loads /api/auth/me on startup, exposes login/logout.

import { goto } from '$app/navigation';

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

	private bootstrapPromise?: Promise<void>;

	bootstrap() {
		if (this.bootstrapPromise) return this.bootstrapPromise;
		this.bootstrapPromise = (async () => {
			try {
				const r = await fetch('/api/auth/me', { credentials: 'include' });
				if (r.ok) {
					this.user = (await r.json()) as AuthUser;
				} else {
					this.user = null;
				}
			} catch {
				this.user = null;
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
				credentials: 'include',
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
			this.user = (await r.json()) as AuthUser;
			return { ok: true };
		} catch (e) {
			const msg = (e as Error).message;
			this.error = msg;
			return { ok: false, error: msg };
		}
	}

	async logout(): Promise<void> {
		try {
			await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' });
		} catch { /* */ }
		this.user = null;
		// drop the cached bootstrap so a subsequent navigation re-reads
		this.bootstrapPromise = undefined;
		goto('/');
	}

	get isAuthed(): boolean {
		return this.user !== null;
	}
	get isAdmin(): boolean {
		return this.user?.role === 'admin';
	}
}

export const auth = new AuthStore();

if (import.meta.hot) {
	import.meta.hot.dispose(() => { /* nothing to dispose */ });
}
