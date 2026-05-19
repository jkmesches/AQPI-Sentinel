<script lang="ts">
	/** /m/history — mobile-native history view.
	 *
	 *  The desktop /history page is a wide multi-column table that does
	 *  not fit a 400px viewport. This route renders the same data as a
	 *  vertical card list with a collapsible filter sheet. Same backend
	 *  endpoints (/api/history/alarms, /api/history/checks). Same drill
	 *  -down pattern (MobileDrillDown).
	 *
	 *  Deeplink params from /m/uptime's drilldown ("Open in History") +
	 *  /m/timeline pre-fill the filters so a tap lands the user in
	 *  context. Without params, defaults to alarms tab, last 24h.
	 */
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { auth } from '$lib/stores/auth.svelte';
	import { url as apiUrl } from '$lib/origin';
	import {
		stageLabel, stageTechCode, severityChip, prettyCheckLabel, fmtAge
	} from '$lib/format';
	import MobileDrillDown from '$lib/components/mobile/MobileDrillDown.svelte';
	import LazyImage from '$lib/components/LazyImage.svelte';
	import { api, type CheckRun } from '$lib/api';
	import { sentinel } from '$lib/stores/state.svelte';

	// Captured-image URL helper. The /api/history/checks list endpoint
	// strips `payload` to keep the response small, so we always need a
	// tight follow-up fetch on drilldown open to find an L4 source.
	function capturedImage(run: any): string | null {
		const src = run?.payload?.source ?? run?.payload?.image_source;
		if (!src) return null;
		return apiUrl(`/api/upstream/image_by_source.png?source=${encodeURIComponent(src)}`);
	}

	type Tab = 'alarms' | 'checks';
	type AlarmRow = {
		id: number;
		check_id: string; target: string; stage: string;
		severity: string; opened_at: string; closed_at: string | null;
		message: string;
	};
	type CheckRow = {
		id: number;
		check_id: string; target: string; stage: string;
		status: string;
		started_at: string; finished_at: string;
		summary: string;
	};

	const STAGE_OPTIONS = [
		{ value: 'L0',      label: 'Connectivity' },
		{ value: 'L1',      label: 'Products' },
		{ value: 'L2',      label: 'Radar Scans' },
		{ value: 'L3',      label: 'Map Overlays' },
		{ value: 'L4-T1T2', label: 'Image Quality' }
	];
	const SEVERITY_OPTIONS = ['warn', 'critical'];
	const STATUS_OPTIONS   = ['pass', 'warn', 'fail', 'error', 'skip'];

	// 24h / 7d / 30d quick presets + custom range — most mobile sessions
	// only care about "what just happened" so a custom picker is hidden
	// behind a chevron toggle.
	type RangePreset = '24h' | '7d' | '30d' | 'custom';
	let rangePreset = $state<RangePreset>('24h');
	let customSince = $state<string>('');
	let customUntil = $state<string>('');

	let tab = $state<Tab>('alarms');
	let stages     = $state<string[]>([]);
	let targets    = $state<string[]>([]);
	let severities = $state<string[]>([]);
	let statuses   = $state<string[]>([]);
	let checkId    = $state<string>(''); // from deep link only
	let rows = $state<(AlarmRow | CheckRow)[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let filterOpen = $state(false);

	// Live-time tick for "x min ago" labels.
	let nowMs = $state(Date.now());
	let nowTimer: ReturnType<typeof setInterval>;

	// Apply deeplink on first load.
	function applyDeeplink() {
		const q = page.url.searchParams;
		if (!q) return;
		const dtab = q.get('tab');
		if (dtab === 'alarms' || dtab === 'checks') tab = dtab;
		const dstage = q.get('stage');
		if (dstage) stages = dstage.split(',').filter(Boolean);
		const dtarget = q.get('target');
		if (dtarget) targets = dtarget.split(',').filter(Boolean);
		const dcheck = q.get('check_id');
		if (dcheck) checkId = dcheck;
		const dsev = q.get('severity');
		if (dsev) severities = dsev.split(',').filter(Boolean);
		const dstat = q.get('status');
		if (dstat) statuses = dstat.split(',').filter(Boolean);
		const dsince = q.get('since');
		const duntil = q.get('until');
		if (dsince && duntil) {
			rangePreset = 'custom';
			try {
				customSince = new Date(dsince).toISOString().slice(0, 16);
				customUntil = new Date(duntil).toISOString().slice(0, 16);
			} catch { /* */ }
		}
	}

	function effectiveRange(): { since: string; until: string } {
		const now = new Date();
		if (rangePreset === 'custom' && customSince && customUntil) {
			return {
				since: new Date(customSince + 'Z').toISOString(),
				until: new Date(customUntil + 'Z').toISOString()
			};
		}
		const ms = rangePreset === '7d' ? 7 * 86400_000
				 : rangePreset === '30d' ? 30 * 86400_000
				 : 86400_000;
		return {
			since: new Date(now.getTime() - ms).toISOString(),
			until: now.toISOString()
		};
	}

	async function load() {
		loading = true;
		error = null;
		try {
			const { since, until } = effectiveRange();
			const qs = new URLSearchParams({ since, until, limit: '300' });
			if (stages.length)    qs.set('stage',    stages.join(','));
			if (targets.length)   qs.set('target',   targets.join(','));
			if (checkId)          qs.set('check_id', checkId);
			if (tab === 'alarms' && severities.length) qs.set('severity', severities.join(','));
			if (tab === 'checks' && statuses.length)   qs.set('status',   statuses.join(','));
			const path = tab === 'alarms' ? '/api/history/alarms' : '/api/history/checks';
			const r = await fetch(apiUrl(`${path}?${qs}`), {
				headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {}
			});
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			rows = await r.json();
		} catch (e) {
			error = (e as Error).message;
			rows = [];
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		applyDeeplink();
		load();
		nowTimer = setInterval(() => (nowMs = Date.now()), 30_000);
		return () => clearInterval(nowTimer);
	});

	function rowKeyTs(r: any): string {
		return tab === 'alarms' ? r.opened_at : r.finished_at;
	}
	function sinceText(ts: string): string {
		const s = (nowMs - Date.parse(ts)) / 1000;
		return s < 0 ? 'in future' : `${fmtAge(s)} ago`;
	}

	// All available targets in current rollup — same source the desktop
	// page draws on. Used to populate the target chip picker.
	const targetOptions = $derived.by(() => {
		const seen = new Set<string>();
		const out: string[] = [];
		for (const list of Object.values(sentinel.rollup?.stages ?? {})) {
			for (const r of list as any[]) {
				if (r.target && !seen.has(r.target)) {
					seen.add(r.target);
					out.push(r.target);
				}
			}
		}
		return out.sort();
	});

	const statusBg: Record<string, string> = {
		pass:  'var(--color-ok)',
		warn:  'var(--color-warn)',
		fail:  'var(--color-fail)',
		error: 'var(--color-fail)',
		skip:  'var(--color-faint)',
		critical: 'var(--color-fail)',
		info:  'var(--color-info)'
	};

	function groupByDay(items: any[]): { day: string; items: any[] }[] {
		const out: Record<string, any[]> = {};
		const order: string[] = [];
		for (const r of items) {
			const day = rowKeyTs(r).slice(0, 10);
			if (!(day in out)) { out[day] = []; order.push(day); }
			out[day].push(r);
		}
		return order.map((d) => ({ day: d, items: out[d] }));
	}
	const grouped = $derived(groupByDay(rows));

	function fmtDay(iso: string): string {
		const todayKey = new Date().toISOString().slice(0, 10);
		const yKey = new Date(Date.now() - 86400_000).toISOString().slice(0, 10);
		if (iso === todayKey) return 'Today';
		if (iso === yKey) return 'Yesterday';
		const d = new Date(iso + 'T00:00:00Z');
		return d.toLocaleDateString(undefined, {
			weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC'
		});
	}

	function toggle(list: string[], v: string): string[] {
		return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
	}
	function clearAll() {
		stages = []; targets = []; severities = []; statuses = []; checkId = '';
		rangePreset = '24h'; customSince = ''; customUntil = '';
	}
	const activeFilterCount = $derived(
		stages.length + targets.length +
		(tab === 'alarms' ? severities.length : statuses.length) +
		(checkId ? 1 : 0) +
		(rangePreset !== '24h' ? 1 : 0)
	);

	// Drilldown.
	let detailOpen = $state(false);
	let detailRow  = $state<any>(null);
	let detailImgRun = $state<CheckRun | null>(null);
	let detailToken = 0;
	async function openDetail(r: any) {
		const my = ++detailToken;
		detailRow = r;
		detailImgRun = null;
		detailOpen = true;
		// Image lookup window: alarm rows span an open→close interval; check
		// rows are a single moment. Pull check_runs in a small surrounding
		// window and take the first one with a payload source.
		try {
			let since: string, until: string;
			if (tab === 'alarms') {
				const open = Date.parse(r.opened_at);
				const close = r.closed_at ? Date.parse(r.closed_at) : open + 5 * 60_000;
				since = new Date(open - 60_000).toISOString();
				until = new Date(close + 60_000).toISOString();
			} else {
				const t = Date.parse(r.finished_at);
				since = new Date(t - 30_000).toISOString();
				until = new Date(t + 30_000).toISOString();
			}
			const runs = await api.historyRuns(r.check_id, r.target, since, until, 20);
			if (my !== detailToken) return;
			detailImgRun = runs.find((x) => capturedImage(x)) ?? null;
		} catch {
			/* swallow */
		}
	}
	function detailKind(): 'alarm' | 'check' {
		return tab === 'alarms' ? 'alarm' : 'check';
	}
</script>

<div class="flex h-full flex-col overflow-hidden">
	<!-- HEADER -->
	<header class="shrink-0 border-b border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2">
		<div class="flex items-center justify-between">
			<h1 class="text-[15px] font-semibold tracking-wide text-[var(--color-bright)]">History</h1>
			<button
				type="button"
				onclick={() => (filterOpen = true)}
				class="flex items-center gap-1.5 rounded-md border border-[var(--color-border-strong)] px-3 py-1.5 text-[11px] uppercase tracking-wider text-[var(--color-default)] active:bg-[var(--color-elevated)]/60"
				style="-webkit-tap-highlight-color: transparent;"
				aria-label="Open filters"
			>
				<svg viewBox="0 0 24 24" class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
					<line x1="3" y1="6"  x2="21" y2="6"  />
					<line x1="6" y1="12" x2="18" y2="12" />
					<line x1="9" y1="18" x2="15" y2="18" />
				</svg>
				Filters
				{#if activeFilterCount > 0}
					<span class="rounded-full bg-[var(--color-ok)]/20 px-1.5 text-[10px] text-[var(--color-bright)] num">{activeFilterCount}</span>
				{/if}
			</button>
		</div>
		<!-- Tab chip strip -->
		<div class="mt-2 flex gap-1">
			<button type="button"
				onclick={() => { tab = 'alarms'; load(); }}
				class="flex-1 rounded-md border px-2 py-1.5 text-[12px] uppercase tracking-wider {tab === 'alarms'
					? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
					: 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
				style="-webkit-tap-highlight-color: transparent; min-height: 36px;"
			>Alarms</button>
			<button type="button"
				onclick={() => { tab = 'checks'; load(); }}
				class="flex-1 rounded-md border px-2 py-1.5 text-[12px] uppercase tracking-wider {tab === 'checks'
					? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
					: 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
				style="-webkit-tap-highlight-color: transparent; min-height: 36px;"
			>Check Runs</button>
		</div>
		<div class="mt-1.5 flex items-center justify-between text-[10px] uppercase tracking-wider text-[var(--color-faint)] num">
			<span>{rangePreset === 'custom' ? 'custom range' : `last ${rangePreset}`} · UTC</span>
			<span>{loading ? '…' : `${rows.length} rows`}</span>
		</div>
	</header>

	<!-- BODY -->
	{#if error}
		<div class="m-3 rounded-sm border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-3 py-2 text-[12px] text-[var(--color-fail)]">{error}</div>
	{/if}

	{#if loading}
		<div class="px-4 py-6 text-[12px] text-[var(--color-muted)]">loading…</div>
	{:else if rows.length === 0}
		<div class="m-3 rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-8 text-center text-[12px] text-[var(--color-muted)]">
			{#if activeFilterCount > 0}
				No matches. Try widening the filters.
			{:else}
				Nothing yet for this range.
			{/if}
		</div>
	{:else}
		<div class="flex-1 overflow-auto px-3 pt-2">
			{#each grouped as g (g.day)}
				<div class="mt-3 mb-1 px-1 text-[10px] uppercase tracking-[0.16em] text-[var(--color-muted)]">{fmtDay(g.day)}</div>
				<ul class="overflow-hidden rounded-md border border-[var(--color-border)] bg-[var(--color-surface)]">
					{#each g.items as r (r.id)}
						<li class="border-b border-[var(--color-border)]/60 last:border-b-0">
							<button
								type="button"
								onclick={() => openDetail(r)}
								class="flex w-full items-start gap-2.5 px-3 py-2.5 text-left active:bg-[var(--color-elevated)]/60"
								style="-webkit-tap-highlight-color: transparent;"
							>
								<!-- Status pip -->
								<span
									class="mt-1 inline-block h-3 w-3 shrink-0 rounded-sm"
									style="background: {tab === 'alarms'
										? (statusBg[r.severity] ?? 'var(--color-muted)')
										: (statusBg[r.status] ?? 'var(--color-muted)')};"
								></span>
								<div class="min-w-0 flex-1">
									<div class="flex items-baseline justify-between gap-2">
										<span class="num truncate text-[13px] text-[var(--color-bright)]">
											{prettyCheckLabel(r.check_id, r.target)}
										</span>
										<span class="shrink-0 num text-[10.5px] text-[var(--color-faint)]">
											{rowKeyTs(r).slice(11, 16)}Z
										</span>
									</div>
									<div class="mt-0.5 flex flex-wrap items-center gap-1.5 text-[10.5px] uppercase tracking-wider">
										{#if tab === 'alarms'}
											<span class={severityChip(r.severity)}>{r.severity}</span>
											{#if r.closed_at}
												<span class="text-[var(--color-ok)]">cleared</span>
											{:else}
												<span class="text-[var(--color-warn)]">open</span>
											{/if}
										{:else}
											<span class="num" style="color: {statusBg[r.status]};">{r.status}</span>
										{/if}
										<span class="text-[var(--color-muted)] num" title={r.stage}>{stageLabel(r.stage)}</span>
										{#if r.target}<span class="text-[var(--color-faint)] num">{r.target}</span>{/if}
									</div>
									{#if (tab === 'alarms' ? r.message : r.summary)}
										<div class="mt-1 line-clamp-2 text-[12px] num text-[var(--color-default)]">
											{tab === 'alarms' ? r.message : r.summary}
										</div>
									{/if}
									<div class="mt-0.5 text-[10.5px] text-[var(--color-faint)] num">{sinceText(rowKeyTs(r))}</div>
								</div>
							</button>
						</li>
					{/each}
				</ul>
			{/each}
			<!-- Bottom-nav clearance -->
			<div style="height: calc(72px + env(safe-area-inset-bottom, 0px));"></div>
		</div>
	{/if}
</div>

<!-- FILTER BOTTOM SHEET -->
{#if filterOpen}
	<div
		class="fixed inset-0 z-40 flex flex-col bg-black/50"
		role="dialog"
		aria-modal="true"
		onclick={(e) => { if (e.target === e.currentTarget) filterOpen = false; }}
		onkeydown={(e) => { if (e.key === 'Escape') filterOpen = false; }}
	>
		<div class="mt-auto rounded-t-xl border-t border-[var(--color-border)] bg-[var(--color-canvas)]" style="padding-bottom: calc(env(safe-area-inset-bottom, 0px) + 16px);">
			<div class="flex items-center justify-between border-b border-[var(--color-border)] px-4 py-2.5">
				<h2 class="text-[14px] font-semibold tracking-wide text-[var(--color-bright)]">Filters</h2>
				<button type="button" onclick={() => (filterOpen = false)} class="text-[12px] uppercase tracking-wider text-[var(--color-muted)] active:text-[var(--color-bright)]">Close</button>
			</div>

			<div class="max-h-[68vh] overflow-auto px-4 py-3 space-y-4">
				<!-- Time range -->
				<section>
					<div class="mb-1.5 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Time range (UTC)</div>
					<div class="grid grid-cols-4 gap-1">
						{#each ['24h','7d','30d','custom'] as p}
							<button type="button"
								onclick={() => (rangePreset = p as RangePreset)}
								class="rounded-md border px-1 py-1.5 text-[12px] num uppercase tracking-wider {rangePreset === p
									? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
									: 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
								style="-webkit-tap-highlight-color: transparent; min-height: 36px;"
							>{p}</button>
						{/each}
					</div>
					{#if rangePreset === 'custom'}
						<div class="mt-2 flex flex-col gap-2 text-[11px]">
							<label class="flex items-center justify-between gap-2">
								<span class="text-[var(--color-muted)] uppercase tracking-wider">since (UTC)</span>
								<input type="datetime-local" bind:value={customSince}
									class="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1.5 text-[16px] num text-[var(--color-bright)]" />
							</label>
							<label class="flex items-center justify-between gap-2">
								<span class="text-[var(--color-muted)] uppercase tracking-wider">until (UTC)</span>
								<input type="datetime-local" bind:value={customUntil}
									class="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1.5 text-[16px] num text-[var(--color-bright)]" />
							</label>
						</div>
					{/if}
				</section>

				<!-- Stage -->
				<section>
					<div class="mb-1.5 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Stage</div>
					<div class="flex flex-wrap gap-1.5">
						{#each STAGE_OPTIONS as opt}
							<button type="button"
								onclick={() => (stages = toggle(stages, opt.value))}
								class="rounded-md border px-2 py-1 text-[11px] {stages.includes(opt.value)
									? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
									: 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
								style="-webkit-tap-highlight-color: transparent;"
								title={opt.value}
							>{opt.label}</button>
						{/each}
					</div>
				</section>

				<!-- Severity (alarms) / Status (checks) -->
				{#if tab === 'alarms'}
					<section>
						<div class="mb-1.5 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Severity</div>
						<div class="flex flex-wrap gap-1.5">
							{#each SEVERITY_OPTIONS as v}
								<button type="button"
									onclick={() => (severities = toggle(severities, v))}
									class="rounded-md border px-2.5 py-1 text-[11px] num uppercase tracking-wider {severities.includes(v)
										? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
										: 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
									style="-webkit-tap-highlight-color: transparent;"
								>{v}</button>
							{/each}
						</div>
					</section>
				{:else}
					<section>
						<div class="mb-1.5 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Status</div>
						<div class="flex flex-wrap gap-1.5">
							{#each STATUS_OPTIONS as v}
								<button type="button"
									onclick={() => (statuses = toggle(statuses, v))}
									class="rounded-md border px-2.5 py-1 text-[11px] num uppercase tracking-wider {statuses.includes(v)
										? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
										: 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
									style="-webkit-tap-highlight-color: transparent;"
								>{v}</button>
							{/each}
						</div>
					</section>
				{/if}

				<!-- Target -->
				{#if targetOptions.length > 0}
					<section>
						<div class="mb-1.5 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Target</div>
						<div class="flex flex-wrap gap-1.5 max-h-[120px] overflow-auto">
							{#each targetOptions as t}
								<button type="button"
									onclick={() => (targets = toggle(targets, t))}
									class="rounded-md border px-2 py-0.5 text-[11px] num {targets.includes(t)
										? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
										: 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
									style="-webkit-tap-highlight-color: transparent;"
								>{t}</button>
							{/each}
						</div>
					</section>
				{/if}

				{#if checkId}
					<section>
						<div class="mb-1.5 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Check ID (from deeplink)</div>
						<div class="flex items-center gap-2">
							<span class="num text-[11px] text-[var(--color-bright)] truncate flex-1">{checkId}</span>
							<button type="button" onclick={() => (checkId = '')}
								class="text-[10.5px] uppercase tracking-wider text-[var(--color-muted)] active:text-[var(--color-fail)]">
								× clear
							</button>
						</div>
					</section>
				{/if}
			</div>

			<div class="grid grid-cols-2 gap-2 border-t border-[var(--color-border)] px-4 pt-3" style="padding-top: 12px;">
				<button type="button" onclick={clearAll}
					class="rounded-md border border-[var(--color-border-strong)] px-3 py-2.5 text-[12px] uppercase tracking-wider text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60"
					style="-webkit-tap-highlight-color: transparent;"
				>Reset</button>
				<button type="button" onclick={() => { filterOpen = false; load(); }}
					class="rounded-md border border-[var(--color-ok)] bg-[var(--color-ok)]/15 px-3 py-2.5 text-[12px] uppercase tracking-wider text-[var(--color-bright)] active:bg-[var(--color-ok)]/25"
					style="-webkit-tap-highlight-color: transparent;"
				>Apply</button>
			</div>
		</div>
	</div>
{/if}

<MobileDrillDown
	bind:open={detailOpen}
	title={detailRow ? prettyCheckLabel(detailRow.check_id, detailRow.target) : ''}
	subtitle={detailRow?.check_id ?? ''}
	stage={detailRow?.stage ?? ''}
	status={detailRow ? (tab === 'alarms' ? (detailRow.closed_at ? 'pass' : detailRow.severity) : detailRow.status) : ''}
>
	{#if detailRow}
		<dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1.5 text-[12px] num">
			<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Target</dt>
			<dd class="text-[var(--color-default)]">{detailRow.target || '—'}</dd>
			<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Stage</dt>
			<dd class="text-[var(--color-default)]">{stageLabel(detailRow.stage)} <span class="text-[var(--color-faint)]">({stageTechCode(detailRow.stage)})</span></dd>
			{#if detailKind() === 'alarm'}
				<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Severity</dt>
				<dd class="text-[var(--color-bright)]">{detailRow.severity}</dd>
				<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Opened</dt>
				<dd class="text-[var(--color-default)]">{detailRow.opened_at.slice(0, 19).replace('T',' ')} UTC</dd>
				<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Closed</dt>
				<dd class="text-[var(--color-default)]">{detailRow.closed_at ? detailRow.closed_at.slice(0,19).replace('T',' ') + ' UTC' : '— (still open)'}</dd>
			{:else}
				<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Status</dt>
				<dd class="text-[var(--color-bright)] uppercase">{detailRow.status}</dd>
				<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Started</dt>
				<dd class="text-[var(--color-default)]">{detailRow.started_at.slice(0,19).replace('T',' ')} UTC</dd>
				<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Finished</dt>
				<dd class="text-[var(--color-default)]">{detailRow.finished_at.slice(0,19).replace('T',' ')} UTC</dd>
			{/if}
		</dl>
		{#if (detailKind() === 'alarm' ? detailRow.message : detailRow.summary)}
			<section class="mt-4">
				<div class="mb-1 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">{detailKind() === 'alarm' ? 'Message' : 'Summary'}</div>
				<pre class="whitespace-pre-wrap rounded-sm border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-[12px] num text-[var(--color-default)] leading-snug">{detailKind() === 'alarm' ? detailRow.message : detailRow.summary}</pre>
			</section>
		{/if}

		<!-- Captured image (L4 image-quality only — every other check type
		     leaves detailImgRun null and this section drops out). -->
		{#if detailImgRun && capturedImage(detailImgRun)}
			<section class="mt-4">
				<div class="mb-1 flex items-baseline justify-between text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
					<span>Captured image</span>
					<span class="num text-[var(--color-faint)] normal-case">{detailImgRun.finished_at.slice(11,19)}Z</span>
				</div>
				<div class="overflow-hidden rounded-sm border border-[var(--color-border)] bg-black">
					<LazyImage src={capturedImage(detailImgRun) ?? ''} alt="captured radar scan" minHeight={200} />
				</div>
			</section>
		{/if}

		<!-- Drilldown nav from a History row: cross-jump to the two other
		     views the user might want next. We're already in History, so
		     omit a "Show in History" self-link. -->
		<div class="mt-5 grid grid-cols-2 gap-2">
			<button type="button"
				onclick={async () => {
					const qs = new URLSearchParams({
						check_id: detailRow.check_id,
						target:   detailRow.target ?? '',
						stage:    detailRow.stage ?? ''
					});
					detailOpen = false;
					await goto(`/m/timeline?${qs}`);
				}}
				class="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-elevated)]/30 px-3 py-2.5 text-center text-[11.5px] uppercase tracking-wider text-[var(--color-bright)] active:bg-[var(--color-elevated)]/60"
				style="-webkit-tap-highlight-color: transparent;"
			>Show in Timeline</button>
			<button type="button"
				onclick={async () => {
					const qs = new URLSearchParams({
						check_id: detailRow.check_id,
						target:   detailRow.target ?? '',
						stage:    detailRow.stage ?? ''
					});
					detailOpen = false;
					await goto(`/m/uptime?${qs}`);
				}}
				class="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-elevated)]/30 px-3 py-2.5 text-center text-[11.5px] uppercase tracking-wider text-[var(--color-bright)] active:bg-[var(--color-elevated)]/60"
				style="-webkit-tap-highlight-color: transparent;"
			>Show in Uptime</button>
		</div>
	{/if}
</MobileDrillDown>
