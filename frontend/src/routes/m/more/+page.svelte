<script lang="ts">
	import { onMount } from 'svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { theme, type ThemeMode } from '$lib/stores/theme.svelte';
	import { pushCapable, currentSubscription, enablePush, disablePush } from '$lib/push';

	function viewDesktop() {
		// 1-year cookie picked up by hooks.server.ts to suppress the
		// mobile-UA redirect. Bounce to root so they see the desktop UI.
		document.cookie = 'sentinel-desktop=1; path=/; max-age=31536000; samesite=lax';
		location.href = '/';
	}

	function setMode(m: ThemeMode) { theme.mode = m; }

	// "Already installed" detection: standalone display-mode in any
	// browser, or iOS's non-standard navigator.standalone.
	let installed = $state(false);
	let isIos = $state(false);

	// Web Push state
	let pushSupported = $state(false);
	let pushSubscribed = $state(false);
	let pushBusy = $state(false);
	let pushError = $state<string | null>(null);
	let permission = $state<NotificationPermission | 'unsupported'>('default');

	async function refreshPush() {
		pushSupported = pushCapable();
		if (!pushSupported) { permission = 'unsupported'; return; }
		permission = Notification.permission;
		const sub = await currentSubscription();
		pushSubscribed = !!sub;
	}

	async function toggleEnable() {
		pushBusy = true; pushError = null;
		try {
			const r = await enablePush();
			if (!r.ok) pushError = r.reason ?? 'failed';
			await refreshPush();
		} finally { pushBusy = false; }
	}
	async function toggleDisable() {
		pushBusy = true; pushError = null;
		try {
			await disablePush();
			await refreshPush();
		} finally { pushBusy = false; }
	}

	onMount(() => {
		if (typeof window === 'undefined') return;
		installed = window.matchMedia('(display-mode: standalone)').matches
			|| (navigator as any).standalone === true;
		isIos = /iPhone|iPod/i.test(navigator.userAgent);
		refreshPush();
	});
</script>

