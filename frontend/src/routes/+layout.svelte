<script lang="ts">
	// Deployment identity, from /api/version. Both of these were hard-coded to
	// AQPI, so an XQPI instance called itself AQPI Sentinel and named radarca
	// as its data source on a page that never contacts radarca. getSite is
	// memoised, so this shares one request with the map.
	import { getSite } from '$lib/site';
	let siteName = $state('AQPI Sentinel');
	// Blank until /api/version answers. See the note on FALLBACK_SOURCE.
	let dataSource = $state('');
	$effect(() => {
		getSite().then((s) => { siteName = s.name; dataSource = s.dataSource; });
	});

	import '../app.css';
	import { onMount, onDestroy } from 'svelte';
	import { page } from '$app/state';
	import { sentinel } from '$lib/stores/state.svelte';
	import { theme } from '$lib/stores/theme.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import StatusDot from '$lib/components/StatusDot.svelte';
	import DisclosureArrow from '$lib/components/DisclosureArrow.svelte';
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
	// fail and error are counted separately. Folding them together overstated
	// failures on the most-read number in the app: an upstream API timeout
	// ("we could not measure") is not a radar outage ("the thing is broken").
	const totalFail = $derived(Object.values(counts).reduce((a, b) => a + b.fail, 0));
	const totalError = $derived(Object.values(counts).reduce((a, b) => a + b.error, 0));
	const sinceUpdate = $derived(
		sentinel.lastUpdate ? (now.getTime() - sentinel.lastUpdate.getTime()) / 1000 : null
	);

	let { children }: { children: any } = $props();

	// The narrow header's menu and stage list, closed again on each page so
	// a link chosen from the menu does not leave it covering the new page.
	let menuOpen = $state(false);
	let stagesOpen = $state(false);
	$effect(() => {
		page.url.pathname;
		menuOpen = false;
	});
	const stripWorst = $derived(
		totalFail || totalError ? 'fail' : totalWarn ? 'warn' : totalPass ? 'pass' : 'skip'
	);
	const stageCount = $derived(Object.keys(counts).length);
	const stagesPassing = $derived(
		Object.values(counts).filter((c) => c.pass && !c.warn && !c.fail && !c.error).length
	);
</script>

<svelte:head><title>{siteName}</title></svelte:head>

