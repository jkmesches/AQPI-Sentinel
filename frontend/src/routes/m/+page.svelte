<script lang="ts">
	import { sentinel } from '$lib/stores/state.svelte';
	import { fmtAge, stageLabel, prettyCheckLabel } from '$lib/format';
	import { productCategory, PRODUCT_CATEGORY_LABEL, PRODUCT_CATEGORY_ORDER } from '$lib/format';
	import { goto } from '$app/navigation';
	import StatusDot from '$lib/components/StatusDot.svelte';
	import MobileStatusMap from '$lib/components/mobile/MobileStatusMap.svelte';
	import MobileDrillDown from '$lib/components/mobile/MobileDrillDown.svelte';

	let now = $state(Date.now());
	let tickTimer: ReturnType<typeof setInterval>;

	import { onMount, onDestroy } from 'svelte';
	onMount(() => { tickTimer = setInterval(() => (now = Date.now()), 5000); });
	onDestroy(() => { if (tickTimer) clearInterval(tickTimer); });

	const counts = $derived(sentinel.rollup?.counts ?? {});
	const totalPass = $derived(Object.values(counts).reduce((a, b) => a + b.pass, 0));
	const totalWarn = $derived(Object.values(counts).reduce((a, b) => a + b.warn, 0));
	const totalFail = $derived(Object.values(counts).reduce((a, b) => a + (b.fail + b.error), 0));
	const total = $derived(Object.values(counts).reduce((a, b) => a + b.total, 0));
	const allClean = $derived(total > 0 && totalWarn === 0 && totalFail === 0);

	const sinceUpdate = $derived(
		sentinel.lastUpdate ? (now - sentinel.lastUpdate.getTime()) / 1000 : null
	);

	const stages = $derived(Object.entries(sentinel.rollup?.stages ?? {}));

	// Per-stage expand state. Default: only show non-pass items; user can tap to expand.
	let expanded = $state<Record<string, boolean>>({});
	function toggleStage(s: string) { expanded[s] = !expanded[s]; }

	function rowsToShow(rows: any[], stageKey: string): any[] {
		return expanded[stageKey] ? rows : rows.filter((r) => r.status !== 'pass' && r.status !== 'skip');
	}

	// Tap-to-drill-down: opens the full-screen mobile detail sheet.
	let detailOpen = $state(false);
	let detailRow = $state<any>(null);
	function openDetail(r: any) {
		detailRow = r;
		detailOpen = true;
	}
</script>

