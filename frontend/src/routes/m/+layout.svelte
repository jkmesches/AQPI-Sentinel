<script lang="ts">
	import { onMount } from 'svelte';
	import MobileNav from '$lib/components/mobile/MobileNav.svelte';
	let { children }: { children: any } = $props();

	// Register the service worker on /m/* page load. Scope is explicitly
	// /m/ so the desktop pages aren't claimed by the PWA shell. Doing this
	// in the layout (vs. root) keeps install prompts scoped to mobile.
	onMount(() => {
		if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
		if (!window.isSecureContext) return; // SW needs HTTPS (or localhost)
		navigator.serviceWorker
			.register('/sw.js', { scope: '/m/' })
			.catch((e) => console.warn('SW registration failed:', e));
	});
</script>

<svelte:head>
	<!-- PWA install — manifest, theme color, and iOS-specific tags. -->
	<link rel="manifest" href="/manifest.webmanifest" />
	<meta name="theme-color" content="#1E4D2B" />
	<!-- iOS Safari is the primary target. apple-touch-icon is required for
	     a polished Add-to-Home-Screen experience; the capable + status-bar
	     tags are what make the installed PWA launch full-screen. -->
	<link rel="apple-touch-icon" sizes="180x180" href="/icons/apple-touch-icon.png" />
	<meta name="apple-mobile-web-app-capable" content="yes" />
	<meta name="mobile-web-app-capable" content="yes" />
	<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
	<meta name="apple-mobile-web-app-title" content="AQPI Sent" />
</svelte:head>

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
		/* Bottom padding clears the nav (mob-tab min-height 48px + 6px top
		   pad + 14px bottom pad = 68px chrome) plus the iOS home-bar safe
		   area. Bumped from 72px after iPhone 15 testing — the nav was
		   overlapping the home indicator gesture zone. */
		padding: 12px 12px calc(86px + env(safe-area-inset-bottom, 0));
		overflow-y: auto;
	}
</style>
