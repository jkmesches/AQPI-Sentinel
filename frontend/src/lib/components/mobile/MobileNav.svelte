<script lang="ts">
	import { page } from '$app/state';
	import { sentinel } from '$lib/stores/state.svelte';

	const alarmCount = $derived(sentinel.alarms.length);
	const path = $derived(page.url.pathname);

	function activeClass(prefix: string): string {
		const exact = prefix === '/m'
			? path === '/m'
			: path === prefix || path.startsWith(prefix + '/');
		return exact
			? 'text-[var(--color-accent)]'
			: 'text-[var(--color-muted)]';
	}
</script>

<nav
	class="fixed inset-x-0 bottom-0 z-30 flex border-t border-[var(--color-border)] bg-[var(--color-surface)]"
	style="padding-bottom: env(safe-area-inset-bottom, 0);"
>
	<a href="/m" class="mob-tab {activeClass('/m')}" aria-label="Status">
		<svg viewBox="0 0 24 24" class="mob-icon" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
			<circle cx="12" cy="12" r="9" />
			<circle cx="12" cy="12" r="3.5" fill="currentColor" stroke="none" />
		</svg>
		<span class="mob-label">Status</span>
	</a>

	<a href="/m/alarms" class="mob-tab {activeClass('/m/alarms')}" aria-label="Alarms">
		<span class="relative">
			<svg viewBox="0 0 24 24" class="mob-icon" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
				<path d="M6 8a6 6 0 1 1 12 0v5l1.5 2.5h-15L6 13V8z" />
				<path d="M10 19a2 2 0 1 0 4 0" />
			</svg>
			{#if alarmCount > 0}
				<span class="absolute -right-1 -top-1 min-w-[14px] rounded-full bg-[var(--color-fail)] px-1 text-center text-[9px] font-semibold leading-[14px] text-white">
					{alarmCount}
				</span>
			{/if}
		</span>
		<span class="mob-label">Alarms</span>
	</a>

	<a href="/m/more" class="mob-tab {activeClass('/m/more')}" aria-label="More">
		<svg viewBox="0 0 24 24" class="mob-icon" fill="currentColor">
			<circle cx="5" cy="12" r="1.8" />
			<circle cx="12" cy="12" r="1.8" />
			<circle cx="19" cy="12" r="1.8" />
		</svg>
		<span class="mob-label">More</span>
	</a>
</nav>

<style>
	.mob-tab {
		flex: 1;
		min-height: 56px;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 2px;
		padding: 6px 4px;
		font-size: 10px;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		transition: color 0.12s ease;
		-webkit-tap-highlight-color: transparent;
	}
	.mob-icon {
		width: 22px;
		height: 22px;
		stroke-linejoin: round;
	}
	.mob-label {
		font-weight: 500;
	}
</style>
