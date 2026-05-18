<script lang="ts">
	/** Report export popup. User picks date range, resolution, stages, targets;
	 *  modal triggers a CSV download from /api/history/report.csv.
	 *
	 *  Defaults come in via props so callers can prefill from the on-screen
	 *  Timeline view — "what I'm looking at" is almost always what users want
	 *  to export, so making them rebuild the filter set from scratch is wasted
	 *  motion. */
	import MultiSelectChips from './MultiSelectChips.svelte';
	import { url as apiUrl } from '$lib/origin';
	import { stageLabel } from '$lib/format';

	let {
		open = $bindable<boolean>(false),
		defaultSince = '',
		defaultUntil = '',
		defaultBucket = '5m',
		defaultStages = $bindable<string[]>([]),
		defaultTargets = $bindable<string[]>([]),
		targetOptions = []
	}: {
		open: boolean;
		defaultSince?: string;
		defaultUntil?: string;
		defaultBucket?: string;
		defaultStages?: string[];
		defaultTargets?: string[];
		targetOptions?: { value: string; label: string; hint?: string }[];
	} = $props();

	let since = $state(defaultSince);
	let until = $state(defaultUntil);
	let bucket = $state(defaultBucket);
	let stages = $state<string[]>([...defaultStages]);
	let targets = $state<string[]>([...defaultTargets]);
	// Tracks whether the user has manually edited the target set. While
	// untouched, the default = "every target in the currently selected
	// stages" — automatically re-derived on every stage change so the
	// "I picked Radars, I want all radars" flow doesn't require manual
	// re-selection (review feedback 2026-05-18).
	let targetsTouched = $state(false);

	// Refresh local state when the modal is re-opened with new defaults.
	$effect(() => {
		if (open) {
			since   = defaultSince;
			until   = defaultUntil;
			bucket  = defaultBucket;
			stages  = [...defaultStages];
			targets = [...defaultTargets];
			targetsTouched = false;
		}
	});

	// When stages change AND the user hasn't manually picked targets,
	// auto-populate targets with every option matching the selected stages.
	// `targetOptions` items carry `hint = stageLabel(stageId)` from the
	// caller — we match on that to keep this module data-format-agnostic.
	$effect(() => {
		if (!open || targetsTouched) return;
		if (stages.length === 0) {
			targets = targetOptions.map((o) => o.value);
		} else {
			const stageDescriptors = new Set(stages.map((s) => stageDescriptor(s)));
			targets = targetOptions
				.filter((o) => !o.hint || stageDescriptors.has(o.hint))
				.map((o) => o.value);
		}
	});
	// Mirror the canonical stage→descriptor map. Duplicated here so the
	// modal doesn't need the caller to pre-stamp options with stage IDs.
	function stageDescriptor(s: string): string {
		return ({
			L0: 'Connectivity', L1: 'Product Freshness', L2: 'Radar Scans',
			L3: 'Map Overlays', 'L4-T1T2': 'Image Quality'
		} as Record<string, string>)[s] ?? s;
	}
	function markTargetsTouched() { targetsTouched = true; }

	const STAGE_OPTIONS = [
		{ value: 'L0',      label: 'Connectivity',      hint: 'L0' },
		{ value: 'L1',      label: 'Product Freshness', hint: 'L1' },
		{ value: 'L2',      label: 'Radar Scans',       hint: 'L2' },
		{ value: 'L3',      label: 'Map Overlays',      hint: 'L3' },
		{ value: 'L4-T1T2', label: 'Image Quality',     hint: 'L4' }
	];
	const BUCKET_OPTIONS = [
		{ value: '1m',  label: '1 minute'  },
		{ value: '5m',  label: '5 minutes' },
		{ value: '15m', label: '15 minutes' },
		{ value: '1h',  label: '1 hour'   },
		{ value: '6h',  label: '6 hours'  },
		{ value: '1d',  label: '1 day'    }
	];

	function localToIso(s: string): string {
		// datetime-local inputs have no zone — treat as UTC to match the
		// timeline page's convention. The dialog labels make this explicit.
		return new Date(s + 'Z').toISOString();
	}

	// Quick estimate of bucket count to warn the user before they request
	// 50,000 rows. Server cap is 90 days; we surface the projected row count
	// per stage/target combo so they can pick coarser resolution if needed.
	const BUCKET_S: Record<string, number> = {
		'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '6h': 21600, '1d': 86400
	};
	const estBuckets = $derived.by(() => {
		if (!since || !until) return 0;
		try {
			const dur = (new Date(until + 'Z').getTime() - new Date(since + 'Z').getTime()) / 1000;
			if (dur <= 0) return 0;
			return Math.floor(dur / (BUCKET_S[bucket] ?? 300));
		} catch {
			return 0;
		}
	});

	function close() {
		open = false;
	}

	function download() {
		const params = new URLSearchParams({
			since:  localToIso(since),
			until:  localToIso(until),
			bucket
		});
		if (stages.length)  params.set('stage',  stages.join(','));
		if (targets.length) params.set('target', targets.join(','));
		const href = apiUrl(`/api/history/report.csv?${params}`);
		window.open(href, '_blank');
		close();
	}

	function fmt(s: string): string {
		// "YYYY-MM-DDTHH:MM" → "YYYY-MM-DD HH:MM UTC" for the helper hint.
		if (!s) return '—';
		return s.replace('T', ' ') + ' UTC';
	}
