<script lang="ts">
	import { onMount } from 'svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { url as apiUrl } from '$lib/origin';

	type ProductOverride = {
		max_freshness_s?: number | null;
		min_png_bytes?:   number | null;
		expected_steps?:  number | null;
		cadence_s?:       number | null;
	};
	type RadarOverride = { silent_fail_s?: number | null };
	type L4Override = {
		extreme_threshold?:  number | null;
		skip_frozen?:        boolean | null;
		frozen_min_cov_pct?: number | null;
		skip_range_ring?:    boolean | null;
	};
	type Globals = {
		hysteresis?:          number | null;
		cadence_tol?:         number | null;
		step_count_tol?:      number | null;
		silent_fail_default?: number | null;
	};
	type Blob = {
		products: Record<string, ProductOverride>;
		radars:   Record<string, RadarOverride>;
		l4:       Record<string, L4Override>;
		globals:  Globals;
	};

	let loading = $state(true);
	let saving = $state(false);
	let error = $state<string | null>(null);
	let info = $state<string | null>(null);

	let current = $state<Blob | null>(null);
	let defaults = $state<Blob | null>(null);
	let updatedAt = $state<string | null>(null);
	let updatedBy = $state<string | null>(null);

	// Edit buffer — what the user is currently typing. Initialized from
	// `current` after fetch; saved back to the server on Apply.
	let draft = $state<Blob | null>(null);

	async function load() {
		loading = true;
		error = null;
		try {
			const r = await fetch(apiUrl('/api/admin/thresholds'), {
				headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {}
			});
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			const j = await r.json();
			current   = j.value;
			defaults  = j.defaults;
			updatedAt = j.updated_at;
			updatedBy = j.updated_by;
			draft = structuredClone(j.value);
		} catch (e) {
			error = (e as Error).message;
		} finally {
			loading = false;
		}
	}

	onMount(load);

	// Resolve current → default for an empty-cell hint. Empty inputs in the
	// admin UI mean "use default".
	function defaultProduct(pid: string, key: keyof ProductOverride): any {
		return (defaults?.products?.[pid]?.[key] ?? null);
	}
	function defaultRadar(rid: string, key: keyof RadarOverride): any {
		return (defaults?.radars?.[rid]?.[key] ?? null);
	}
	function defaultL4(pid: string, key: keyof L4Override): any {
		return (defaults?.l4?.[pid]?.[key] ?? null);
	}
	function defaultGlobal(key: keyof Globals): any {
		return (defaults?.globals?.[key] ?? null);
	}

	function diffSummary(): { key: string; before: any; after: any }[] {
		if (!current || !draft) return [];
		const out: { key: string; before: any; after: any }[] = [];
		function walk(prefix: string, a: any, b: any) {
			const keys = new Set([...Object.keys(a ?? {}), ...Object.keys(b ?? {})]);
			for (const k of keys) {
				const va = a?.[k]; const vb = b?.[k];
				if (typeof va === 'object' && va !== null && !Array.isArray(va)) {
					walk(`${prefix}.${k}`, va, vb);
				} else if (va !== vb) {
					out.push({ key: `${prefix}.${k}`.replace(/^\./, ''), before: va, after: vb });
				}
			}
		}
		walk('', current, draft);
		return out;
	}

	let confirmOpen = $state(false);

	function openConfirm() {
		info = null;
		error = null;
		confirmOpen = true;
	}

	async function apply() {
		if (!draft) return;
		saving = true;
		error = null;
		try {
			const r = await fetch(apiUrl('/api/admin/thresholds'), {
				method: 'PUT',
				headers: {
					'content-type': 'application/json',
					...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {})
				},
				body: JSON.stringify({ value: draft })
			});
			if (!r.ok) {
				const j = await r.json().catch(() => ({}));
				throw new Error(j.detail ?? `HTTP ${r.status}`);
			}
			confirmOpen = false;
			info = 'Thresholds saved. The next check tick picks up the new values.';
			await load();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			saving = false;
		}
	}

	function resetDraft() {
		if (current) draft = structuredClone(current);
		info = null;
		error = null;
	}

	// Convert empty / NaN to null so the backend treats it as "use default".
	function parseNum(v: string): number | null {
		if (v === '' || v === null || v === undefined) return null;
		const n = Number(v);
		return Number.isFinite(n) ? n : null;
	}

	// --- Retroactive reprocess job ---
	let nowIso = new Date().toISOString().slice(0, 16);
	let oneDayAgoIso = new Date(Date.now() - 86400_000).toISOString().slice(0, 16);
	let reSince = $state(oneDayAgoIso);
	let reUntil = $state(nowIso);
	let reStages = $state<Record<string, boolean>>({ L1: true, L2: true, 'L4-T1T2': true });
	let reConfirmText = $state('');
	let reJob = $state<any>(null);
	let rePoller: ReturnType<typeof setInterval> | undefined;
	let reError = $state<string | null>(null);

	function startPolling(jobId: string) {
		if (rePoller) clearInterval(rePoller);
		rePoller = setInterval(async () => {
			try {
				const r = await fetch(apiUrl(`/api/admin/thresholds/reprocess/status?job_id=${jobId}`), {
					headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {}
				});
				if (r.ok) {
					reJob = await r.json();
					if (['completed', 'cancelled', 'error'].includes(reJob.state)) {
						clearInterval(rePoller);
						rePoller = undefined;
					}
				}
			} catch { /* keep polling */ }
		}, 1000);
	}

	async function startReprocess() {
		reError = null;
		if (reConfirmText !== 'APPLY') {
			reError = 'Type APPLY to confirm.';
			return;
		}
		const stages = Object.entries(reStages).filter(([, v]) => v).map(([k]) => k);
		if (stages.length === 0) {
			reError = 'Select at least one stage.';
			return;
		}
		try {
			const r = await fetch(apiUrl('/api/admin/thresholds/reprocess'), {
				method: 'POST',
				headers: {
					'content-type': 'application/json',
					...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {})
				},
				body: JSON.stringify({
					since: new Date(reSince + 'Z').toISOString(),
					until: new Date(reUntil + 'Z').toISOString(),
					only_stages: stages,
					confirm: 'APPLY'
				})
			});
			if (!r.ok) {
				const j = await r.json().catch(() => ({}));
				throw new Error(j.detail ?? `HTTP ${r.status}`);
			}
			const j = await r.json();
			reJob = j;
			reConfirmText = '';
			startPolling(j.job_id);
		} catch (e) {
			reError = (e as Error).message;
		}
	}

	async function cancelReprocess() {
		if (!reJob?.job_id) return;
		try {
			await fetch(apiUrl('/api/admin/thresholds/reprocess/cancel'), {
				method: 'POST',
				headers: {
					'content-type': 'application/json',
					...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {})
				},
				body: JSON.stringify({ job_id: reJob.job_id })
			});
		} catch { /* */ }
	}
