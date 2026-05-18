<script lang="ts">
	/** Detail popup for a single history row.
	 *
	 *  For check_runs we have summary + stage/status from the list; the
	 *  full payload lives in /api/history/runs which we fetch on open.
	 *  For alarms we show the alarm metadata directly + a "find related
	 *  runs" follow-on link. The rich verify-yourself UI lives in the
	 *  Timeline drill-down — we link there for the deep dive.
	 */
	import { api } from '$lib/api';
	import { url as apiUrl } from '$lib/origin';
	import { stageLabel, stageTechCode, severityChip, prettyCheckLabel } from '$lib/format';
	import StatusBadge from './StatusBadge.svelte';
	import LazyImage from './LazyImage.svelte';

	let {
		open = $bindable<boolean>(false),
		row = $bindable<any>(null),
		tab = 'checks'
	}: {
		open: boolean;
		row: any;
		tab: 'alarms' | 'checks';
	} = $props();

	let fullRun = $state<any>(null);
	let related = $state<any[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);

	// Refetch when the modal is opened with a new row.
	$effect(() => {
		if (!open || !row) return;
		fullRun = null;
		related = [];
		error = null;
		const ctrl = new AbortController();
		(async () => {
			loading = true;
			try {
				if (tab === 'checks') {
					const fa = new Date(row.finished_at).getTime();
					const since = new Date(fa - 60_000).toISOString();
					const until = new Date(fa + 60_000).toISOString();
					const rows = await api.historyRuns(row.check_id, row.target, since, until, 5);
					// Exact match by id if available; otherwise the closest finished_at.
					fullRun =
						rows.find((r: any) => r.id === row.id) ??
						rows.find((r: any) => r.finished_at === row.finished_at) ??
						rows[0] ?? null;
				} else {
					// Alarm — fetch the check_runs that fired during its window so the
					// user can see what evidence triggered it.
					const opened = new Date(row.opened_at).getTime();
					const closed = row.closed_at ? new Date(row.closed_at).getTime() : Date.now();
					const since = new Date(opened - 60_000).toISOString();
					const until = new Date(closed + 60_000).toISOString();
					related = await api.historyRuns(row.check_id, row.target, since, until, 30);
				}
			} catch (e) {
				error = (e as Error).message;
			} finally {
				loading = false;
			}
		})();
		return () => ctrl.abort();
	});

	function close() {
		open = false;
		fullRun = null;
		related = [];
		error = null;
	}

	function escListener(e: KeyboardEvent) {
		if (e.key === 'Escape') close();
	}
	$effect(() => {
		if (open) {
			document.addEventListener('keydown', escListener);
			return () => document.removeEventListener('keydown', escListener);
		}
	});

	// "Open in Timeline" — no per-row deeplink yet (timeline tabs are by
	// stage); send the user to the page and let them filter.
	const timelineHref = '/timeline';

	function historyDeeplink(): string {
		if (!row) return '/history';
		const params = new URLSearchParams();
		if (row.stage)    params.set('stage',    row.stage);
		if (row.target)   params.set('target',   row.target);
		if (row.check_id) params.set('check_id', row.check_id);
		params.set('tab', tab);
		if (tab === 'checks') {
			const fa = new Date(row.finished_at).getTime();
			params.set('since', new Date(fa - 5 * 60_000).toISOString());
			params.set('until', new Date(fa + 5 * 60_000).toISOString());
		} else {
			params.set('since', row.opened_at);
			if (row.closed_at) params.set('until', row.closed_at);
		}
		return `/history?${params}`;
	}

	// L4 image URL — only present when the run hit a per-radar L4 check that
	// captured a PNG snapshot. Payload key is `source` (set by
	// layer4_image.py); we keep `image_source` as a fallback for any
	// legacy rows.
	function capturedImage(run: any): string | null {
		const src = run?.payload?.source ?? run?.payload?.image_source;
		if (!src) return null;
		return apiUrl(`/api/upstream/image_by_source.png?source=${encodeURIComponent(src)}`);
	}

	// For alarm rows the alarm record itself has no payload (the API list
	// strips it) — but the modal fetches the underlying check_runs that
	// fired during the alarm window into `related`. Walk those to find a
	// run whose payload carries an image source.
	function alarmCapturedImage(): { url: string; run: any } | null {
		for (const r of related) {
			const u = capturedImage(r);
			if (u) return { url: u, run: r };
		}
		return null;
	}
	function alarmAnyL4Verdicts(): { run: any; l4v: Array<[string, string, string]> } | null {
		for (const r of related) {
			const l4v = l4Verdicts(r);
			if (l4v) return { run: r, l4v };
		}
		return null;
	}

	// Render the reasoning trail for the cached run. For L1 we surface
	// the sub-check verdict dict (sub_status). For L2 we surface the
	// reconcile bundle (verdict, primary_age_s, silent_fail_s, dead
	// moments). For L4 we surface tier1 + tier2 verdicts. Everything
	// shows in compact tabular form so a maintainer can read it without
	// expanding the raw payload.
	function l1SubStatus(run: any): Array<[string, string]> | null {
		const s = run?.payload?.sub_status;
		if (!s || typeof s !== 'object') return null;
		return Object.entries(s).map(([k, v]) => [k, String(v)] as [string, string]);
	}
	function l2Reconcile(run: any): Record<string, any> | null {
		return run?.payload?.reconcile ?? null;
	}
	function l4Verdicts(run: any): Array<[string, string, string]> | null {
		const t1 = run?.payload?.tier1;
		const t2 = run?.payload?.tier2;
		if (!t1 || !t2) return null;
		const out: Array<[string, string, string]> = [];
		if (typeof t1.coverage_pct === 'number') out.push(['coverage', `${t1.coverage_pct.toFixed(1)}%`, '']);
		if (typeof t1.autocorr === 'number')     out.push(['autocorr',  t1.autocorr.toFixed(3), '']);
		for (const [k, label] of [['extreme','EXTREME'], ['speckle','SPECKLE'], ['range_ring','RANGE RING'], ['frozen','FROZEN']]) {
			const v = (t2 as any)[k];
			if (v?.verdict !== undefined) {
				out.push([label, v.verdict, v.fraction != null ? `frac=${Number(v.fraction).toFixed(3)}` : '']);
			}
		}
		return out;
	}

	function fmtTs(s: string | null | undefined): string {
		return s ? s.slice(0, 19).replace('T', ' ') + ' UTC' : '—';
	}
