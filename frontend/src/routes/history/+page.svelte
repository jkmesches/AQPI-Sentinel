<script lang="ts">
	import { onMount } from 'svelte';
	import StatusBadge from '$lib/components/StatusBadge.svelte';
	import MultiSelectChips from '$lib/components/MultiSelectChips.svelte';
	import HistoryDetailModal from '$lib/components/HistoryDetailModal.svelte';
	import { severityChip, stageLabel } from '$lib/format';
	import { sentinel } from '$lib/stores/state.svelte';
	import { url as apiUrl } from '$lib/origin';
	import { page } from '$app/state';

	type Tab = 'alarms' | 'checks';

	const now = new Date();
	const aWeekAgo = new Date(now.getTime() - 7 * 86400_000);

	let tab = $state<Tab>('alarms');
	let since = $state(aWeekAgo.toISOString().slice(0, 16)); // datetime-local format
	let until = $state(now.toISOString().slice(0, 16));
	let stages = $state<string[]>([]);
	let targets = $state<string[]>([]);
	let checkId = $state('');           // optional deeplink narrowing
	let severities = $state<string[]>([]);
	let statuses = $state<string[]>([]);
	let totalMatching = $state(0);
	let truncated = $state(false);
	let rows = $state<any[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);

	// Detail modal state — populated when the user clicks a row.
	let detailOpen = $state(false);
	let detailRow  = $state<any>(null);
	function openDetail(r: any) {
		detailRow = r;
		detailOpen = true;
	}

	// Stage options mirror the canonical descriptor map. Labels stay sortable
	// alphabetically; hints surface the technical code so power users can
	// still recognize "L4" without expanding the chip.
	const STAGE_OPTIONS = [
		{ value: 'L0',      label: 'Connectivity',     hint: 'L0' },
		{ value: 'L1',      label: 'Product Freshness', hint: 'L1' },
		{ value: 'L2',      label: 'Radar Scans',      hint: 'L2' },
		{ value: 'L3',      label: 'Map Overlays',     hint: 'L3' },
		{ value: 'L4-T1T2', label: 'Image Quality',    hint: 'L4' }
	];
	const SEVERITY_OPTIONS = [
		{ value: 'info',     label: 'Info' },
		{ value: 'warn',     label: 'Warn' },
		{ value: 'critical', label: 'Critical' }
	];
	const STATUS_OPTIONS = [
		{ value: 'pass',  label: 'Pass' },
		{ value: 'warn',  label: 'Warn' },
		{ value: 'fail',  label: 'Fail' },
		{ value: 'error', label: 'Error' },
		{ value: 'skip',  label: 'Skip' }
	];

	// Targets pulled from current rollup so the picker knows about every
	// radar + product currently in the registry. The "add custom…" option
	// in MultiSelectChips lets users filter by older targets too.
	const targetOptions = $derived.by(() => {
		const seen = new Set<string>();
		const out: { value: string; label: string; hint?: string }[] = [];
		for (const [stageId, list] of Object.entries(sentinel.rollup?.stages ?? {})) {
			for (const r of list as any[]) {
				if (!r.target || seen.has(r.target)) continue;
				seen.add(r.target);
				out.push({ value: r.target, label: r.target, hint: stageLabel(stageId) });
			}
		}
		out.sort((a, b) => a.label.localeCompare(b.label));
		return out;
	});

	function localToIso(s: string): string {
		// datetime-local has no zone; treat as UTC for query
		return new Date(s + 'Z').toISOString();
	}

	function buildParams(): URLSearchParams {
		const params = new URLSearchParams({
			since: localToIso(since),
			until: localToIso(until)
		});
		if (stages.length)  params.set('stage',    stages.join(','));
		if (targets.length) params.set('target',   targets.join(','));
		if (checkId)        params.set('check_id', checkId);
		if (tab === 'alarms' && severities.length) params.set('severity', severities.join(','));
		if (tab === 'checks' && statuses.length)   params.set('status',   statuses.join(','));
		return params;
	}

	async function load() {
		loading = true;
		error = null;
		try {
			const params = buildParams();
			params.set('limit', '500');
			const r = await fetch(`/api/history/${tab}?${params}`);
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			rows = await r.json();
			const tot = r.headers.get('X-Total-Matching');
			totalMatching = tot ? Number(tot) : rows.length;
			truncated = r.headers.get('X-Truncated') === '1';
		} catch (e) {
			error = (e as Error).message;
			rows = [];
		} finally {
			loading = false;
		}
	}

	function exportCsv() {
		const params = buildParams();
		params.set('type', tab);
		window.open(apiUrl(`/api/history/export.csv?${params}`), '_blank');
	}

	function clearCheckId() {
		checkId = '';
		load();
	}

	// Deeplink prefill from query params. Timeline → "Open in History" passes
	// stage/target/check_id and an optional since/until window narrowed to the
	// clicked cell, so the user lands on rows relevant to what they clicked.
	function applyDeeplink() {
		const q = page.url.searchParams;
		const ds = q.get('stage');
		if (ds) stages = ds.split(',').filter(Boolean);
		const dt = q.get('target');
		if (dt) targets = dt.split(',').filter(Boolean);
		const dc = q.get('check_id');
		if (dc) checkId = dc;
		const dsince = q.get('since');
		if (dsince) {
			try { since = new Date(dsince).toISOString().slice(0, 16); } catch { /* */ }
		}
		const duntil = q.get('until');
		if (duntil) {
			try { until = new Date(duntil).toISOString().slice(0, 16); } catch { /* */ }
		}
		const dtab = q.get('tab');
		if (dtab === 'alarms' || dtab === 'checks') tab = dtab;
		// The daily report links here to evidence a claim about non-passing
		// runs. Without this the filter was dropped and the reader landed on
		// 500 rows of mostly-passing history with the handful that mattered
		// somewhere off the bottom — which reads as "the report was wrong".
		const dstatus = q.get('status');
		if (dstatus) statuses = dstatus.split(',').filter(Boolean);
		const dsev = q.get('severity');
		if (dsev) severities = dsev.split(',').filter(Boolean);
	}

	onMount(() => {
		applyDeeplink();
		load();
	});