</script>

{#if open}
	<!-- Backdrop -->
	<div
		class="fixed inset-0 z-50 flex items-center justify-center bg-black/55 backdrop-blur-sm p-4"
		onclick={close}
		role="dialog"
		aria-modal="true"
		aria-labelledby="report-export-title"
	>
		<!-- Card (stop propagation so clicking inside doesn't close) -->
		<div
			class="relative w-full max-w-[640px] rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] shadow-2xl"
			onclick={(e) => e.stopPropagation()}
			role="document"
		>
			<header class="flex items-center justify-between border-b border-[var(--color-border)] px-5 py-3">
				<div class="flex items-baseline gap-3">
					<span id="report-export-title" class="label tracking-[0.16em] text-[var(--color-bright)]">EXPORT REPORT</span>
					<span class="text-[10.5px] text-[var(--color-faint)]">build & download a bucketed CSV</span>
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

			<div class="space-y-4 px-5 py-4 text-[12px]">
				<!-- Date range -->
				<section>
					<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Time range (UTC)</div>
					<div class="flex flex-wrap items-center gap-3">
						<label class="flex items-center gap-2">
							<span class="w-14 text-[var(--color-muted)] uppercase tracking-wider text-[10.5px]">since</span>
							<input
								type="datetime-local"
								bind:value={since}
								class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[11.5px] num text-[var(--color-bright)]"
							/>
						</label>
						<label class="flex items-center gap-2">
							<span class="w-14 text-[var(--color-muted)] uppercase tracking-wider text-[10.5px]">until</span>
							<input
								type="datetime-local"
								bind:value={until}
								class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[11.5px] num text-[var(--color-bright)]"
							/>
						</label>
					</div>
					<div class="mt-1 text-[10.5px] text-[var(--color-faint)] num">
						{fmt(since)} → {fmt(until)}
					</div>
				</section>

				<!-- Resolution -->
				<section>
					<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Resolution</div>
					<div class="flex flex-wrap gap-1">
						{#each BUCKET_OPTIONS as opt}
							<button
								type="button"
								onclick={() => (bucket = opt.value)}
								class="rounded-sm border px-2 py-1 text-[11px] num transition-colors {bucket === opt.value
									? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
									: 'border-[var(--color-border-strong)] text-[var(--color-muted)] hover:bg-[var(--color-elevated)]/40 hover:text-[var(--color-default)]'}"
							>
								{opt.label}
							</button>
						{/each}
					</div>
					{#if estBuckets > 0}
						<div class="mt-1 text-[10.5px] text-[var(--color-faint)] num">
							≈ {estBuckets.toLocaleString()} buckets × selected checks
							{#if estBuckets > 2000}
								<span class="ml-1 text-[var(--color-warn)]">(consider coarser resolution)</span>
							{/if}
						</div>
					{/if}
				</section>

				<!-- Stage multi-select -->
				<section>
					<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Stages</div>
					<MultiSelectChips
						label=""
						options={STAGE_OPTIONS}
						bind:selected={stages}
						width="w-56"
					/>
					<div class="mt-1 text-[10.5px] text-[var(--color-faint)]">
						{stages.length === 0 ? 'All stages will be included.' : `${stages.length} stage${stages.length === 1 ? '' : 's'} selected.`}
					</div>
				</section>

				<!-- Target multi-select -->
				<section>
					<div class="mb-1 flex items-center justify-between text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">
						<span>Targets / products</span>
						{#if targetsTouched}
							<button
								type="button"
								class="text-[var(--color-faint)] hover:text-[var(--color-bright)]"
								onclick={() => { targetsTouched = false; }}
							>
								reset to stage defaults
							</button>
						{/if}
					</div>
					<div onclick={markTargetsTouched} role="presentation">
						<MultiSelectChips
							label=""
							options={targetOptions}
							bind:selected={targets}
							allowCustom={true}
							placeholder="XSCV / custom…"
							width="w-56"
						/>
					</div>
					<div class="mt-1 text-[10.5px] text-[var(--color-faint)]">
						{#if !targetsTouched}
							Auto-populated from your stage selection. Click any chip / dropdown action to override.
						{:else if targets.length === 0}
							All targets will be included.
						{:else}
							{targets.length} target{targets.length === 1 ? '' : 's'} selected.
						{/if}
					</div>
				</section>
			</div>

			<!-- Actions -->
			<footer class="flex items-center justify-between border-t border-[var(--color-border)] bg-[var(--color-canvas)]/40 px-5 py-3">
				<div class="text-[10.5px] text-[var(--color-faint)]">
					CSV: bucket_ts, stage, check_id, target, status, n_runs
				</div>
				<div class="flex gap-2">
					<button
						type="button"
						class="border border-[var(--color-border-strong)] px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)] hover:bg-[var(--color-elevated)]/40"
						onclick={close}
					>
						cancel
					</button>
					<button
						type="button"
						class="border border-[var(--color-ok)] bg-[var(--color-ok)]/15 px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-ok)]/25"
						onclick={download}
					>
						download CSV
					</button>
				</div>
			</footer>
		</div>
	</div>
{/if}