</script>

{#if open && row}
	<div
		class="fixed inset-0 z-50 flex items-start justify-center bg-black/55 backdrop-blur-sm p-4 overflow-y-auto"
		onclick={close}
		role="dialog"
		aria-modal="true"
		aria-labelledby="history-detail-title"
	>
		<div
			class="relative my-8 w-full max-w-[760px] rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] shadow-2xl"
			onclick={(e) => e.stopPropagation()}
			role="document"
		>
			<!-- Header -->
			<header class="flex items-start justify-between gap-3 border-b border-[var(--color-border)] px-5 py-3">
				<div class="min-w-0 flex-1">
					<div class="flex items-center gap-2 text-[10px] uppercase tracking-[0.16em] text-[var(--color-muted)]">
						{#if tab === 'alarms'}
							<span class={severityChip(row.severity)}>{row.severity}</span>
							<span>·</span>
							<span class="num text-[var(--color-bright)]">alarm #{row.id}</span>
						{:else}
							<StatusBadge status={row.status} />
							<span>·</span>
							<span class="num text-[var(--color-bright)]">run #{row.id}</span>
						{/if}
						<span>·</span>
						<span title={`Internal stage: ${row.stage}`}>{stageLabel(row.stage)}</span>
					</div>
					<h2 id="history-detail-title" class="mt-1 text-[15px] font-semibold text-[var(--color-bright)]">
						{prettyCheckLabel(row.check_id, row.target)}
					</h2>
					<div class="num text-[11px] text-[var(--color-faint)] truncate">{row.check_id}</div>
				</div>
				<button
					class="inline-flex h-6 w-6 items-center justify-center text-[var(--color-muted)] hover:text-[var(--color-bright)]"
					aria-label="close"
					onclick={close}
				>
					<svg viewBox="0 0 12 12" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5">
						<line x1="2.5" y1="2.5" x2="9.5" y2="9.5" />
						<line x1="9.5" y1="2.5" x2="2.5" y2="9.5" />
					</svg>
				</button>
			</header>

			<!-- Body -->
			<div class="space-y-4 px-5 py-4 text-[12px]">
				<!-- Summary grid -->
				<dl class="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-[11.5px] num">
					<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Target</dt>
					<dd class="text-[var(--color-default)]">{row.target || '—'}</dd>

					{#if tab === 'checks'}
						<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Started</dt>
						<dd class="text-[var(--color-default)]">{fmtTs(row.started_at)}</dd>
						<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Finished</dt>
						<dd class="text-[var(--color-default)]">{fmtTs(row.finished_at)}</dd>
					{:else}
						<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Opened</dt>
						<dd class="text-[var(--color-default)]">{fmtTs(row.opened_at)}</dd>
						<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Closed</dt>
						<dd class="text-[var(--color-default)]">{row.closed_at ? fmtTs(row.closed_at) : '— (still open)'}</dd>
						{#if row.suppressed_by}
							<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Suppressed</dt>
							<dd class="text-[var(--color-warn)]">{row.suppressed_by}</dd>
						{/if}
					{/if}
				</dl>

				<!-- Message / summary -->
				{#if tab === 'alarms' && row.message}
					<section>
						<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Message</div>
						<pre class="whitespace-pre-wrap rounded-sm border border-[var(--color-border)] bg-[var(--color-canvas)] px-3 py-2 text-[11.5px] num text-[var(--color-default)] leading-snug">{row.message}</pre>
					</section>
				{/if}
				{#if tab === 'checks' && (fullRun?.summary || row.summary)}
					<section>
						<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Summary</div>
						<pre class="whitespace-pre-wrap rounded-sm border border-[var(--color-border)] bg-[var(--color-canvas)] px-3 py-2 text-[11.5px] num text-[var(--color-default)] leading-snug">{fullRun?.summary || row.summary}</pre>
					</section>
				{/if}

				<!-- Captured image (L4 only) -->
				{#if tab === 'checks' && fullRun}
					{@const img = capturedImage(fullRun)}
					{#if img}
						<section>
							<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Captured image</div>
							<div class="overflow-hidden rounded-sm border border-[var(--color-border)] bg-[var(--color-canvas)]">
								<LazyImage src={img} alt="captured scan" minHeight={240} />
							</div>
						</section>
					{/if}

					<!-- L4 tier1 + tier2 verdicts (image-QC reasoning trail) -->
					{@const l4v = l4Verdicts(fullRun)}
					{#if l4v}
						<section>
							<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Image QC verdicts</div>
							<table class="w-full rounded-sm border border-[var(--color-border)] bg-[var(--color-canvas)] text-[11.5px] num">
								<tbody>
									{#each l4v as [k, v, hint]}
										<tr class="border-b border-[var(--color-border)]/40 last:border-b-0">
											<td class="px-3 py-1 text-[var(--color-muted)] uppercase tracking-wider text-[10.5px]">{k}</td>
											<td class="px-3 py-1 text-[var(--color-bright)]">{v}</td>
											<td class="px-3 py-1 text-[var(--color-faint)]">{hint}</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</section>
					{/if}

					<!-- L1 sub-check verdict trail -->
					{@const sub = l1SubStatus(fullRun)}
					{#if sub}
						<section>
							<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Sub-check verdicts</div>
							<table class="w-full rounded-sm border border-[var(--color-border)] bg-[var(--color-canvas)] text-[11.5px] num">
								<tbody>
									{#each sub as [k, v]}
										<tr class="border-b border-[var(--color-border)]/40 last:border-b-0">
											<td class="px-3 py-1 text-[var(--color-muted)] uppercase tracking-wider text-[10.5px]">{k}</td>
											<td class="px-3 py-1 {v === 'pass' ? 'text-[var(--color-ok)]' : v === 'warn' ? 'text-[var(--color-warn)]' : v === 'fail' || v === 'error' ? 'text-[var(--color-fail)]' : 'text-[var(--color-muted)]'}">{v}</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</section>
					{/if}

					<!-- L2 reconcile bundle -->
					{@const reco = l2Reconcile(fullRun)}
					{#if reco}
						<section>
							<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Radar reconciliation</div>
							<dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 rounded-sm border border-[var(--color-border)] bg-[var(--color-canvas)] px-3 py-2 text-[11.5px] num">
								{#each Object.entries(reco) as [k, v]}
									{#if typeof v !== 'object' || v === null}
										<dt class="text-[var(--color-muted)] uppercase tracking-wider text-[10.5px]">{k}</dt>
										<dd class="text-[var(--color-bright)]">{String(v)}</dd>
									{/if}
								{/each}
							</dl>
						</section>
					{/if}
				{/if}

				<!-- Loading / error -->
				{#if loading}
					<div class="text-[11px] text-[var(--color-muted)]">loading details…</div>
				{/if}
				{#if error}
					<div class="rounded-sm border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-3 py-2 text-[11px] text-[var(--color-fail)]">
						{error}
					</div>
				{/if}

				<!-- Related runs (alarms only) -->
				{#if tab === 'alarms' && related.length > 0}
					{@const alarmImg = alarmCapturedImage()}
					{#if alarmImg}
						<section>
							<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">
								Captured image (from triggering run)
							</div>
							<div class="overflow-hidden rounded-sm border border-[var(--color-border)] bg-[var(--color-canvas)]">
								<LazyImage src={alarmImg.url} alt="captured scan" minHeight={240} />
							</div>
						</section>
					{/if}

					{@const aV = alarmAnyL4Verdicts()}
					{#if aV}
						<section>
							<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">
								Image QC verdicts (from triggering run)
							</div>
							<table class="w-full rounded-sm border border-[var(--color-border)] bg-[var(--color-canvas)] text-[11.5px] num">
								<tbody>
									{#each aV.l4v as [k, v, hint]}
										<tr class="border-b border-[var(--color-border)]/40 last:border-b-0">
											<td class="px-3 py-1 text-[var(--color-muted)] uppercase tracking-wider text-[10.5px]">{k}</td>
											<td class="px-3 py-1 text-[var(--color-bright)]">{v}</td>
											<td class="px-3 py-1 text-[var(--color-faint)]">{hint}</td>
										</tr>
									{/each}
								</tbody>
							</table>
						</section>
					{/if}

					<section>
						<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">
							Runs during alarm window ({related.length})
						</div>
						<ul class="max-h-[200px] overflow-y-auto rounded-sm border border-[var(--color-border)] divide-y divide-[var(--color-border)]/60">
							{#each related as r}
								<li class="flex items-center gap-3 px-3 py-1.5 text-[11.5px] num">
									<StatusBadge status={r.status} />
									<span class="text-[var(--color-muted)]">{r.finished_at?.slice(11, 19)}Z</span>
									<span class="flex-1 truncate text-[var(--color-default)]">{r.summary || ''}</span>
								</li>
							{/each}
						</ul>
					</section>
				{/if}

				<!-- Raw payload (collapsible) -->
				{#if tab === 'checks' && fullRun?.payload}
					<details>
						<summary class="cursor-pointer text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)] hover:text-[var(--color-default)]">
							Raw payload
						</summary>
						<pre class="mt-1 max-h-[280px] overflow-auto whitespace-pre-wrap rounded-sm border border-[var(--color-border)] bg-[var(--color-canvas)] px-3 py-2 text-[10.5px] num text-[var(--color-muted)] leading-snug">{JSON.stringify(fullRun.payload, null, 2)}</pre>
					</details>
				{/if}
			</div>

			<!-- Footer actions -->
			<footer class="flex items-center justify-between gap-3 border-t border-[var(--color-border)] bg-[var(--color-canvas)]/40 px-5 py-3 text-[11px]">
				<div class="text-[10px] text-[var(--color-faint)]">
					{#if tab === 'checks'}
						For the full verify-yourself view (thresholds, upstream URLs, replication recipe), open in Timeline.
					{:else}
						See the runs above for what triggered this alarm.
					{/if}
				</div>
				<div class="flex gap-2">
					<a
						class="border border-[var(--color-border-strong)] px-3 py-1 uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)] hover:bg-[var(--color-elevated)]/40"
						href={timelineHref}
					>
						open in timeline
					</a>
					<a
						class="border border-[var(--color-border-strong)] bg-[var(--color-elevated)]/30 px-3 py-1 uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-elevated)]/60"
						href={historyDeeplink()}
					>
						open this exact in history
					</a>
				</div>
			</footer>
		</div>
	</div>
{/if}
