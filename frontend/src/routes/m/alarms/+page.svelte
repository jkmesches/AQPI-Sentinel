<script lang="ts">
	import { sentinel } from '$lib/stores/state.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { api } from '$lib/api';
	import { stageLabel } from '$lib/format';
	import { onMount, onDestroy } from 'svelte';

	let now = $state(Date.now());
	let tickTimer: ReturnType<typeof setInterval>;
	onMount(() => { tickTimer = setInterval(() => (now = Date.now()), 5000); });
	onDestroy(() => { if (tickTimer) clearInterval(tickTimer); });

	// Severity-then-recency sort. Critical first, then warn, then info.
	const SEV_RANK: Record<string, number> = { critical: 0, warn: 1, info: 2 };
	const sorted = $derived(
		[...sentinel.alarms].sort((a, b) => {
			const dsr = (SEV_RANK[a.severity] ?? 9) - (SEV_RANK[b.severity] ?? 9);
			if (dsr !== 0) return dsr;
			return Date.parse(b.opened_at) - Date.parse(a.opened_at);
		})
	);

	let busy = $state<Record<number, boolean>>({});
	let banner = $state<{ kind: 'ok' | 'err'; text: string } | null>(null);

	function ageOf(iso: string): string {
		const s = Math.max(0, (now - Date.parse(iso)) / 1000);
		if (s < 60) return `${s | 0}s`;
		if (s < 3600) return `${(s / 60) | 0}m`;
		if (s < 86400) return `${(s / 3600) | 0}h`;
		return `${(s / 86400) | 0}d`;
	}

	function sevClass(sev: string): string {
		if (sev === 'critical') return 'text-[var(--color-critical)]';
		if (sev === 'warn') return 'text-[var(--color-warn)]';
		return 'text-[var(--color-info)]';
	}

	async function ack(id: number) {
		busy[id] = true;
		banner = null;
		try {
			await api.ack(id);
			await sentinel.refresh();
			banner = { kind: 'ok', text: 'acknowledged' };
		} catch (e) {
			banner = { kind: 'err', text: (e as Error).message };
		} finally {
			busy[id] = false;
		}
	}
</script>

{#if banner}
	<div class="mb-3 rounded border px-3 py-2 text-[12px] {banner.kind === 'ok'
		? 'border-[var(--color-ok)]/40 bg-[var(--color-ok)]/10 text-[var(--color-ok)]'
		: 'border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 text-[var(--color-fail)]'}">
		{banner.text}
	</div>
{/if}

{#if sentinel.loading && !sentinel.alarms.length}
	<div class="px-2 py-6 text-center text-[13px] text-[var(--color-muted)]">loading…</div>
{:else if sorted.length === 0}
	<div class="px-2 py-10 text-center">
		<div class="text-[40px] text-[var(--color-ok)]">✓</div>
		<div class="mt-2 text-[13px] uppercase tracking-[0.16em] text-[var(--color-muted)]">no open alarms</div>
	</div>
{:else}
	<div class="mb-3 px-1 text-[11px] uppercase tracking-[0.16em] text-[var(--color-muted)]">
		{sorted.length} open
	</div>
	<ul class="space-y-2.5">
		{#each sorted as a (a.id)}
			<li class="relative overflow-hidden rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] {sevClass(a.severity)}">
				<!-- left severity stripe -->
				<span class="absolute inset-y-0 left-0 w-1" style="background: currentColor;"></span>
				<div class="pl-3 pr-3 py-3">
					<div class="flex items-center gap-2 text-[10px] uppercase tracking-[0.14em]">
						<span class="font-semibold">{a.severity}</span>
						<span class="text-[var(--color-faint)]">·</span>
						<span class="num text-[var(--color-muted)]" title={a.stage}>{stageLabel(a.stage)}</span>
						<span class="text-[var(--color-faint)]">·</span>
						<span class="num text-[var(--color-muted)]">{ageOf(a.opened_at)}</span>
						{#if a.suppressed_by}
							<span class="ml-auto rounded-sm bg-[var(--color-elevated)] px-1.5 py-0.5 text-[9.5px] text-[var(--color-muted)]">suppressed</span>
						{/if}
					</div>
					<div class="num mt-1.5 text-[13.5px] text-[var(--color-bright)]">
						{a.check_id}{#if a.target} · <span class="text-[var(--color-default)]">{a.target}</span>{/if}
					</div>
					{#if a.message}
						<div class="mt-1 text-[12px] text-[var(--color-default)]">{a.message}</div>
					{/if}

					{#if a.ack?.acked_at}
						<div class="num mt-2 text-[11px] text-[var(--color-muted)]">
							✓ acked by {a.ack.acked_by} · {ageOf(a.ack.acked_at)} ago
						</div>
					{:else if auth.user}
						<div class="mt-2 flex gap-2">
							<button
								type="button"
								onclick={() => ack(a.id)}
								disabled={busy[a.id]}
								class="min-h-[40px] flex-1 rounded border border-[var(--color-border-strong)] bg-[var(--color-surface-hi)] text-[12px] uppercase tracking-wider text-[var(--color-bright)] disabled:opacity-50"
								style="-webkit-tap-highlight-color: transparent;"
							>
								{busy[a.id] ? 'acking…' : 'acknowledge'}
							</button>
						</div>
					{:else}
						<div class="mt-2 text-[11px] text-[var(--color-faint)]">
							<a href="/login?next={encodeURIComponent('/m/alarms')}" class="underline">sign in</a> to ack
						</div>
					{/if}
				</div>
			</li>
		{/each}
	</ul>
{/if}