<!-- Hero status -->
<section class="mb-5 px-1 pt-2 text-center">
	{#if sentinel.loading && !sentinel.rollup}
		<div class="text-[14px] text-[var(--color-muted)]">loading…</div>
	{:else if sentinel.error && !sentinel.rollup}
		<div class="text-[14px] text-[var(--color-fail)]">{sentinel.error}</div>
	{:else}
		<div class="num text-[56px] leading-none {allClean ? 'text-[var(--color-ok)]' : totalFail > 0 ? 'text-[var(--color-fail)]' : 'text-[var(--color-warn)]'}">
			{totalPass}<span class="text-[var(--color-faint)]">/{total}</span>
		</div>
		<div class="mt-2 text-[12px] uppercase tracking-[0.16em] text-[var(--color-muted)]">
			checks passing
		</div>

		<div class="mt-4 flex items-center justify-center gap-3 text-[13px] num">
			{#if totalWarn}
				<span class="rounded-full border border-[var(--color-warn)]/40 bg-[var(--color-warn)]/10 px-2.5 py-0.5 text-[var(--color-warn)]">
					{totalWarn} warn
				</span>
			{/if}
			{#if totalFail}
				<span class="rounded-full border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-2.5 py-0.5 text-[var(--color-fail)]">
					{totalFail} fail
				</span>
			{/if}
			{#if allClean}
				<span class="text-[var(--color-faint)]">all clear</span>
			{/if}
		</div>

		<div class="mt-3 text-[11px] text-[var(--color-faint)] num">
			{#if sinceUpdate !== null}
				updated {fmtAge(sinceUpdate)} ago
			{:else}
				—
			{/if}
		</div>
	{/if}
</section>

<!-- Geographic context: tiny radar map (lazy-loaded MapLibre chunk) -->
<section class="mb-4">
	<MobileStatusMap />
</section>

<!-- Per-stage cards -->
{#each stages as [stage, rows] (stage)}
	{@const c = counts[stage] ?? { pass: 0, warn: 0, fail: 0, error: 0, total: 0 }}
	{@const stageStatus = c.fail || c.error ? 'fail' : c.warn ? 'warn' : 'pass'}
	{@const visible = rowsToShow(rows, stage)}
	<section class="mb-3 overflow-hidden rounded-md border border-[var(--color-border)] bg-[var(--color-surface)]">
		<button
			type="button"
			onclick={() => toggleStage(stage)}
			class="flex w-full items-center gap-3 px-3 py-3 text-left"
			style="-webkit-tap-highlight-color: transparent;"
		>
			<StatusDot status={stageStatus} size={9} />
			<span class="text-[14px] font-semibold tracking-[0.12em] text-[var(--color-bright)]" title={stage}>{stageLabel(stage)}</span>
			<span class="num text-[12px] text-[var(--color-muted)]">{c.pass}/{c.total}</span>
			{#if c.warn}<span class="num text-[12px] text-[var(--color-warn)]">{c.warn} W</span>{/if}
			{#if c.fail + c.error}<span class="num text-[12px] text-[var(--color-fail)]">{c.fail + c.error} F</span>{/if}
			<span class="ml-auto text-[12px] text-[var(--color-faint)]">
				{expanded[stage] ? '−' : '+'}
			</span>
		</button>

		{#if visible.length > 0}
			<ul class="divide-y divide-[var(--color-border)] border-t border-[var(--color-border)]">
				{#each visible as r (r.check_id + '|' + r.target)}
					<li>
						<button
							type="button"
							onclick={() => openDetail(r)}
							class="flex w-full items-start gap-3 px-3 py-2.5 text-left text-[13px] active:bg-[var(--color-elevated)]/60"
							style="-webkit-tap-highlight-color: transparent;"
						>
							<StatusDot status={r.status} size={7} />
							<div class="min-w-0 flex-1">
								<div class="num truncate text-[13px] text-[var(--color-default)]" title={`${r.check_id} · ${r.target}`}>
									{prettyCheckLabel(r.check_id, r.target)}
								</div>
								{#if r.summary}
									<div class="num mt-0.5 truncate text-[11px] text-[var(--color-muted)]">
										{r.summary}
									</div>
								{/if}
							</div>
							<svg viewBox="0 0 24 24" width="14" height="14" class="mt-1 shrink-0 text-[var(--color-faint)]" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
								<polyline points="9 6 15 12 9 18" />
							</svg>
						</button>
					</li>
				{/each}
			</ul>
			{#if !expanded[stage] && c.pass > 0}
				<button
					type="button"
					onclick={() => toggleStage(stage)}
					class="block w-full px-3 py-2 text-center text-[11px] uppercase tracking-wider text-[var(--color-muted)]"
				>
					show {c.pass} passing
				</button>
			{/if}
		{:else if expanded[stage]}
			<div class="px-3 py-3 text-center text-[12px] italic text-[var(--color-faint)]">no checks</div>
		{/if}
	</section>
{/each}

<MobileDrillDown
	bind:open={detailOpen}
	title={detailRow ? prettyCheckLabel(detailRow.check_id ?? '', detailRow.target ?? '') : ''}
	subtitle={detailRow?.check_id ?? ''}
	stage={detailRow?.stage ?? ''}
	status={detailRow?.status ?? ''}
>
	{#if detailRow}
		<dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1.5 text-[12px] num">
			<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Target</dt>
			<dd class="text-[var(--color-default)]">{detailRow.target || '—'}</dd>
			<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Status</dt>
			<dd class="text-[var(--color-bright)]">{detailRow.status}</dd>
		</dl>

		{#if detailRow.summary}
			<section class="mt-4">
				<div class="mb-1 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Summary</div>
				<pre class="whitespace-pre-wrap rounded-sm border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-[12px] num text-[var(--color-default)] leading-snug">{detailRow.summary}</pre>
			</section>
		{/if}

		<div class="mt-5 flex flex-col gap-2">
			<button
				type="button"
				onclick={async () => {
					const url = `/m/timeline?check_id=${encodeURIComponent(detailRow.check_id ?? '')}` +
						`&target=${encodeURIComponent(detailRow.target ?? '')}` +
						`&stage=${encodeURIComponent(detailRow.stage ?? '')}`;
					detailOpen = false;
					await goto(url);
				}}
				class="w-full rounded-md border border-[var(--color-border-strong)] bg-[var(--color-elevated)]/30 px-3 py-2.5 text-center text-[12px] uppercase tracking-wider text-[var(--color-bright)] active:bg-[var(--color-elevated)]/60"
				style="-webkit-tap-highlight-color: transparent;"
			>
				Open in History
			</button>
		</div>
	{/if}
</MobileDrillDown>
