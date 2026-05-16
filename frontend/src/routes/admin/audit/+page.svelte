<script lang="ts">
	import { onMount } from 'svelte';
	interface AuditRow {
		id: number; at: string;
		user_email: string; action: string;
		target: string | null;
		payload: Record<string, unknown> | null;
	}
	let rows = $state<AuditRow[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	async function load() {
		loading = true; error = null;
		try {
			const r = await fetch('/api/admin/audit?limit=500', { credentials: 'include' });
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			rows = await r.json();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			loading = false;
		}
	}
	onMount(load);

	function fmt(iso: string) { return iso.slice(0, 19).replace('T', ' '); }
</script>

<div class="p-6 max-w-5xl">
	<div class="label tracking-[0.18em] text-[var(--color-bright)] mb-1">AUDIT LOG</div>
	<div class="text-[11px] text-[var(--color-muted)] mb-4">
		Who did what and when. Every admin write action lands here.
	</div>

	{#if error}<div class="mb-4 border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-3 py-2 text-[12px] text-[var(--color-fail)]">{error}</div>{/if}

	{#if loading}
		<div class="text-[12px] text-[var(--color-muted)]">loading…</div>
	{:else if !rows.length}
		<div class="text-[12px] text-[var(--color-faint)] italic">no audit entries yet.</div>
	{:else}
		<table class="w-full text-[11px]">
			<thead class="text-[var(--color-muted)]">
				<tr class="border-b border-[var(--color-border)]">
					<th class="px-2 py-1 text-left">at</th>
					<th class="px-2 py-1 text-left">user</th>
					<th class="px-2 py-1 text-left">action</th>
					<th class="px-2 py-1 text-left">target</th>
					<th class="px-2 py-1 text-left">payload</th>
				</tr>
			</thead>
			<tbody>
				{#each rows as r}
					<tr class="border-b border-[var(--color-border)] hover:bg-[var(--color-elevated)]/40">
						<td class="px-2 py-1 num text-[var(--color-muted)] whitespace-nowrap">{fmt(r.at)}</td>
						<td class="px-2 py-1 num text-[var(--color-default)]">{r.user_email}</td>
						<td class="px-2 py-1 num text-[var(--color-bright)]">{r.action}</td>
						<td class="px-2 py-1 num text-[var(--color-default)]">{r.target ?? '—'}</td>
						<td class="px-2 py-1 num text-[var(--color-muted)] truncate max-w-md" title={r.payload ? JSON.stringify(r.payload) : ''}>
							{r.payload ? JSON.stringify(r.payload) : ''}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>
