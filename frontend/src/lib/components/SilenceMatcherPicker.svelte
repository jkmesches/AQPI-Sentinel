<script lang="ts">
	/** Silence matcher picker — replaces the raw JSON input on /admin/silences.
	 *
	 *  Surfaces four common presets (All / All Radars / All Products / All
	 *  Website) plus a "Custom" panel that exposes the underlying matcher keys
	 *  (check_id / target / stage / severity / status_at_open) as fields.
	 *
	 *  Empty matchers ({}) = "match every alarm" which is the All preset.
	 *  Stage-only matchers narrow to one layer; the descriptor labels follow
	 *  the canonical map (Connectivity / Product Freshness / etc.) via
	 *  stageLabel. Custom is a power-user escape hatch — laypeople pick from
	 *  the presets, advanced users edit the keys.
	 */
	import { stageLabel } from '$lib/format';

	let {
		value = $bindable<Record<string, string>>({}),
		summaryWidth = 'w-72'
	}: {
		value: Record<string, string>;
		summaryWidth?: string;
	} = $props();

	type PresetKey = 'all' | 'all_radars' | 'all_products' | 'all_website' | 'custom';
	const PRESETS: { key: PresetKey; label: string; description: string; matchers: Record<string, string> }[] = [
		{ key: 'all',          label: 'All alarms',
		  description: 'Match every alarm regardless of stage/check/target.',
		  matchers: {} },
		{ key: 'all_radars',   label: 'All radars',
		  description: 'Match anything in the Radar Scans stage (L2).',
		  matchers: { stage: 'L2' } },
		{ key: 'all_products', label: 'All products',
		  description: 'Match anything in the Product Freshness stage (L1).',
		  matchers: { stage: 'L1' } },
		{ key: 'all_website',  label: 'All website',
		  description: 'Match anything in the Connectivity stage (L0).',
		  matchers: { stage: 'L0' } },
		{ key: 'custom',       label: 'Custom matchers',
		  description: 'Match by specific check_id, target, stage, or severity.',
		  matchers: {} }
	];

	function presetForValue(v: Record<string, string>): PresetKey {
		const keys = Object.keys(v);
		if (keys.length === 0) return 'all';
		if (keys.length === 1 && v.stage === 'L2') return 'all_radars';
		if (keys.length === 1 && v.stage === 'L1') return 'all_products';
		if (keys.length === 1 && v.stage === 'L0') return 'all_website';
		return 'custom';
	}

	let open = $state(false);
	// Tracks "user has explicitly clicked Custom" so the inline form shows
	// even when `value === {}`. Without this the previous version derived
	// currentPreset from `value` alone, which meant clicking Custom on a
	// fresh silence (value={}) re-rendered as the All preset and the form
	// never appeared (review feedback 2026-05-18).
	let customStickyOpen = $state(false);
	const currentPreset = $derived<PresetKey>(
		customStickyOpen ? 'custom' : presetForValue(value)
	);

	// Inner fields for the Custom panel; persisted only while the popup is
	// open. On selection of Custom they sync from `value` so the user can
	// extend an existing preset rather than start blank.
	let cm_stage   = $state(value.stage   ?? '');
	let cm_target  = $state(value.target  ?? '');
	let cm_check   = $state(value.check_id ?? '');
	let cm_sev     = $state(value.severity ?? '');
	let cm_status  = $state(value.status_at_open ?? '');

	$effect(() => {
		if (open) {
			cm_stage   = value.stage   ?? '';
			cm_target  = value.target  ?? '';
			cm_check   = value.check_id ?? '';
			cm_sev     = value.severity ?? '';
			cm_status  = value.status_at_open ?? '';
			// If the bound value already looks custom (mixed/unknown keys),
			// prime the sticky flag so the form is visible on open.
			if (presetForValue(value) === 'custom') customStickyOpen = true;
		} else {
			customStickyOpen = false;
		}
	});

	function applyPreset(p: PresetKey) {
		const preset = PRESETS.find((x) => x.key === p)!;
		if (p !== 'custom') {
			customStickyOpen = false;
			value = { ...preset.matchers };
			open = false;
			return;
		}
		// Custom: reveal the inline form and keep popup open.
		customStickyOpen = true;
	}

	function applyCustom() {
		const out: Record<string, string> = {};
		if (cm_stage)  out.stage          = cm_stage;
		if (cm_target) out.target         = cm_target;
		if (cm_check)  out.check_id       = cm_check;
		if (cm_sev)    out.severity       = cm_sev;
		if (cm_status) out.status_at_open = cm_status;
		value = out;
		open = false;
	}

	function summary(): string {
		const p = currentPreset;
		const preset = PRESETS.find((x) => x.key === p);
		if (p !== 'custom' && preset) return preset.label;
		const parts = Object.entries(value).map(([k, v]) => `${k}=${v}`);
		return parts.length === 0 ? 'No matchers (matches all)' : parts.join(' · ');
	}

	let rootEl: HTMLDivElement | undefined;
	function handleDocClick(e: MouseEvent) {
		if (rootEl && !rootEl.contains(e.target as Node)) open = false;
	}
	$effect(() => {
		if (open) {
			document.addEventListener('mousedown', handleDocClick);
			return () => document.removeEventListener('mousedown', handleDocClick);
		}
	});

	const STAGE_OPTIONS = ['L0', 'L1', 'L2', 'L3', 'L4-T1T2'];
