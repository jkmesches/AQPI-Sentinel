<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import MobileNav from '$lib/components/mobile/MobileNav.svelte';
	import { initInstallCapture } from '$lib/platform.svelte';
	import { version, loadVersion } from '$lib/stores/version.svelte';
	let { children }: { children: any } = $props();

	// Live UTC clock in the header. 1 Hz is plenty for a seconds readout
	// and the timer is harmless across visibility changes; the page is
	// already polling /api/status separately so we don't suspend this
	// when the tab is hidden — the clock just keeps ticking.
	let now = $state(new Date());
	let clockTimer: ReturnType<typeof setInterval>;
	const utcClock = $derived(now.toISOString().slice(11, 19));

	// Register the service worker on /m/* page load. Scope is explicitly
	// /m/ so the desktop pages aren't claimed by the PWA shell. Doing this
	// in the layout (vs. root) keeps install prompts scoped to mobile.
	onMount(() => {
		clockTimer = setInterval(() => (now = new Date()), 1000);
		loadVersion();
		// Capture the install prompt at the LAYOUT level. The
		// `beforeinstallprompt` event fires once per page load before
		// the user navigates anywhere, so deferring this to /m/more's
		// onMount would miss the event entirely on Android Chrome after
		// SvelteKit client-side navigation.
		initInstallCapture();
		if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
		if (!window.isSecureContext) return; // SW needs HTTPS (or localhost)
		navigator.serviceWorker
			.register('/sw.js', { scope: '/m/' })
			.catch((e) => console.warn('SW registration failed:', e));
	});
	onDestroy(() => { if (clockTimer) clearInterval(clockTimer); });
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
		<span class="num text-[13px] tracking-wide text-[var(--color-bright)] ml-auto" title="Current UTC time">
			{utcClock}<span class="ml-1 text-[10px] text-[var(--color-muted)]">UTC</span>
		</span>
	</header>

	<main class="mob-main">
		{@render children?.()}

		<!-- Mobile attribution footer. Sits inside mob-main so it scrolls
		     with the content rather than fixing above the bottom nav.
		     Slightly more vertical breathing room than the desktop variant
		     to keep tap targets at ≥44px. -->
		<footer class="mt-6 border-t border-[var(--color-border)] pt-3 text-center text-[10.5px] text-[var(--color-faint)] leading-relaxed">
			<div>
				Built by
				<a
					class="text-[var(--color-muted)] active:text-[var(--color-bright)] underline-offset-2 hover:underline"
					href="https://github.com/jkmesches"
					target="_blank"
					rel="noopener"
				>Joseph Mesches</a>
			</div>
			<div class="mt-1 flex items-center justify-center gap-3">
				{#if version.value}
					<span class="num">v{version.value}</span>
					<span>·</span>
				{/if}
				<a
					class="text-[var(--color-muted)] active:text-[var(--color-bright)] underline-offset-2 hover:underline"
					href="https://jkmesches.github.io/SentinelProject/"
					target="_blank"
					rel="noopener"
				>documentation</a>
				<span>·</span>
				<a
					class="text-[var(--color-muted)] active:text-[var(--color-bright)] underline-offset-2 hover:underline"
					href="https://radarca.engr.colostate.edu"
					target="_blank"
					rel="noopener"
				>radarca · CSU</a>
			</div>
		</footer>
	</main>

	<MobileNav />
</div>

<style>
	.mob-shell {
		display: flex;
		flex-direction: column;
		/* FIXED height so .mob-main's `flex: 1 + overflow-y: auto` creates
		   an INTERNAL scroll container. With `min-height: 100vh`, the shell
		   would grow with its content and the page would scroll the BODY
		   instead — which makes `.mob-header { position: sticky }` track
		   the body scroll and visually "float" as you scrolled.
		   100svh accounts for iOS Safari's smaller-viewport behavior
		   (URL bar + bottom toolbar). 100vh fallback for browsers that
		   don't support svh. */
		height: 100vh;
		height: 100svh;
		background: var(--color-canvas);
		overflow: hidden;
	}
	.mob-header {
		position: sticky;
		top: 0;
		z-index: 20;
		display: flex;
		align-items: center;
		gap: 12px;
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