</script>

<div class="flex h-full flex-col">
	<header class="flex flex-wrap items-baseline justify-between gap-2 border-b border-[var(--color-border)] px-5 py-3">
		<div class="flex items-baseline gap-3">
			<span class="label tracking-[0.18em] text-[var(--color-bright)]">THRESHOLDS</span>
			<span class="text-[11px] text-[var(--color-faint)]">
				Admin-managed knobs for every check evaluator. Changes take effect on the next tick.
			</span>
		</div>
		{#if updatedAt}
			<span class="text-[10.5px] text-[var(--color-faint)] num">
				updated {updatedAt.slice(0, 19).replace('T', ' ')}Z by {updatedBy ?? '—'}
			</span>
		{/if}
	</header>

	{#if loading}
		<div class="px-5 py-6 text-[12px] text-[var(--color-muted)]">loading…</div>
	{:else if !draft || !defaults}
		<div class="px-5 py-6 text-[12px] text-[var(--color-fail)]">{error ?? 'no data'}</div>
	{:else}
		<div class="flex-1 overflow-auto px-5 py-4 text-[12px] space-y-6">

			<!-- Globals -->
			<section>
				<h2 class="mb-2 text-[10px] uppercase tracking-[0.16em] text-[var(--color-muted)]">Global tolerances</h2>
				<div class="grid grid-cols-2 gap-x-6 gap-y-2 max-w-2xl">
					{#each ['hysteresis','cadence_tol','step_count_tol','silent_fail_default'] as k}
						<label class="flex items-center justify-between gap-3">
							<span class="num text-[var(--color-default)]">{k}</span>
							<input
								type="number"
								step="any"
								value={draft.globals[k as keyof Globals] ?? ''}
								oninput={(e) => (draft!.globals[k as keyof Globals] = parseNum((e.target as HTMLInputElement).value) as any)}
								placeholder={String(defaultGlobal(k as keyof Globals))}
								class="w-32 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11.5px] num text-[var(--color-bright)] text-right"
							/>
						</label>
					{/each}
				</div>
			</section>

			<!-- Products -->
			<section>
				<h2 class="mb-2 text-[10px] uppercase tracking-[0.16em] text-[var(--color-muted)]">Products (L1 freshness · cadence · step count · size)</h2>
				<div class="overflow-auto rounded-sm border border-[var(--color-border)]">
					<table class="w-full text-[11px] num">
						<thead class="bg-[var(--color-elevated)]/40 text-[var(--color-muted)] uppercase tracking-wider">
							<tr>
								<th class="px-3 py-1 text-left">product_id</th>
								<th class="px-3 py-1 text-right">max_freshness_s</th>
								<th class="px-3 py-1 text-right">min_png_bytes</th>
								<th class="px-3 py-1 text-right">expected_steps</th>
								<th class="px-3 py-1 text-right">cadence_s</th>
							</tr>
						</thead>
						<tbody>
							{#each Object.keys(defaults.products).sort() as pid}
								{@const cur = draft.products[pid] ?? (draft.products[pid] = {})}
								<tr class="border-t border-[var(--color-border)]/40">
									<td class="px-3 py-1 text-[var(--color-bright)]">{pid}</td>
									{#each ['max_freshness_s','min_png_bytes','expected_steps','cadence_s'] as k}
										<td class="px-3 py-1 text-right">
											<input
												type="number"
												step="any"
												value={cur[k as keyof ProductOverride] ?? ''}
												oninput={(e) => (cur[k as keyof ProductOverride] = parseNum((e.target as HTMLInputElement).value) as any)}
												placeholder={String(defaultProduct(pid, k as keyof ProductOverride) ?? '—')}
												class="w-28 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)] text-right"
											/>
										</td>
									{/each}
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</section>

			<!-- Radars -->
			<section>
				<h2 class="mb-2 text-[10px] uppercase tracking-[0.16em] text-[var(--color-muted)]">Radars (L2 ghost-up threshold)</h2>
				<div class="overflow-auto rounded-sm border border-[var(--color-border)] max-w-md">
					<table class="w-full text-[11px] num">
						<thead class="bg-[var(--color-elevated)]/40 text-[var(--color-muted)] uppercase tracking-wider">
							<tr>
								<th class="px-3 py-1 text-left">radar_id</th>
								<th class="px-3 py-1 text-right">silent_fail_s</th>
							</tr>
						</thead>
						<tbody>
							{#each Object.keys(defaults.radars).sort() as rid}
								{@const cur = draft.radars[rid] ?? (draft.radars[rid] = {})}
								<tr class="border-t border-[var(--color-border)]/40">
									<td class="px-3 py-1 text-[var(--color-bright)]">{rid}</td>
									<td class="px-3 py-1 text-right">
										<input
											type="number"
											step="any"
											value={cur.silent_fail_s ?? ''}
											oninput={(e) => (cur.silent_fail_s = parseNum((e.target as HTMLInputElement).value))}
											placeholder={String(defaultRadar(rid, 'silent_fail_s') ?? '—')}
											class="w-28 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)] text-right"
										/>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</section>

			<!-- L4 Image QC -->
			<section>
				<h2 class="mb-2 text-[10px] uppercase tracking-[0.16em] text-[var(--color-muted)]">Image QC (L4) — per-product profiles</h2>
				<div class="overflow-auto rounded-sm border border-[var(--color-border)]">
					<table class="w-full text-[11px] num">
						<thead class="bg-[var(--color-elevated)]/40 text-[var(--color-muted)] uppercase tracking-wider">
							<tr>
								<th class="px-3 py-1 text-left">identifier</th>
								<th class="px-3 py-1 text-right">extreme_threshold</th>
								<th class="px-3 py-1 text-right">frozen_min_cov_pct</th>
								<th class="px-3 py-1 text-center">skip_frozen</th>
								<th class="px-3 py-1 text-center">skip_range_ring</th>
							</tr>
						</thead>
						<tbody>
							{#each Object.keys(defaults.l4).sort() as pid}
								{@const cur = draft.l4[pid] ?? (draft.l4[pid] = {})}
								<tr class="border-t border-[var(--color-border)]/40">
									<td class="px-3 py-1 text-[var(--color-bright)]">{pid}</td>
									<td class="px-3 py-1 text-right">
										<input
											type="number"
											step="any"
											value={cur.extreme_threshold ?? ''}
											oninput={(e) => (cur.extreme_threshold = parseNum((e.target as HTMLInputElement).value))}
											placeholder={String(defaultL4(pid, 'extreme_threshold') ?? '—')}
											class="w-24 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)] text-right"
										/>
									</td>
									<td class="px-3 py-1 text-right">
										<input
											type="number"
											step="any"
											value={cur.frozen_min_cov_pct ?? ''}
											oninput={(e) => (cur.frozen_min_cov_pct = parseNum((e.target as HTMLInputElement).value))}
											placeholder={String(defaultL4(pid, 'frozen_min_cov_pct') ?? '—')}
											class="w-24 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)] text-right"
										/>
									</td>
									<td class="px-3 py-1 text-center">
										<input
											type="checkbox"
											checked={!!cur.skip_frozen}
											onchange={(e) => (cur.skip_frozen = (e.target as HTMLInputElement).checked)}
											class="accent-[var(--color-ok)]"
										/>
									</td>
									<td class="px-3 py-1 text-center">
										<input
											type="checkbox"
											checked={!!cur.skip_range_ring}
											onchange={(e) => (cur.skip_range_ring = (e.target as HTMLInputElement).checked)}
											class="accent-[var(--color-ok)]"
										/>
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</section>
			<!-- Retroactive reprocess -->
			<section class="rounded-sm border border-[var(--color-warn)]/30 bg-[var(--color-warn)]/5 p-4">
				<h2 class="mb-2 text-[10px] uppercase tracking-[0.16em] text-[var(--color-warn)]">Retroactive reprocess</h2>
				<p class="mb-3 text-[11px] text-[var(--color-default)] leading-relaxed">
					Re-classifies historical <span class="num">check_runs</span> rows in the chosen window
					under the <em>currently saved</em> thresholds. Re-running with no threshold change is a
					no-op. Rows with <span class="num">reason=local_dns_error</span> or
					<span class="num">transport_error</span> are preserved.
				</p>
				<div class="flex flex-wrap items-center gap-4 mb-3">
					<label class="flex items-center gap-2">
						<span class="text-[10.5px] uppercase tracking-wider text-[var(--color-muted)]">since (UTC)</span>
						<input type="datetime-local" bind:value={reSince}
							class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11px] num text-[var(--color-bright)]" />
					</label>
					<label class="flex items-center gap-2">
						<span class="text-[10.5px] uppercase tracking-wider text-[var(--color-muted)]">until (UTC)</span>
						<input type="datetime-local" bind:value={reUntil}
							class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11px] num text-[var(--color-bright)]" />
					</label>
					<div class="flex items-center gap-3">
						<span class="text-[10.5px] uppercase tracking-wider text-[var(--color-muted)]">stages</span>
						{#each [['L1','Product Freshness'],['L2','Radar Scans'],['L4-T1T2','Image Quality']] as [k, lbl]}
							<label class="flex items-center gap-1 text-[11px]">
								<input type="checkbox"
									checked={reStages[k]}
									onchange={(e) => (reStages[k] = (e.target as HTMLInputElement).checked)}
									class="accent-[var(--color-ok)]" />
								<span class="text-[var(--color-default)]">{lbl}</span>
							</label>
						{/each}
					</div>
				</div>

				{#if reJob && reJob.state === 'running'}
					{@const pct = reJob.n_total ? Math.floor((reJob.n_evaluated / reJob.n_total) * 100) : 0}
					<div class="flex items-center gap-3">
						<div class="num text-[11.5px] text-[var(--color-bright)]">running…</div>
						<div class="num text-[11px] text-[var(--color-muted)]">
							{reJob.n_evaluated}/{reJob.n_total} ({pct}%) · changed {reJob.n_changed} · preserved {reJob.n_preserved}
						</div>
						<button class="ml-auto border border-[var(--color-fail)]/50 px-3 py-1 text-[10.5px] uppercase tracking-wider text-[var(--color-fail)] hover:bg-[var(--color-fail)]/10" onclick={cancelReprocess}>cancel</button>
					</div>
					<div class="mt-2 h-1.5 rounded-sm bg-[var(--color-elevated)]">
						<div class="h-full rounded-sm bg-[var(--color-ok)]" style="width: {pct}%"></div>
					</div>
				{:else if reJob && ['completed','cancelled','error'].includes(reJob.state)}
					<div class="text-[11.5px] num {reJob.state === 'error' ? 'text-[var(--color-fail)]' : 'text-[var(--color-ok)]'}">
						{reJob.state}: {reJob.n_evaluated} evaluated · {reJob.n_changed} changed · {reJob.n_preserved} preserved
						{#if reJob.error}<div class="text-[var(--color-fail)] mt-1">{reJob.error}</div>{/if}
					</div>
					<button class="mt-2 text-[10.5px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)] underline" onclick={() => (reJob = null)}>
						start another
					</button>
				{:else}
					<div class="flex items-center gap-2">
						<input
							type="text"
							placeholder="type APPLY to confirm"
							bind:value={reConfirmText}
							class="w-48 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11px] num text-[var(--color-bright)]"
						/>
						<button
							type="button"
							class="border border-[var(--color-warn)] bg-[var(--color-warn)]/15 px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-warn)]/25 disabled:opacity-50"
							onclick={startReprocess}
							disabled={reConfirmText !== 'APPLY'}
						>
							Start retroactive reprocess
						</button>
						{#if reError}<span class="text-[var(--color-fail)] text-[11px]">{reError}</span>{/if}
					</div>
				{/if}
			</section>
		</div>

		<!-- Footer actions -->
		<footer class="flex items-center justify-between gap-3 border-t border-[var(--color-border)] bg-[var(--color-canvas)]/40 px-5 py-3 text-[11px]">
			<div class="text-[10.5px] text-[var(--color-faint)]">
				{#if error}<span class="text-[var(--color-fail)]">{error}</span>
				{:else if info}<span class="text-[var(--color-ok)]">{info}</span>
				{:else}Live values update on next check tick. Reprocess option appears once you confirm.{/if}
			</div>
			<div class="flex gap-2">
				<button
					type="button"
					class="border border-[var(--color-border-strong)] px-3 py-1 uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]"
					onclick={resetDraft}
				>
					reset
				</button>
				<button
					type="button"
					class="border border-[var(--color-ok)] bg-[var(--color-ok)]/15 px-3 py-1 uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-ok)]/25"
					onclick={openConfirm}
				>
					review & apply
				</button>
			</div>
		</footer>

		<!-- Confirm modal -->
		{#if confirmOpen}
			{@const diff = diffSummary()}
			<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/55 backdrop-blur-sm p-4" onclick={() => (confirmOpen = false)} role="dialog">
				<div class="relative w-full max-w-[640px] rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] shadow-2xl" onclick={(e) => e.stopPropagation()} role="document">
					<header class="flex items-center justify-between border-b border-[var(--color-border)] px-5 py-3">
						<span class="label tracking-[0.16em] text-[var(--color-bright)]">CONFIRM THRESHOLD CHANGES</span>
						<button class="text-[var(--color-muted)] hover:text-[var(--color-bright)]" onclick={() => (confirmOpen = false)}>×</button>
					</header>
					<div class="px-5 py-4 text-[12px] space-y-3">
						{#if diff.length === 0}
							<div class="text-[var(--color-muted)]">No changes to apply.</div>
						{:else}
							<div class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">{diff.length} change{diff.length === 1 ? '' : 's'}</div>
							<ul class="max-h-[40vh] overflow-auto divide-y divide-[var(--color-border)]/60 rounded-sm border border-[var(--color-border)] text-[11.5px] num">
								{#each diff as d}
									<li class="flex items-center gap-3 px-3 py-1.5">
										<span class="flex-1 text-[var(--color-default)]">{d.key}</span>
										<span class="text-[var(--color-muted)]">{d.before === null || d.before === undefined ? '(default)' : String(d.before)}</span>
										<span class="text-[var(--color-faint)]">→</span>
										<span class="text-[var(--color-bright)]">{d.after === null || d.after === undefined ? '(default)' : String(d.after)}</span>
									</li>
								{/each}
							</ul>
							<div class="rounded-sm border border-[var(--color-warn)]/40 bg-[var(--color-warn)]/10 px-3 py-2 text-[11px] text-[var(--color-warn)]">
								These changes affect future evaluations only. To reclassify historical rows, use the Retroactive Reprocess panel after applying.
							</div>
						{/if}
					</div>
					<footer class="flex items-center justify-end gap-2 border-t border-[var(--color-border)] bg-[var(--color-canvas)]/40 px-5 py-3">
						<button class="border border-[var(--color-border-strong)] px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]" onclick={() => (confirmOpen = false)}>
							cancel
						</button>
						<button
							class="border border-[var(--color-ok)] bg-[var(--color-ok)]/20 px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-bright)] disabled:opacity-50"
							onclick={apply}
							disabled={saving || diff.length === 0}
						>
							{saving ? 'saving…' : 'apply'}
						</button>
					</footer>
				</div>
			</div>
		{/if}
	{/if}
</div>