</script>

<div class="flex h-full flex-col">
	<!-- TIME SCRUBBER + FILTERS -->
	<header class="flex flex-wrap items-center gap-3 border-b border-[var(--color-border)] px-4 py-2 text-[11px]">
		<div class="flex items-center gap-1">
			<span class="text-[var(--color-muted)] uppercase tracking-wider">since (UTC)</span>
			<input
				type="datetime-local"
				bind:value={since}
				class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11px] text-[var(--color-default)]"
			/>
		</div>
		<div class="flex items-center gap-1">
			<span class="text-[var(--color-muted)] uppercase tracking-wider">until (UTC)</span>
			<input
				type="datetime-local"
				bind:value={until}
				class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11px] text-[var(--color-default)]"
			/>
		</div>

		<MultiSelectChips
			label="stage"
			options={STAGE_OPTIONS}
			bind:selected={stages}
		/>
		<MultiSelectChips
			label="target"
			options={targetOptions}
			bind:selected={targets}
			allowCustom={true}
			placeholder="XSCV / custom…"
		/>

		{#if checkId}
			<span class="inline-flex items-center gap-1 rounded-sm border border-[var(--color-info)]/60 bg-[var(--color-info)]/10 px-1.5 py-0.5 text-[10px] num text-[var(--color-info)]">
				check_id: {checkId}
				<button
					type="button"
					class="text-[var(--color-info)] hover:text-[var(--color-fail)]"
					aria-label="clear check_id filter"
					onclick={clearCheckId}
				>
					×
				</button>
			</span>
		{/if}

		{#if tab === 'alarms'}
			<MultiSelectChips
				label="severity"
				options={SEVERITY_OPTIONS}
				bind:selected={severities}
				width="w-32"
			/>
		{:else}
			<MultiSelectChips
				label="status"
				options={STATUS_OPTIONS}
				bind:selected={statuses}
				width="w-32"
			/>
		{/if}

		<button
			class="border border-[var(--color-border-strong)] px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-elevated)]"
			onclick={load}
		>
			refresh
		</button>
		<button
			class="border border-[var(--color-border-strong)] px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]"
			onclick={exportCsv}
		>
			⤓ CSV
		</button>

		<span class="ml-auto text-[var(--color-muted)]">
			{#if loading}…{:else}{rows.length} rows{/if}
			{#if error}<span class="text-[var(--color-fail)]">  {error}</span>{/if}
		</span>
	</header>

	<!--
		A capped list must say so. Newest-first means the cut falls at the START
		of the window, so a 24 h filter can render 500 passing rows and hide
		every failure — which is exactly how the daily report's evidence links
		came to contradict the report.
	-->
	{#if truncated}
		<div
			class="border-b border-[var(--color-border)] bg-[var(--color-warn-bg,transparent)] px-4 py-1.5 text-[11px] text-[var(--color-warn)]"
		>
			Showing the newest {rows.length.toLocaleString()} of
			{totalMatching.toLocaleString()} matching runs — the older
			{(totalMatching - rows.length).toLocaleString()} are not on this page.
			Narrow the window, or filter by status, to see them.
		</div>
	{/if}

	<!-- TABS -->
	<nav class="flex items-center gap-3 border-b border-[var(--color-border)] px-4 py-1 text-[11px] uppercase tracking-wider">
		<button
			class={tab === 'alarms'
				? 'text-[var(--color-bright)] border-b border-[var(--color-bright)] -mb-px py-1'
				: 'text-[var(--color-muted)] hover:text-[var(--color-default)] py-1'}
			onclick={() => {
				tab = 'alarms';
				load();
			}}
		>
			Alarms
		</button>
		<button
			class={tab === 'checks'
				? 'text-[var(--color-bright)] border-b border-[var(--color-bright)] -mb-px py-1'
				: 'text-[var(--color-muted)] hover:text-[var(--color-default)] py-1'}
			onclick={() => {
				tab = 'checks';
				load();
			}}
		>
			Check Runs
		</button>
	</nav>

	<!-- TABLE -->
	<div class="flex-1 overflow-auto">
		{#if tab === 'alarms'}
			<table class="w-full text-[11px]">
				<thead class="sticky top-0 bg-[var(--color-canvas)] text-[var(--color-muted)]">
					<tr class="border-b border-[var(--color-border)]">
						<th class="px-3 py-1 text-left">severity</th>
						<th class="px-3 py-1 text-left">stage</th>
						<th class="px-3 py-1 text-left">id</th>
						<th class="px-3 py-1 text-left">check / target</th>
						<th class="px-3 py-1 text-left">opened</th>
						<th class="px-3 py-1 text-left">closed</th>
						<th class="px-3 py-1 text-left">message</th>
					</tr>
				</thead>
				<tbody>
					{#each rows as a}
						<tr
							class="border-b border-[var(--color-border)] hover:bg-[var(--color-elevated)]/40 cursor-pointer"
							onclick={() => openDetail(a)}
							title="Click for details"
						>
							<td class="px-3 py-1"><span class={severityChip(a.severity)}>{a.severity}</span></td>
							<td class="px-3 py-1 text-[var(--color-muted)]" title={a.stage}>{stageLabel(a.stage)}</td>
							<td class="px-3 py-1 num text-[var(--color-bright)]">#{a.id}</td>
							<td class="px-3 py-1 num"><span class="text-[var(--color-default)]">{a.target}</span><span class="text-[var(--color-muted)]">  ({a.check_id})</span></td>
							<td class="px-3 py-1 num text-[var(--color-muted)]">{a.opened_at?.slice(0, 19)}</td>
							<td class="px-3 py-1 num text-[var(--color-muted)]">{a.closed_at?.slice(0, 19) ?? '—'}</td>
							<td class="px-3 py-1 text-[var(--color-muted)] truncate max-w-md">{a.message}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{:else}
			<table class="w-full text-[11px]">
				<thead class="sticky top-0 bg-[var(--color-canvas)] text-[var(--color-muted)]">
					<tr class="border-b border-[var(--color-border)]">
						<th class="px-3 py-1 text-left">status</th>
						<th class="px-3 py-1 text-left">stage</th>
						<th class="px-3 py-1 text-left">target</th>
						<th class="px-3 py-1 text-left">check_id</th>
						<th class="px-3 py-1 text-left">finished</th>
						<th class="px-3 py-1 text-left">summary</th>
					</tr>
				</thead>
				<tbody>
					{#each rows as r}
						<tr
							class="border-b border-[var(--color-border)] hover:bg-[var(--color-elevated)]/40 cursor-pointer"
							onclick={() => openDetail(r)}
							title="Click for details"
						>
							<td class="px-3 py-1"><StatusBadge status={r.status} /></td>
							<td class="px-3 py-1 text-[var(--color-muted)]" title={r.stage}>{stageLabel(r.stage)}</td>
							<td class="px-3 py-1 num text-[var(--color-default)]">{r.target}</td>
							<td class="px-3 py-1 num text-[var(--color-muted)]">{r.check_id}</td>
							<td class="px-3 py-1 num text-[var(--color-muted)]">{r.finished_at?.slice(0, 19)}</td>
							<td class="px-3 py-1 text-[var(--color-muted)] truncate max-w-2xl">{r.summary}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</div>
</div>

<HistoryDetailModal bind:open={detailOpen} bind:row={detailRow} {tab} />
