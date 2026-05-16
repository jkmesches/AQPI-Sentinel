<script lang="ts">
	import { onMount } from 'svelte';

	interface Silence {
		id: string;
		matchers: Record<string, string>;
		starts: string;
		ends: string;
		reason: string | null;
		created_at: string;
		created_by: string | null;
	}

	let rows = $state<Silence[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// new-silence form
	let newId = $state('');
	let newMatchers = $state('{"target":"XSCV"}');
	let newStarts = $state(new Date().toISOString().slice(0, 16));
	let newEnds = $state(new Date(Date.now() + 86400_000).toISOString().slice(0, 16));
	let newReason = $state('');
	let busy = $state(false);

	async function load() {
		loading = true;
		error = null;
		try {
			const r = await fetch('/api/silences');
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			rows = await r.json();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			loading = false;
		}
	}

	async function del(sid: string) {
		if (!confirm(`Delete silence "${sid}"?`)) return;
		try {
			const r = await fetch(`/api/silences/${encodeURIComponent(sid)}`, {
				method: 'DELETE'
			});
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			await load();
		} catch (e) {
			alert(`delete failed: ${(e as Error).message}`);
		}
	}

	async function create(e: Event) {
		e.preventDefault();
		busy = true;
		try {
			const matchers = JSON.parse(newMatchers);
			const r = await fetch('/api/silences', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				
				body: JSON.stringify({
					id:       newId,
					matchers,
					starts:   new Date(newStarts).toISOString(),
					ends:     new Date(newEnds).toISOString(),
					reason:   newReason || null
				})
			});
			if (!r.ok) {
				const j = await r.json().catch(() => null);
				throw new Error(j?.detail ?? `HTTP ${r.status}`);
			}
			newId = '';
			newReason = '';
			await load();
		} catch (e) {
			alert(`create failed: ${(e as Error).message}`);
		} finally {
			busy = false;
		}
	}

	function fmt(iso: string): string {
		return iso.slice(0, 19).replace('T', ' ');
	}
	function active(s: Silence): boolean {
		const now = Date.now();
		return Date.parse(s.starts) <= now && now < Date.parse(s.ends);
	}

	onMount(load);
</script>

<div class="p-6 max-w-4xl">
	<div class="label tracking-[0.18em] text-[var(--color-bright)] mb-1">SILENCES</div>
	<div class="text-[11px] text-[var(--color-muted)] mb-6">
		Suppress alarms matching the given criteria during the window. Useful during planned
		downtime or known-bad upstream issues.
	</div>

	{#if error}
		<div class="mb-4 border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-3 py-2 text-[12px] text-[var(--color-fail)]">
			{error}
		</div>
	{/if}

	<!-- ACTIVE / PAST LIST -->
	{#if loading}
		<div class="text-[12px] text-[var(--color-muted)]">loading…</div>
	{:else if !rows.length}
		<div class="text-[12px] text-[var(--color-faint)] italic">no silences.</div>
	{:else}
		<table class="w-full text-[11px] mb-6">
			<thead class="text-[var(--color-muted)]">
				<tr class="border-b border-[var(--color-border)]">
					<th class="px-2 py-1 text-left">id</th>
					<th class="px-2 py-1 text-left">matchers</th>
					<th class="px-2 py-1 text-left">window</th>
					<th class="px-2 py-1 text-left">reason</th>
					<th class="px-2 py-1 text-left">by</th>
					<th class="px-2 py-1"></th>
				</tr>
			</thead>
			<tbody>
				{#each rows as s}
					<tr class="border-b border-[var(--color-border)] {active(s) ? '' : 'opacity-50'}">
						<td class="px-2 py-1 num text-[var(--color-bright)]">{s.id}</td>
						<td class="px-2 py-1 num text-[var(--color-default)] truncate max-w-xs" title={JSON.stringify(s.matchers)}>
							{JSON.stringify(s.matchers)}
						</td>
						<td class="px-2 py-1 num text-[var(--color-muted)]">
							{fmt(s.starts)} → {fmt(s.ends)}
							{#if active(s)}<span class="ml-1 text-[var(--color-ok)] uppercase text-[9.5px]">active</span>{/if}
						</td>
						<td class="px-2 py-1 text-[var(--color-muted)] truncate max-w-md">{s.reason ?? '—'}</td>
						<td class="px-2 py-1 num text-[var(--color-muted)]">{s.created_by ?? '?'}</td>
						<td class="px-2 py-1 text-right">
							<button
								class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-fail)]"
								onclick={() => del(s.id)}
							>delete</button>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}

	<!-- NEW SILENCE FORM -->
	<form class="border border-[var(--color-border)] bg-[var(--color-canvas)] p-4" onsubmit={create}>
		<div class="label text-[var(--color-bright)] mb-3">NEW SILENCE</div>
		<div class="grid grid-cols-[7rem_1fr] gap-x-4 gap-y-3 text-[12px] items-center">
			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">id</label>
			<input bind:value={newId} required class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[12px] num" placeholder="xscv-maintenance" />

			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">matchers (JSON)</label>
			<input bind:value={newMatchers} required class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[12px] num" />

			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">starts</label>
			<input bind:value={newStarts} type="datetime-local" class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[12px] num w-64" />

			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">ends</label>
			<input bind:value={newEnds} type="datetime-local" class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[12px] num w-64" />

			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">reason</label>
			<input bind:value={newReason} class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[12px]" placeholder="planned downtime" />
		</div>
		<div class="mt-4">
			<button
				type="submit"
				disabled={busy}
				class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-elevated)] disabled:opacity-50"
			>
				{busy ? 'creating…' : 'create silence'}
			</button>
		</div>
	</form>
</div>
