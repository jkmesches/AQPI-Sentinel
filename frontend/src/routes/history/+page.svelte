<script lang="ts">
	import { onMount } from 'svelte';
	import StatusBadge from '$lib/components/StatusBadge.svelte';
	import { severityChip } from '$lib/format';
	import { url as apiUrl } from '$lib/origin';

	type Tab = 'alarms' | 'checks';

	const now = new Date();
	const aWeekAgo = new Date(now.getTime() - 7 * 86400_000);

	let tab = $state<Tab>('alarms');
	let since = $state(aWeekAgo.toISOString().slice(0, 16)); // datetime-local format
	let until = $state(now.toISOString().slice(0, 16));
	let stage = $state('');
	let target = $state('');
	let severity = $state('');
	let status = $state('');
	let rows = $state<any[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);

	function localToIso(s: string): string {
		// datetime-local has no zone; treat as UTC for query
		return new Date(s + 'Z').toISOString();
	}

	async function load() {
		loading = true;
		error = null;
		try {
			const params = new URLSearchParams({
				since: localToIso(since),
				until: localToIso(until),
				limit: '500'
			});
			if (stage) params.set('stage', stage);
			if (target) params.set('target', target);
			if (tab === 'alarms' && severity) params.set('severity', severity);
			if (tab === 'checks' && status) params.set('status', status);
			const r = await fetch(`/api/history/${tab}?${params}`);
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			rows = await r.json();
		} catch (e) {
			error = (e as Error).message;
			rows = [];
		} finally {
			loading = false;
		}
	}

	function exportCsv() {
		const params = new URLSearchParams({
			type: tab,
			since: localToIso(since),
			until: localToIso(until)
		});
		if (stage) params.set('stage', stage);
		if (target) params.set('target', target);
		window.open(apiUrl(`/api/history/export.csv?${params}`), '_blank');
	}

	onMount(load);
</script>

<div class="flex h-full flex-col">
	<!-- TIME SCRUBBER + FILTERS -->
	<header class="flex flex-wrap items-center gap-3 border-b border-[var(--color-border)] px-4 py-2 text-[11px]">
		<div class="flex items-center gap-1">
			<span class="text-[var(--color-muted)] uppercase tracking-wider">since</span>
			<input
				type="datetime-local"
				bind:value={since}
				class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11px] text-[var(--color-default)]"
			/>
		</div>
		<div class="flex items-center gap-1">
			<span class="text-[var(--color-muted)] uppercase tracking-wider">until</span>
			<input
				type="datetime-local"
				bind:value={until}
				class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11px] text-[var(--color-default)]"
			/>
		</div>

		<div class="flex items-center gap-1">
			<span class="text-[var(--color-muted)] uppercase tracking-wider">stage</span>
			<input
				type="text"
				placeholder="L0|L1|L2|L3|L4-T1T2"
				bind:value={stage}
				class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11px] text-[var(--color-default)] w-28"
			/>
		</div>

		<div class="flex items-center gap-1">
			<span class="text-[var(--color-muted)] uppercase tracking-wider">target</span>
			<input
				type="text"
				placeholder="XSCV"
				bind:value={target}
				class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11px] text-[var(--color-default)] w-28"
			/>
		</div>

		{#if tab === 'alarms'}
			<div class="flex items-center gap-1">
				<span class="text-[var(--color-muted)] uppercase tracking-wider">severity</span>
				<select
					bind:value={severity}
					class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11px] text-[var(--color-default)]"
				>
					<option value="">any</option>
					<option value="info">info</option>
					<option value="warn">warn</option>
					<option value="critical">critical</option>
				</select>
			</div>
		{:else}
			<div class="flex items-center gap-1">
				<span class="text-[var(--color-muted)] uppercase tracking-wider">status</span>
				<select
					bind:value={status}
					class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11px] text-[var(--color-default)]"
				>
					<option value="">any</option>
					<option value="pass">pass</option>
					<option value="warn">warn</option>
					<option value="fail">fail</option>
					<option value="error">error</option>
					<option value="skip">skip</option>
				</select>
			</div>
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
						<tr class="border-b border-[var(--color-border)] hover:bg-[var(--color-elevated)]/40">
							<td class="px-3 py-1"><span class={severityChip(a.severity)}>{a.severity}</span></td>
							<td class="px-3 py-1 text-[var(--color-muted)]">{a.stage}</td>
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
						<tr class="border-b border-[var(--color-border)] hover:bg-[var(--color-elevated)]/40">
							<td class="px-3 py-1"><StatusBadge status={r.status} /></td>
							<td class="px-3 py-1 text-[var(--color-muted)]">{r.stage}</td>
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
