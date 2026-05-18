<script lang="ts">
	/** /m/timeline — vertical mobile timeline.
	 *
	 *  Plan spec: "shows a vertical timeline view, ensure it is
	 *  aesthetically pleasing and functional without any jumbled/overlapping
	 *  text". Approach: a chronological list of alarm open/close events
	 *  plus current stage rollup at the top. One row per event, status
	 *  marker on the left, time on the right. No horizontal scroll. Tap
	 *  any row to drill into the alarm (reuses MobileDrillDown).
	 *
	 *  Time-bucket pivoting (the desktop's wide grid) doesn't fit a
	 *  320px viewport — keep the desktop /timeline page as the rich view
	 *  and link to it via "switch to desktop" from /m/more.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { sentinel } from '$lib/stores/state.svelte';
	import { url as apiUrl } from '$lib/origin';
	import { fmtAge, severityChip, stageLabel, prettyCheckLabel } from '$lib/format';
	import MobileDrillDown from '$lib/components/mobile/MobileDrillDown.svelte';
	import PieStatus from '$lib/components/PieStatus.svelte';

	type Event = {
		kind: 'open' | 'close';
		id: number;
		ts: string;             // wall-clock ISO
		check_id: string;
		target: string;
		stage: string;
		severity: string;
		status?: string;        // close → final status; open → severity
		message: string;
	};

	let events = $state<Event[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let now = $state(Date.now());
	let tickTimer: ReturnType<typeof setInterval>;

	const counts = $derived(sentinel.rollup?.counts ?? {});
	const totalCounts = $derived.by(() => {
		const out = { pass: 0, warn: 0, fail: 0, error: 0, skip: 0 };
		for (const c of Object.values(counts)) {
			out.pass  += c.pass  ?? 0;
			out.warn  += c.warn  ?? 0;
			out.fail  += c.fail  ?? 0;
			out.error += c.error ?? 0;
			out.skip  += c.skip  ?? 0;
		}
		return out;
	});

	// Load the past 24h of alarm history. Each row spawns one or two
	// timeline events (open, and close if closed_at present).
	async function load() {
		loading = true;
		error = null;
		try {
			const since = new Date(Date.now() - 86400_000).toISOString();
			const r = await fetch(apiUrl(`/api/history/alarms?since=${encodeURIComponent(since)}&limit=200`), {
				headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {}
			});
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			const rows: any[] = await r.json();
			const out: Event[] = [];
			for (const a of rows) {
				out.push({
					kind: 'open', id: a.id, ts: a.opened_at,
					check_id: a.check_id, target: a.target, stage: a.stage,
					severity: a.severity, message: a.message
				});
				if (a.closed_at) {
					out.push({
						kind: 'close', id: a.id, ts: a.closed_at,
						check_id: a.check_id, target: a.target, stage: a.stage,
						severity: a.severity, status: 'pass',
						message: `Cleared after ${fmtAge((Date.parse(a.closed_at) - Date.parse(a.opened_at)) / 1000)}`
					});
				}
			}
			// Newest first.
			out.sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts));
			events = out;
		} catch (e) {
			error = (e as Error).message;
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		load();
		tickTimer = setInterval(() => (now = Date.now()), 30_000);
	});
	onDestroy(() => { if (tickTimer) clearInterval(tickTimer); });

	function sinceText(ts: string): string {
		const s = (now - Date.parse(ts)) / 1000;
		return s < 0 ? 'in future' : `${fmtAge(s)} ago`;
	}

	function eventColor(e: Event): string {
		if (e.kind === 'close') return 'var(--color-ok)';
		if (e.severity === 'critical') return 'var(--color-fail)';
		if (e.severity === 'warn') return 'var(--color-warn)';
		return 'var(--color-info)';
	}

	function groupByDay(evts: Event[]): { day: string; items: Event[] }[] {
		const out: Record<string, Event[]> = {};
		const order: string[] = [];
		for (const e of evts) {
			const day = e.ts.slice(0, 10);
			if (!(day in out)) { out[day] = []; order.push(day); }
			out[day].push(e);
		}
		return order.map((d) => ({ day: d, items: out[d] }));
	}
	const grouped = $derived(groupByDay(events));

	// Drill-down state.
	let detailOpen = $state(false);
	let detailEvent = $state<Event | null>(null);
	function openDetail(e: Event) {
		detailEvent = e;
		detailOpen = true;
	}

	function fmtDay(iso: string): string {
		const d = new Date(iso + 'T00:00:00Z');
		const today = new Date(new Date().toISOString().slice(0, 10) + 'T00:00:00Z').getTime();
		const ms = d.getTime();
		if (ms === today) return 'Today';
		if (ms === today - 86400_000) return 'Yesterday';
		return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' });
	}
</script>

<div class="px-4 py-4">
	<!-- Header with current-state pie -->
	<header class="mb-4 flex items-center gap-3">
		<PieStatus counts={totalCounts} size={28} />
		<div>
			<h1 class="text-[16px] font-semibold tracking-wide text-[var(--color-bright)]">Timeline</h1>
			<div class="text-[11px] text-[var(--color-muted)]">
				{totalCounts.pass} pass · {totalCounts.warn} warn · {totalCounts.fail + totalCounts.error} fail · last 24h
			</div>
		</div>
	</header>

	{#if error}
		<div class="mb-3 rounded-sm border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-3 py-2 text-[12px] text-[var(--color-fail)]">{error}</div>
	{/if}

	{#if loading}
		<div class="text-[12px] text-[var(--color-muted)]">loading…</div>
	{:else if events.length === 0}
		<div class="rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-6 text-center text-[12px] text-[var(--color-muted)]">
			Nothing in the last 24 h. Everything's been quiet.
		</div>
	{:else}
		{#each grouped as g}
			<div class="mb-2 mt-3 px-1 text-[10px] uppercase tracking-[0.16em] text-[var(--color-muted)]">{fmtDay(g.day)}</div>
			<ul class="overflow-hidden rounded-md border border-[var(--color-border)] bg-[var(--color-surface)]">
				{#each g.items as e (`${e.kind}-${e.id}-${e.ts}`)}
					<li class="border-b border-[var(--color-border)]/60 last:border-b-0">
						<button
							type="button"
							onclick={() => openDetail(e)}
							class="flex w-full items-start gap-3 px-3 py-2.5 text-left active:bg-[var(--color-elevated)]/60"
							style="-webkit-tap-highlight-color: transparent;"
						>
							<!-- Vertical timeline rail + marker -->
							<div class="relative flex w-3 shrink-0 flex-col items-center self-stretch py-1">
								<span class="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-[var(--color-border)]"></span>
								<span class="relative mt-1 inline-block h-2.5 w-2.5 rounded-full" style="background:{eventColor(e)};"></span>
							</div>
							<div class="min-w-0 flex-1">
								<div class="flex items-baseline justify-between gap-2">
									<span class="num truncate text-[13px] text-[var(--color-bright)]">
										{prettyCheckLabel(e.check_id, e.target)}
									</span>
									<span class="num shrink-0 text-[10.5px] text-[var(--color-faint)]">
										{e.ts.slice(11, 16)}Z
									</span>
								</div>
								<div class="mt-0.5 flex items-center gap-2 text-[10.5px] uppercase tracking-wider">
									{#if e.kind === 'open'}
										<span class={severityChip(e.severity)}>{e.severity}</span>
									{:else}
										<span class="text-[var(--color-ok)]">cleared</span>
									{/if}
									<span class="text-[var(--color-muted)] num" title={e.stage}>{stageLabel(e.stage)}</span>
								</div>
								{#if e.message}
									<div class="mt-1 line-clamp-2 text-[12px] num text-[var(--color-default)]">
										{e.message}
									</div>
								{/if}
								<div class="mt-0.5 text-[10.5px] text-[var(--color-faint)] num">{sinceText(e.ts)}</div>
							</div>
						</button>
					</li>
				{/each}
			</ul>
		{/each}
	{/if}

	<!-- Bottom-nav clearance so the last row isn't covered. -->
	<div style="height: calc(56px + env(safe-area-inset-bottom, 0px));"></div>
</div>

<MobileDrillDown
	bind:open={detailOpen}
	title={detailEvent ? prettyCheckLabel(detailEvent.check_id, detailEvent.target) : ''}
	subtitle={detailEvent?.check_id ?? ''}
	stage={detailEvent?.stage ?? ''}
	status={detailEvent?.kind === 'close' ? 'pass' : (detailEvent?.severity ?? '')}
>
	{#if detailEvent}
		<dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1.5 text-[12px] num">
			<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Target</dt>
			<dd class="text-[var(--color-default)]">{detailEvent.target || '—'}</dd>
			<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">When</dt>
			<dd class="text-[var(--color-default)]">{detailEvent.ts.slice(0, 19).replace('T', ' ')} UTC</dd>
			<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Kind</dt>
			<dd class="text-[var(--color-bright)]">{detailEvent.kind === 'open' ? 'Alarm opened' : 'Alarm cleared'}</dd>
		</dl>
		{#if detailEvent.message}
			<section class="mt-4">
				<div class="mb-1 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Message</div>
				<pre class="whitespace-pre-wrap rounded-sm border border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-[12px] num text-[var(--color-default)] leading-snug">{detailEvent.message}</pre>
			</section>
		{/if}
		<div class="mt-5 flex flex-col gap-2">
			<a
				href={`/history?check_id=${encodeURIComponent(detailEvent.check_id)}&target=${encodeURIComponent(detailEvent.target)}&stage=${encodeURIComponent(detailEvent.stage)}&tab=alarms`}
				class="w-full rounded-md border border-[var(--color-border-strong)] bg-[var(--color-elevated)]/30 px-3 py-2.5 text-center text-[12px] uppercase tracking-wider text-[var(--color-bright)] active:bg-[var(--color-elevated)]/60"
				style="-webkit-tap-highlight-color: transparent;"
			>
				Open in History
			</a>
		</div>
	{/if}
</MobileDrillDown>
