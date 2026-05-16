// Theme store — auto/light/dark, persisted in localStorage, applies a
// `data-theme="light|dark"` attribute on <html> so the CSS @theme overrides
// can target [data-theme="light"]. The "resolved" derived value is what
// components should react to.

export type ThemeMode = 'auto' | 'light' | 'dark';
const KEY = 'sentinel-theme';

class ThemeStore {
	mode = $state<ThemeMode>('auto');
	systemDark = $state(false);

	readonly resolved = $derived<'light' | 'dark'>(
		this.mode === 'auto' ? (this.systemDark ? 'dark' : 'light') : this.mode
	);

	private mql?: MediaQueryList;
	private mqlHandler = (e: MediaQueryListEvent) => {
		this.systemDark = e.matches;
	};

	start() {
		if (typeof window === 'undefined') return;
		const saved = (localStorage.getItem(KEY) as ThemeMode | null) ?? 'auto';
		if (saved === 'auto' || saved === 'light' || saved === 'dark') this.mode = saved;
		this.mql = window.matchMedia('(prefers-color-scheme: dark)');
		this.systemDark = this.mql.matches;
		this.mql.addEventListener('change', this.mqlHandler);

		// Apply on every change. Using $effect would work but this store is
		// instantiated outside a component, so we run a manual subscription.
		queueMicrotask(() => this.apply());
	}

	stop() {
		this.mql?.removeEventListener('change', this.mqlHandler);
	}

	set(mode: ThemeMode) {
		this.mode = mode;
		localStorage.setItem(KEY, mode);
		this.apply();
	}

	apply() {
		document.documentElement.setAttribute('data-theme', this.resolved);
	}
}

export const theme = new ThemeStore();

// Same HMR cleanup story as state.svelte.ts — the matchMedia listener
// would otherwise leak across hot-reloads.
if (import.meta.hot) {
	import.meta.hot.dispose(() => {
		try { theme.stop(); } catch { /* */ }
	});
}