</script>

<div bind:this={rootEl} class="relative inline-block text-[11.5px]">
	<button
		type="button"
		onclick={() => (open = !open)}
		class="flex items-center justify-between gap-2 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-left num text-[var(--color-bright)] hover:bg-[var(--color-elevated)]/40 {summaryWidth}"
		aria-haspopup="true"
		aria-expanded={open}
	>
		<span class="truncate">{summary()}</span>
		<svg viewBox="0 0 10 10" width="9" height="9" class="text-[var(--color-muted)] shrink-0" fill="currentColor">
			<polygon points="2,3 8,3 5,7" />
		</svg>
	</button>

	{#if open}
		<div
			class="absolute left-0 top-full z-40 mt-1 w-[360px] overflow-hidden rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] shadow-2xl"
			role="dialog"
		>
			<header class="border-b border-[var(--color-border)] px-3 py-2 text-[10px] uppercase tracking-[0.16em] text-[var(--color-muted)]">
				Choose matcher
			</header>
			<ul class="divide-y divide-[var(--color-border)]/60">
				{#each PRESETS as p}
					<li>
						<button
							type="button"
							class="flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-[var(--color-elevated)]/40"
							onclick={() => applyPreset(p.key)}
						>
							<span class="inline-flex h-3 w-3 mt-0.5 items-center justify-center rounded-full border {currentPreset === p.key ? 'border-[var(--color-ok)] bg-[var(--color-ok)]' : 'border-[var(--color-border-strong)]'}">
							</span>
							<span class="flex-1 min-w-0">
								<div class="text-[12px] font-medium text-[var(--color-bright)]">{p.label}</div>
								<div class="text-[10.5px] text-[var(--color-faint)] leading-snug">{p.description}</div>
							</span>
						</button>
					</li>
				{/each}
			</ul>

			{#if currentPreset === 'custom'}
				<div class="border-t border-[var(--color-border)] bg-[var(--color-canvas)]/60 px-3 py-2 space-y-1.5 text-[11px]">
					<div class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] mb-1">Custom matchers</div>
					<label class="flex items-center gap-2">
						<span class="w-16 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">stage</span>
						<select bind:value={cm_stage} class="flex-1 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)]">
							<option value="">(any)</option>
							{#each STAGE_OPTIONS as s}
								<option value={s}>{stageLabel(s)} ({s})</option>
							{/each}
						</select>
					</label>
					<label class="flex items-center gap-2">
						<span class="w-16 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">target</span>
						<input type="text" bind:value={cm_target} placeholder="XSCV / qpe_15min / …" class="flex-1 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)]" />
					</label>
					<label class="flex items-center gap-2">
						<span class="w-16 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">check_id</span>
						<input type="text" bind:value={cm_check} placeholder="layer2.radar.XEBY / …" class="flex-1 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)]" />
					</label>
					<label class="flex items-center gap-2">
						<span class="w-16 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">severity</span>
						<select bind:value={cm_sev} class="flex-1 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)]">
							<option value="">(any)</option>
							<option value="info">info</option>
							<option value="warn">warn</option>
							<option value="critical">critical</option>
						</select>
					</label>
					<div class="flex items-center justify-end gap-2 pt-1">
						<button type="button" class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]" onclick={() => (open = false)}>cancel</button>
						<button type="button" class="border border-[var(--color-ok)] bg-[var(--color-ok)]/15 px-2 py-0.5 text-[10.5px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-ok)]/25" onclick={applyCustom}>apply</button>
					</div>
				</div>
			{/if}
		</div>
	{/if}
</div>