<section class="mb-5">
	<div class="mb-2 text-[10px] uppercase tracking-[0.18em] text-[var(--color-muted)]">account</div>
	<div class="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-3">
		{#if auth.loading}
			<div class="text-[13px] text-[var(--color-muted)]">…</div>
		{:else if auth.user}
			<div class="text-[14px] num text-[var(--color-bright)]">
				{auth.user.display_name || auth.user.email}
			</div>
			<div class="num mt-1 text-[11px] text-[var(--color-muted)]">
				{auth.user.email} · {auth.user.role}
			</div>
			<button
				type="button"
				onclick={() => auth.logout()}
				class="mt-3 min-h-[44px] w-full rounded border border-[var(--color-border-strong)] text-[12px] uppercase tracking-wider text-[var(--color-fail)]"
				style="-webkit-tap-highlight-color: transparent;"
			>sign out</button>
		{:else}
			<a
				href="/login?next={encodeURIComponent('/m')}"
				class="block min-h-[44px] rounded border border-[var(--color-border-strong)] text-center text-[12px] uppercase leading-[44px] tracking-wider text-[var(--color-bright)]"
				style="-webkit-tap-highlight-color: transparent;"
			>sign in</a>
		{/if}
	</div>
</section>

<section class="mb-5">
	<div class="mb-2 text-[10px] uppercase tracking-[0.18em] text-[var(--color-muted)]">notifications</div>
	<div class="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-3">
		{#if !pushSupported}
			<div class="text-[12px] text-[var(--color-muted)]">
				This browser doesn't support Web Push.
				{#if isIos && !installed}
					<br /><span class="text-[var(--color-faint)]">iOS only delivers push to PWAs that have been Added to Home Screen — install the app first (see Install above).</span>
				{/if}
			</div>
		{:else if !auth.user}
			<div class="text-[12px] text-[var(--color-muted)]">
				<a href="/login?next={encodeURIComponent('/m/more')}" class="underline">Sign in</a> to receive alarm notifications.
			</div>
		{:else if pushSubscribed}
			<div class="text-[13px] text-[var(--color-ok)]">✓ Notifications enabled on this device.</div>
			<div class="mt-1 text-[11px] text-[var(--color-muted)]">You'll get a push when any alarm fires.</div>
			<a
				href="/m/push-settings"
				class="mt-3 inline-flex min-h-[44px] w-full items-center justify-center rounded border border-[var(--color-accent)]/40 bg-[var(--color-accent)]/5 text-[12px] uppercase tracking-wider text-[var(--color-accent)]"
				style="-webkit-tap-highlight-color: transparent;"
			>customize routing →</a>
			<button
				type="button"
				onclick={toggleDisable}
				disabled={pushBusy}
				class="mt-2 min-h-[44px] w-full rounded border border-[var(--color-border-strong)] text-[12px] uppercase tracking-wider text-[var(--color-fail)] disabled:opacity-50"
				style="-webkit-tap-highlight-color: transparent;"
			>{pushBusy ? 'disabling…' : 'disable notifications'}</button>
		{:else}
			<div class="text-[12px] text-[var(--color-default)] leading-relaxed">
				Get a push notification on this device when any alarm fires.
				{#if isIos && !installed}
					<br /><span class="text-[var(--color-warn)]">iOS requires the app to be installed to home screen first.</span>
				{/if}
				{#if permission === 'denied'}
					<br /><span class="text-[var(--color-fail)]">Notification permission was denied — re-enable it in your browser/OS settings, then try again.</span>
				{/if}
			</div>
			<button
				type="button"
				onclick={toggleEnable}
				disabled={pushBusy || (isIos && !installed) || permission === 'denied'}
				class="mt-3 min-h-[44px] w-full rounded border border-[var(--color-accent)] bg-[var(--color-accent)]/10 text-[12px] uppercase tracking-wider text-[var(--color-accent)] disabled:opacity-50"
				style="-webkit-tap-highlight-color: transparent;"
			>{pushBusy ? 'enabling…' : 'enable notifications'}</button>
		{/if}
		{#if pushError}
			<div class="mt-2 text-[11px] text-[var(--color-fail)] num">{pushError}</div>
		{/if}
	</div>
</section>

<section class="mb-5">
	<div class="mb-2 text-[10px] uppercase tracking-[0.18em] text-[var(--color-muted)]">theme</div>
	<div class="grid grid-cols-3 gap-2">
		{#each ['light', 'auto', 'dark'] as m}
			{@const isActive = theme.mode === m}
			<button
				type="button"
				onclick={() => setMode(m as ThemeMode)}
				class="min-h-[44px] rounded border text-[12px] uppercase tracking-wider {isActive
					? 'border-[var(--color-accent)] bg-[var(--color-accent)]/10 text-[var(--color-accent)]'
					: 'border-[var(--color-border-strong)] text-[var(--color-default)]'}"
				style="-webkit-tap-highlight-color: transparent;"
			>{m}</button>
		{/each}
	</div>
</section>

{#if !installed}
	<section class="mb-5">
		<div class="mb-2 text-[10px] uppercase tracking-[0.18em] text-[var(--color-muted)]">install</div>
		<div class="rounded-md border border-[var(--color-accent)]/40 bg-[var(--color-accent)]/10 px-3 py-3 text-[12px] text-[var(--color-default)] leading-relaxed">
			{#if isIos}
				Add Sentinel to your home screen for full-screen launch and
				push notifications:<br />
				<span class="text-[var(--color-muted)]">tap</span> <span class="text-[var(--color-bright)]">Share</span>
				<span class="text-[var(--color-muted)]">→</span>
				<span class="text-[var(--color-bright)]">Add to Home Screen</span>.
			{:else}
				Add Sentinel to your home screen via your browser's menu for
				full-screen launch and push notifications.
			{/if}
		</div>
	</section>
{/if}

<section class="mb-5">
	<div class="mb-2 text-[10px] uppercase tracking-[0.18em] text-[var(--color-muted)]">view</div>
	<button
		type="button"
		onclick={viewDesktop}
		class="block min-h-[44px] w-full rounded border border-[var(--color-border-strong)] text-center text-[12px] uppercase tracking-wider text-[var(--color-bright)]"
		style="-webkit-tap-highlight-color: transparent;"
	>switch to desktop view</button>
	<div class="mt-2 px-1 text-[11px] text-[var(--color-faint)]">
		Stays in desktop view until cleared. Open <span class="num">?mobile=1</span> on any page to come back.
	</div>
</section>

<section>
	<div class="mb-2 text-[10px] uppercase tracking-[0.18em] text-[var(--color-muted)]">about</div>
	<div class="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-3 text-[12px] text-[var(--color-muted)]">
		<div class="num">AQPI Sentinel — radarca.engr.colostate.edu</div>
		<div class="num mt-1 text-[var(--color-faint)]">v0.1.0 · mobile</div>
	</div>
</section>
