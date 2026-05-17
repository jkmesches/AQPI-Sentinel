<script lang="ts">
	import MobileNav from '$lib/components/mobile/MobileNav.svelte';
	let { children }: { children: any } = $props();
</script>

<div class="mob-shell">
	<header class="mob-header">
		<span class="text-[14px] font-semibold tracking-[0.20em] text-[var(--color-accent)]">
			AQPI SENTINEL
		</span>
	</header>

	<main class="mob-main">
		{@render children?.()}
	</main>

	<MobileNav />
</div>

<style>
	.mob-shell {
		display: flex;
		flex-direction: column;
		min-height: 100vh;
		min-height: 100svh;
		background: var(--color-canvas);
	}
	.mob-header {
		position: sticky;
		top: 0;
		z-index: 20;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 12px 16px;
		padding-top: calc(12px + env(safe-area-inset-top, 0));
		background: var(--color-surface);
		border-bottom: 1px solid var(--color-border);
	}
	.mob-main {
		flex: 1;
		padding: 12px 12px calc(72px + env(safe-area-inset-bottom, 0));
		overflow-y: auto;
	}
</style>
