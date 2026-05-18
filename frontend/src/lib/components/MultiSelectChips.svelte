<script lang="ts">
	/** Multi-select chip widget. Options come in as {value, label}; selection
	 *  is a string[] of values. Also supports free-text addition so the user
	 *  can filter by targets we don't know about ahead of time (older radars,
	 *  custom check IDs, etc.). */
	let {
		label = '',
		options = [],
		selected = $bindable<string[]>([]),
		placeholder = 'Add…',
		allowCustom = false,
		width = 'w-44'
	}: {
		label?: string;
		options: { value: string; label: string; hint?: string }[];
		selected: string[];
		placeholder?: string;
		allowCustom?: boolean;
		width?: string;
	} = $props();

	let open = $state(false);
	let custom = $state('');
	let rootEl: HTMLDivElement | undefined;

	// Sort the option list so selected items float to the top of the dropdown,
	// then alphabetical. The chip strip already shows the user's choices —
	// this just keeps the dropdown in a stable order they can scan.
	const sortedOptions = $derived.by(() => {
		const opts = [...options];
		opts.sort((a, b) => {
			const aSel = selected.includes(a.value) ? 0 : 1;
			const bSel = selected.includes(b.value) ? 0 : 1;
			if (aSel !== bSel) return aSel - bSel;
			return a.label.localeCompare(b.label);
		});
		return opts;
	});

	function toggle(value: string) {
		selected = selected.includes(value)
			? selected.filter((v) => v !== value)
			: [...selected, value];
	}

	function remove(value: string) {
		selected = selected.filter((v) => v !== value);
	}

	function addCustom() {
		const v = custom.trim();
		if (!v) return;
		if (!selected.includes(v)) selected = [...selected, v];
		custom = '';
	}

	function clearAll() {
		selected = [];
	}
	function selectAll() {
		selected = options.map((o) => o.value);
	}

	// Close on outside click.
	function handleDocClick(e: MouseEvent) {
		if (!rootEl) return;
		if (!rootEl.contains(e.target as Node)) open = false;
	}
	$effect(() => {
		if (open) {
			document.addEventListener('mousedown', handleDocClick);
			return () => document.removeEventListener('mousedown', handleDocClick);
		}
	});

	const buttonLabel = $derived.by(() => {
		if (selected.length === 0) return 'any';
		if (selected.length === 1) {
			const o = options.find((x) => x.value === selected[0]);
			return o?.label ?? selected[0];
		}
		return `${selected.length} selected`;
	});
</script>

<div bind:this={rootEl} class="relative flex items-center gap-1 text-[11px]">
	{#if label}
		<span class="text-[var(--color-muted)] uppercase tracking-wider">{label}</span>
	{/if}
	<button
		type="button"
		onclick={() => (open = !open)}
		class="flex items-center gap-1 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11px] text-[var(--color-default)] hover:bg-[var(--color-elevated)]/40 {width}"
	>
		<span class="num truncate {selected.length ? 'text-[var(--color-bright)]' : 'text-[var(--color-muted)]'}">
			{buttonLabel}
		</span>
		<svg viewBox="0 0 10 10" width="9" height="9" class="ml-auto text-[var(--color-muted)]" fill="currentColor">
			<polygon points="2,3 8,3 5,7" />
		</svg>
	</button>

	{#if open}
		<div
			class="absolute left-0 top-full z-40 mt-1 max-h-[60vh] w-[260px] overflow-y-auto rounded-sm border border-[var(--color-border-strong)] bg-[var(--color-surface)] shadow-xl"
		>
			<div class="flex items-center justify-between border-b border-[var(--color-border)] px-3 py-1.5 text-[10px] uppercase tracking-[0.16em] text-[var(--color-muted)]">
				<span>{selected.length} of {options.length} selected</span>
				<span class="flex items-center gap-2">
					<button class="text-[var(--color-faint)] hover:text-[var(--color-bright)]" onclick={selectAll}>all</button>
					{#if selected.length > 0}
						<button class="text-[var(--color-faint)] hover:text-[var(--color-fail)]" onclick={clearAll}>clear</button>
					{/if}
				</span>
			</div>
			<ul class="divide-y divide-[var(--color-border)]/60">
				{#each sortedOptions as opt}
					{@const checked = selected.includes(opt.value)}
					<li>
						<button
							type="button"
							class="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[11px] hover:bg-[var(--color-elevated)]/40"
							onclick={() => toggle(opt.value)}
						>
							<span
								class="inline-flex h-3 w-3 items-center justify-center rounded-[2px] border {checked
									? 'border-[var(--color-ok)] bg-[var(--color-ok)]/20 text-[var(--color-ok)]'
									: 'border-[var(--color-border-strong)] text-transparent'}"
							>
								<svg viewBox="0 0 10 10" width="8" height="8" fill="none" stroke="currentColor" stroke-width="2">
									<polyline points="2,5 4,7 8,3" />
								</svg>
							</span>
							<span class="num flex-1 text-[var(--color-bright)]">{opt.label}</span>
							{#if opt.hint}
								<span class="text-[9.5px] text-[var(--color-faint)]">{opt.hint}</span>
							{/if}
						</button>
					</li>
				{/each}
			</ul>
			{#if allowCustom}
				<div class="border-t border-[var(--color-border)] px-3 py-1.5">
					<form
						onsubmit={(e) => {
							e.preventDefault();
							addCustom();
						}}
						class="flex items-center gap-1"
					>
						<input
							type="text"
							bind:value={custom}
							{placeholder}
							class="flex-1 border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[11px] text-[var(--color-bright)]"
						/>
						<button
							type="submit"
							class="border border-[var(--color-border-strong)] px-2 py-1 text-[10px] uppercase tracking-wider text-[var(--color-default)] hover:bg-[var(--color-elevated)]"
						>
							add
						</button>
					</form>
				</div>
			{/if}
		</div>
	{/if}

	{#if selected.length > 0}
		<div class="flex flex-wrap items-center gap-1">
			{#each selected as v}
				{@const o = options.find((x) => x.value === v)}
				<span
					class="inline-flex items-center gap-1 rounded-sm border border-[var(--color-border-strong)] bg-[var(--color-elevated)]/50 px-1.5 py-0.5 text-[10px] num text-[var(--color-bright)]"
				>
					{o?.label ?? v}
					<button
						type="button"
						class="text-[var(--color-muted)] hover:text-[var(--color-fail)]"
						aria-label="remove"
						onclick={() => remove(v)}
					>
						×
					</button>
				</span>
			{/each}
		</div>
	{/if}
</div>
