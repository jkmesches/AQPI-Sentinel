<script lang="ts">
	import '../app.css';
	import { onMount, onDestroy } from 'svelte';
	import { page } from '$app/state';
	import { sentinel } from '$lib/stores/state.svelte';
	import { theme } from '$lib/stores/theme.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import StatusDot from '$lib/components/StatusDot.svelte';
	import NavLink from '$lib/components/NavLink.svelte';
	import ThemeToggle from '$lib/components/ThemeToggle.svelte';
	import { fmtAge, stageLabel } from '$lib/format';
	import { diag } from '$lib/diag';
	import { installFetchPrefix } from '$lib/origin';
	import { version, loadVersion } from '$lib/stores/version.svelte';

	// Mobile routes (/m/*) supply their own chrome — skip the desktop
	// header / stage strip / overflow-hidden main when we're under /m.
	const isMobile = $derived(page.url.pathname.startsWith('/m'));

	// Routes /api/* fetches to the prod backend host when not on dev's
	// :5173 (i.e. in production, with no reverse proxy). No-op in dev.
	installFetchPrefix();

	let now = $state(new Date());
	let tickTimer: ReturnType<typeof setInterval>;

	onMount(() => {
		theme.start();
		auth.bootstrap();
		sentinel.start(5000);
		loadVersion();
		if (diag.tick) tickTimer = setInterval(() => (now = new Date()), 1000);
	});
	onDestroy(() => {
		theme.stop();
		sentinel.stop();
		if (tickTimer) clearInterval(tickTimer);
	});

	// keep <html data-theme> in sync as resolved value changes
	$effect(() => {
		theme.resolved;
		theme.apply();
	});

	const utc = $derived(now.toISOString().slice(11, 19));
	const localFmt = new Intl.DateTimeFormat([], {
		hour: '2-digit',
		minute: '2-digit',
		second: '2-digit',
		hour12: false,
		timeZoneName: 'short'
	});
	const local = $derived(localFmt.format(now));
	// "19:34:08 MDT" → split for display
	const localParts = $derived.by(() => {
		const m = local.match(/^(\d{2}:\d{2}:\d{2})\s+(.+)$/);
		return m ? { time: m[1], zone: m[2] } : { time: local, zone: '' };
	});
	const counts = $derived(sentinel.rollup?.counts ?? {});
	const totalPass = $derived(Object.values(counts).reduce((a, b) => a + b.pass, 0));
	const totalWarn = $derived(Object.values(counts).reduce((a, b) => a + b.warn, 0));
	const totalFail = $derived(
		Object.values(counts).reduce((a, b) => a + b.fail + b.error, 0)
	);
	const totalSkip = $derived(Object.values(counts).reduce((a, b) => a + (b.skip ?? 0), 0));
	const total = $derived(Object.values(counts).reduce((a, b) => a + b.total, 0));
	const sinceUpdate = $derived(
		sentinel.lastUpdate ? (now.getTime() - sentinel.lastUpdate.getTime()) / 1000 : null
	);

	let { children }: { children: any } = $props();
</script>