{#if isMobile}
	<!-- /m/* routes render their own shell via src/routes/m/+layout.svelte -->
	{@render children?.()}
{:else}
<div class="flex h-full flex-col">
	<!-- HEADER.
	     Wraps rather than overflowing. A real phone is sent to /m by the
	     UA sniff in hooks.server.ts, so this is the narrowed desktop
	     window, where the row used to measure 1332 px whatever the
	     viewport was and every page scrolled sideways under it. A page
	     that scrolls sideways turns a drag on the map into a pan of the
	     page. -->
	<!-- Below 1024 px the header is two rows: the name and a menu button,
	     then the UTC clock. The page links, the theme
	     toggle and sign-in open from the menu, and the data source, local
	     clock and update age wait for a wider window. Unfolded, the same
	     header took five rows of a 320 px phone before any page content, a
	     third of a phone held sideways (734 x 343), and three rows of a
	     768 px tablet, taller than the folded header just below it. -->
	<header
		class="band flex flex-wrap items-center gap-x-5 gap-y-2 px-4 py-2.5 text-[12px]"
	>
		<div class="order-1 flex min-w-0 items-baseline gap-3 lg:order-none">
			<span
				class="text-[14px] font-semibold tracking-[0.22em] text-[var(--color-accent)]"
			>{siteName.toUpperCase()}</span>
			<!-- The separator goes with the source. A deployment whose backend
			     does not name one left the header reading "AQPI SENTINEL ·". -->
			{#if dataSource}
				<span class="hidden text-[var(--color-faint)] text-[12px] lg:inline">·</span>
				<span
					class="hidden text-[12px] text-[var(--color-muted)] num lg:inline"
					data-data-source>{dataSource}</span>
			{/if}
		</div>

		<button
			type="button"
			class="order-2 ml-auto flex h-8 w-8 items-center justify-center rounded-sm border border-[var(--color-border-strong)] text-[var(--color-default)] hover:text-[var(--color-bright)] lg:hidden"
			aria-expanded={menuOpen}
			aria-controls="site-nav site-tools"
			aria-label="Menu"
			onclick={() => (menuOpen = !menuOpen)}
		>
			<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
				<path
					d={menuOpen ? 'M3.5 3.5 L12.5 12.5 M12.5 3.5 L3.5 12.5' : 'M2 4 H14 M2 8 H14 M2 12 H14'}
					stroke="currentColor"
					stroke-width="1.6"
					stroke-linecap="round"
					fill="none"
				/>
			</svg>
		</button>

		<nav
			id="site-nav"
			class="{menuOpen ? 'flex' : 'hidden'} order-4 basis-full flex-wrap items-center gap-1 lg:order-none lg:flex lg:basis-auto lg:border-l lg:border-[var(--color-border)] lg:pl-3"
			aria-label="Pages"
		>
			<NavLink href="/" label="Map" />
			<NavLink href="/dashboard" label="Dashboard" />
			<NavLink href="/timeline" label="Timeline" />
			<NavLink href="/history" label="History" />
			<NavLink href="/faq" label="FAQ" />
			{#if auth.isAdmin}
				<NavLink href="/admin" label="Admin" />
			{/if}
		</nav>

		<!-- `contents` below 1024 px, so the two groups inside take their own
		     places in the header's order; one right-aligned group above. -->
		<div class="contents lg:ml-auto lg:flex lg:flex-wrap lg:items-center lg:justify-end lg:gap-x-6 lg:gap-y-1">
		<div class="order-3 flex basis-full flex-wrap items-center gap-x-6 gap-y-1 text-[12px] lg:order-none lg:basis-auto">
			<!-- dual clock: UTC + local -->
			<div class="flex items-baseline gap-3">
				<div class="flex items-baseline gap-1">
					<span class="num text-[18px] tracking-wide text-[var(--color-bright)] leading-none">{utc}</span>
					<span class="label">utc</span>
				</div>
				<span class="hidden text-[var(--color-faint)] lg:inline">·</span>
				<div class="hidden items-baseline gap-1 lg:flex">
					<span class="num text-[18px] tracking-wide text-[var(--color-default)] leading-none">{localParts.time}</span>
					<span class="label">{localParts.zone || 'local'}</span>
				</div>
			</div>

			<!-- liveness indicator -->
			<div class="hidden items-center gap-2 lg:flex">
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
		</div>

		<div
			id="site-tools"
			class="{menuOpen ? 'flex' : 'hidden'} order-5 basis-full flex-wrap items-center gap-x-6 gap-y-2 lg:order-none lg:flex lg:basis-auto"
		>
			<ThemeToggle />

			<!-- AUTH CHIP -->
			<div class="flex items-center gap-2 border-l border-[var(--color-border)] pl-3 text-[12px]">
				{#if auth.loading}
					<span class="text-[var(--color-faint)]">…</span>
				{:else if auth.user}
					<span class="num text-[var(--color-bright)]" title={`${auth.user.email} · ${auth.user.role}`}>
						{auth.user.display_name || auth.user.email}
					</span>
					<a
						href="/settings/devices"
						class="text-[12px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]"
						title="Per-device push notification routing"
					>
						devices
					</a>
					<button
						type="button"
						class="text-[12px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-fail)]"
						onclick={() => auth.logout()}
					>
						sign out
					</button>
				{:else}
					<a
						class="text-[12px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]"
						href="/login"
					>
						sign in
					</a>
				{/if}
			</div>
		</div>
		</div>
	</header>

	<!-- STAGE STRIP. Wraps for the reason the header does: six stages and
	     an alarm count in one non-wrapping row measured 885 px whatever
	     the viewport was. Below 1024 px it is one summary row that opens the
	     list, since wrapped it took six rows of a phone. -->
	<nav
		class="border-b border-[var(--color-border)] px-4 py-1.5"
		aria-label="Pipeline stages"
	>
		<button
			type="button"
			class="flex w-full items-center gap-2 text-left text-[12px] lg:hidden"
			aria-expanded={stagesOpen}
			aria-controls="stage-list"
			onclick={() => (stagesOpen = !stagesOpen)}
		>
			<StatusDot status={stripWorst} size={7} />
			<span class="label tracking-[0.18em] text-[var(--color-default)]">Stages</span>
			<span class="num whitespace-nowrap text-[var(--color-muted)]">{stagesPassing}/{stageCount} ok</span>
			{#if sentinel.alarms.length}
				<span class="num ml-auto whitespace-nowrap text-[var(--color-fail)]">
					{sentinel.alarms.length} alarm{sentinel.alarms.length === 1 ? '' : 's'}
				</span>
			{/if}
			<span class="flex {sentinel.alarms.length ? '' : 'ml-auto'}">
				<DisclosureArrow open={stagesOpen} />
			</span>
		</button>
		<!-- Eight stages and the alarm count took 2,127 px in one row, so the
		     strip wrapped on a 1920 px screen. The dots separate the stages,
		     without the "·" spans and the 28 px of gap each one added, and
		     the names track at 0.06em, for 1,744 px. The names use utilities
		     over `.label`, whose unlayered letter-spacing and color override
		     any utility. -->
		<div
			id="stage-list"
			class="{stagesOpen ? 'flex' : 'hidden'} flex-wrap items-center gap-x-4 gap-y-0.5 pt-1.5 lg:flex lg:pt-0"
		>
		{#each Object.entries(counts) as [stage, c]}
			<!-- Fall through to 'skip' (gray) when nothing in the stage
			     is actually healthy — a category whose rows are all
			     cascade-demoted shouldn't display green. -->
			{@const s = c.fail || c.error ? 'fail'
				: c.warn ? 'warn'
				: c.pass ? 'pass'
				: 'skip'}
			<div class="flex items-center gap-1.5 whitespace-nowrap text-[12px]" title={`Internal stage: ${stage}`}>
				<StatusDot status={s} size={7} />
				<span class="font-medium uppercase tracking-[0.06em] text-[var(--color-muted)]">{stageLabel(stage)}</span>
				<span class="num text-[var(--color-muted)]">{c.pass}/{c.total}</span>
				{#if c.warn}<span class="num text-[var(--color-warn)]">{c.warn} W</span>{/if}
				{#if c.fail}<span class="num text-[var(--color-fail)]">{c.fail} F</span>{/if}
				{#if c.error}<span class="num text-[var(--color-error)]">{c.error} E</span>{/if}
			</div>
		{/each}
		{#if sentinel.alarms.length}
			<div class="ml-auto hidden items-center gap-1.5 whitespace-nowrap text-[12px] lg:flex">
				<span class="font-medium uppercase tracking-[0.06em] text-[var(--color-muted)]">alarms</span>
				<span class="num text-[var(--color-bright)]">{sentinel.alarms.length} open</span>
			</div>
		{/if}
		</div>
	</nav>

	{#if diag.anyDisabled}
		<div
			class="bg-[var(--color-warn)]/15 border-b border-[var(--color-warn)]/40 px-4 py-1 text-[12px] num text-[var(--color-warn)]"
			role="region"
			aria-label="Diagnostic flags"
		>
			<!-- Names the flags rather than calling them disabled: `probe`
			     turns a snapshot ON, and a banner reading "disabled = probe"
			     describes the opposite of what the page is doing. -->
			DIAG: {diag.raw.join(', ')}
			<a class="ml-3 underline" href={location.pathname}>clear</a>
		</div>
	{/if}
	<main class="flex-1 overflow-hidden">
		{@render children?.()}
	</main>

	<!-- Tasteful attribution footer. Muted single-line strip across the
	     bottom; doesn't shout, but it's there. Links open in a new tab.
	     Wraps below about 380 px: unwrapped, the link group ran to 376 px
	     and scrolled every page sideways in a 320 px window. -->
	<footer
		class="site-footer flex flex-wrap items-center justify-between gap-x-3 gap-y-0.5 border-t border-[var(--color-border)] px-4 py-0.5 text-[12px] text-[var(--color-faint)]"
	>
		<!-- Each link is at least 24 x 24 px (WCAG 2.2 2.5.8), and the
		     footer's padding gives back the height that adds. -->
		<span>
			Built by
			<a
				class="inline-flex min-h-6 min-w-6 items-center justify-center text-[var(--color-muted)] hover:text-[var(--color-bright)] transition-colors"
				href="https://github.com/jkmesches"
				target="_blank"
				rel="noopener"
			>Joseph Mesches</a>
		</span>
		<span class="flex flex-wrap items-center gap-x-3 gap-y-0.5">
			{#if version.value}
				<span class="num text-[var(--color-faint)]">v{version.value}</span>
				<span class="text-[var(--color-faint)]">·</span>
			{/if}
			<a
				class="inline-flex min-h-6 min-w-6 items-center justify-center text-[var(--color-muted)] hover:text-[var(--color-bright)] transition-colors"
				href="/faq"
			>FAQ</a>
			<span class="text-[var(--color-faint)]">·</span>
			<a
				class="inline-flex min-h-6 min-w-6 items-center justify-center text-[var(--color-muted)] hover:text-[var(--color-bright)] transition-colors"
				href="https://jkmesches.github.io/AQPI-Sentinel/"
				target="_blank"
				rel="noopener"
			>documentation</a>
			<span class="text-[var(--color-faint)]">·</span>
			<a
				class="inline-flex min-h-6 min-w-6 items-center justify-center text-[var(--color-muted)] hover:text-[var(--color-bright)] transition-colors"
				href="/api/docs"
				target="_blank"
				rel="noopener"
			>API</a>
			<span class="text-[var(--color-faint)]">·</span>
			<a
				class="inline-flex min-h-6 min-w-6 items-center justify-center text-[var(--color-muted)] hover:text-[var(--color-bright)] transition-colors"
				href="https://radarca.engr.colostate.edu"
				target="_blank"
				rel="noopener"
			>radarca · CSU</a>
		</span>
	</footer>
</div>
{/if}