{#if isMobile}
	<!-- /m/* routes render their own shell via src/routes/m/+layout.svelte -->
	{@render children?.()}
{:else}
<div class="flex h-full flex-col">
	<!-- HEADER -->
	<header
		class="flex items-center gap-5 border-b border-[var(--color-border)] px-4 py-2.5 text-[12px]"
	>
		<div class="flex items-baseline gap-3">
			<span
				class="text-[14px] font-semibold tracking-[0.22em] text-[var(--color-accent)]"
			>AQPI SENTINEL</span>
			<span class="text-[var(--color-faint)] text-[11px]">·</span>
			<span class="text-[11px] text-[var(--color-muted)] num">radarca.engr.colostate.edu</span>
		</div>

		<nav class="flex items-center gap-1 border-l border-[var(--color-border)] pl-3">
			<NavLink href="/" label="Live" />
			<NavLink href="/timeline" label="Timeline" />
			<NavLink href="/history" label="History" />
			{#if auth.isAdmin}
				<NavLink href="/admin" label="Admin" />
			{/if}
		</nav>

		<div class="ml-auto flex items-center gap-6 text-[11px]">
			<!-- dual clock: UTC + local -->
			<div class="flex items-baseline gap-3">
				<div class="flex items-baseline gap-1">
					<span class="num text-[18px] tracking-wide text-[var(--color-bright)] leading-none">{utc}</span>
					<span class="label">utc</span>
				</div>
				<span class="text-[var(--color-faint)]">·</span>
				<div class="flex items-baseline gap-1">
					<span class="num text-[18px] tracking-wide text-[var(--color-default)] leading-none">{localParts.time}</span>
					<span class="label">{localParts.zone || 'local'}</span>
				</div>
			</div>

			<!-- ratio + WARN/FAIL/SKIP pill — every cell of the sub-strip
			     contributes here so pass + warn + fail + skip = total. Without
			     skip the math didn't add up (e.g. 36 pass + 1 fail vs 38 total). -->
			<div class="flex items-center gap-3 num">
				<span class="text-[var(--color-ok)]">{totalPass}</span>
				{#if totalWarn}<span class="text-[var(--color-warn)]">{totalWarn} W</span>{/if}
				{#if totalFail}<span class="text-[var(--color-fail)]">{totalFail} F</span>{/if}
				{#if totalSkip}<span class="text-[var(--color-muted)]">{totalSkip} S</span>{/if}
				<span class="text-[var(--color-faint)]">/ {total}</span>
			</div>

			<!-- liveness indicator -->
			<div class="flex items-center gap-2">
				{#if sentinel.loading}
					<span class="text-[var(--color-muted)]">…</span>
				{:else if sentinel.error}
					<span class="text-[var(--color-fail)]">{sentinel.error}</span>
				{:else}
					<span
						class="inline-block h-1.5 w-1.5 rounded-full {sinceUpdate !== null && sinceUpdate < 15
							? 'bg-[var(--color-ok)]'
							: 'bg-[var(--color-warn)]'}"
					></span>
					<span class="num text-[var(--color-muted)]">
						{sinceUpdate !== null ? fmtAge(sinceUpdate) : '—'}
					</span>
				{/if}
			</div>

			<ThemeToggle />

			<!-- AUTH CHIP -->
			<div class="flex items-center gap-2 border-l border-[var(--color-border)] pl-3 text-[11px]">
				{#if auth.loading}
					<span class="text-[var(--color-faint)]">…</span>
				{:else if auth.user}
					<span class="num text-[var(--color-bright)]" title={`${auth.user.email} · ${auth.user.role}`}>
						{auth.user.display_name || auth.user.email}
					</span>
					<a
						href="/settings/devices"
						class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]"
						title="Per-device push notification routing"
					>
						devices
					</a>
					<button
						type="button"
						class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-fail)]"
						onclick={() => auth.logout()}
					>
						sign out
					</button>
				{:else}
					<a
						class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]"
						href="/login"
					>
						sign in
					</a>
				{/if}
			</div>
		</div>
	</header>

	<!-- STAGE STRIP -->
	<nav
		class="flex items-center gap-5 border-b border-[var(--color-border)] px-4 py-1.5"
	>
		{#each Object.entries(counts) as [stage, c]}
			<!-- Fall through to 'skip' (gray) when nothing in the stage
			     is actually healthy — a category whose rows are all
			     cascade-demoted shouldn't display green. -->
			{@const s = c.fail || c.error ? 'fail'
				: c.warn ? 'warn'
				: c.pass ? 'pass'
				: 'skip'}
			<div class="flex items-center gap-2 text-[11px]" title={`Internal stage: ${stage}`}>
				<StatusDot status={s} size={7} />
				<span class="label tracking-[0.18em] text-[var(--color-default)]">{stageLabel(stage)}</span>
				<span class="num text-[10.5px] text-[var(--color-muted)]">{c.pass}/{c.total}</span>
				{#if c.warn}<span class="num text-[10.5px] text-[var(--color-warn)]">{c.warn} W</span>{/if}
				{#if c.fail + c.error}<span class="num text-[10.5px] text-[var(--color-fail)]">{c.fail + c.error} F</span>{/if}
			</div>
			<span class="text-[var(--color-faint)]">·</span>
		{/each}
		{#if sentinel.alarms.length}
			<div class="ml-auto flex items-center gap-2 text-[11px]">
				<span class="label text-[var(--color-fail)]">alarms</span>
				<span class="num text-[var(--color-bright)]">{sentinel.alarms.length} open</span>
			</div>
		{/if}
	</nav>

	{#if diag.anyDisabled}
		<div class="bg-[var(--color-warn)]/15 border-b border-[var(--color-warn)]/40 px-4 py-1 text-[11px] num text-[var(--color-warn)]">
			DIAG: disabled = {diag.raw.join(', ')}
			<a class="ml-3 underline" href={location.pathname}>clear</a>
		</div>
	{/if}
	<main class="flex-1 overflow-hidden">
		{@render children?.()}
	</main>

	<!-- Tasteful attribution footer. Muted single-line strip across the
	     bottom; doesn't shout, but it's there. Links open in a new tab. -->
	<footer
		class="flex items-center justify-between gap-3 border-t border-[var(--color-border)] bg-[var(--color-canvas)]/60 px-4 py-1.5 text-[10.5px] text-[var(--color-faint)]"
	>
		<span>
			Built by
			<a
				class="text-[var(--color-muted)] hover:text-[var(--color-bright)] transition-colors"
				href="https://github.com/jkmesches"
				target="_blank"
				rel="noopener"
			>Joseph Mesches</a>
		</span>
		<span class="flex items-center gap-3">
			{#if version.value}
				<span class="num text-[var(--color-faint)]">v{version.value}</span>
				<span class="text-[var(--color-faint)]">·</span>
			{/if}
			<a
				class="text-[var(--color-muted)] hover:text-[var(--color-bright)] transition-colors"
				href="https://jkmesches.github.io/SentinelProject/"
				target="_blank"
				rel="noopener"
			>documentation</a>
			<span class="text-[var(--color-faint)]">·</span>
			<a
				class="text-[var(--color-muted)] hover:text-[var(--color-bright)] transition-colors"
				href="/api/docs"
				target="_blank"
				rel="noopener"
			>API</a>
			<span class="text-[var(--color-faint)]">·</span>
			<a
				class="text-[var(--color-muted)] hover:text-[var(--color-bright)] transition-colors"
				href="https://radarca.engr.colostate.edu"
				target="_blank"
				rel="noopener"
			>radarca · CSU</a>
		</span>
	</footer>
</div>
{/if}
