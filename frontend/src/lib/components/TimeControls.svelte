<script lang="ts">
	/** Time controls strip — full-width band beneath the map. */
	let {
		stepIdx = 0,
		totalSteps = 0,
		stepLabel = '',
		playing = false,
		activity = [],
		onFirst,
		onPrev,
		onTogglePlay,
		onNext,
		onLast,
		onScrub
	}: {
		stepIdx?: number;
		totalSteps?: number;
		stepLabel?: string;
		playing?: boolean;
		activity?: number[];
		onFirst?: () => void;
		onPrev?: () => void;
		onTogglePlay?: () => void;
		onNext?: () => void;
		onLast?: () => void;
		onScrub?: (i: number) => void;
	} = $props();

	const disabled = $derived(totalSteps === 0);
	const maxAct = $derived(activity.length ? Math.max(0.005, ...activity) : 0.005);

	// split the label into time + date for two-line display.
	const labelParts = $derived.by(() => {
		// expected "SAT 16-MAY-2026  02:00:00"
		const m = stepLabel?.match(/^(\S+)\s+(\S+)\s+(\S+)/);
		if (m) return { day: m[1], date: m[2], time: m[3] };
		return { day: '', date: '', time: stepLabel || '' };
	});

	const stepBtn =
		'inline-flex h-7 w-7 items-center justify-center text-[var(--color-default)] hover:text-[var(--color-bright)] disabled:opacity-30 disabled:hover:text-[var(--color-default)] transition-colors';
</script>

<div
	class="flex items-center gap-4 border-t border-[var(--color-border)] bg-[var(--color-surface)]/95 px-4 py-2 backdrop-blur"
>
	<!-- Timestamp readout: time prominent, date subdued -->
	<div class="flex min-w-[10rem] flex-col leading-tight">
		<span class="num text-[18px] tracking-wide text-[var(--color-bright)]">
			{labelParts.time || '—:—:—'}
		</span>
		<span class="num text-[10px] text-[var(--color-muted)]">
			{labelParts.day}
			{labelParts.date}
		</span>
	</div>

	<!-- transport -->
	<div class="flex items-center gap-0.5">
		<button class={stepBtn} onclick={onFirst} title="first frame" {disabled} aria-label="first">
			<svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor">
				<rect x="2" y="3" width="1.5" height="10" />
				<polygon points="5,8 13,3 13,13" />
			</svg>
		</button>
		<button class={stepBtn} onclick={onPrev} title="step back" {disabled} aria-label="step back">
			<svg viewBox="0 0 16 16" width="11" height="11" fill="currentColor">
				<polygon points="4,8 13,3 13,13" />
			</svg>
		</button>
		<button
			class="inline-flex h-10 w-10 items-center justify-center rounded-full border transition-colors disabled:opacity-30 {playing
				? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-ok)]'
				: 'border-[var(--color-border-strong)] text-[var(--color-bright)] hover:bg-[var(--color-elevated)]'}"
			onclick={onTogglePlay}
			title={playing ? 'pause' : 'play'}
			{disabled}
			aria-label={playing ? 'pause' : 'play'}
		>
			{#if playing}
				<svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor">
					<rect x="4" y="3" width="3" height="10" />
					<rect x="9" y="3" width="3" height="10" />
				</svg>
			{:else}
				<svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor">
					<polygon points="4,3 13,8 4,13" />
				</svg>
			{/if}
		</button>
		<button class={stepBtn} onclick={onNext} title="step forward" {disabled} aria-label="step forward">
			<svg viewBox="0 0 16 16" width="11" height="11" fill="currentColor">
				<polygon points="3,3 12,8 3,13" />
			</svg>
		</button>
		<button class={stepBtn} onclick={onLast} title="latest" {disabled} aria-label="latest">
			<svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor">
				<polygon points="3,3 11,8 3,13" />
				<rect x="12.5" y="3" width="1.5" height="10" />
			</svg>
		</button>
	</div>

	<!-- timeline -->
	<div class="flex flex-1 flex-col gap-1">
		<!-- activity bars: one per step, height = non-empty pixel fraction normalized -->
		<div class="flex h-3 items-end gap-px overflow-hidden" title="weather activity per step (auto-scaled)">
			{#if activity.length && activity.length === totalSteps}
				{#each activity as a, i}
					<div
						class="flex-1 transition-colors {i === stepIdx
							? 'bg-[var(--color-ok)]'
							: i < stepIdx
								? 'bg-[var(--color-ok)]/45'
								: 'bg-[var(--color-faint)]/45'}"
						style="height: {Math.max(8, (a / maxAct) * 100)}%"
					></div>
				{/each}
			{:else if totalSteps > 0}
				<div class="flex-1 self-center text-center text-[9px] text-[var(--color-faint)]">
					measuring activity…
				</div>
			{/if}
		</div>
		<div class="relative h-2">
			<!-- track -->
			<div class="absolute inset-0 rounded-full bg-[var(--color-border)]"></div>
			<!-- ticks -->
			{#if totalSteps > 1}
				<div class="absolute inset-0 flex items-center">
					{#each Array(totalSteps) as _, i}
						<span
							class="absolute h-[6px] w-px {i <= stepIdx
								? 'bg-[var(--color-ok)]'
								: 'bg-[var(--color-faint)]'} top-1/2 -translate-y-1/2"
							style="left:{(i / (totalSteps - 1)) * 100}%"
						></span>
					{/each}
				</div>
			{/if}
			<!-- progress fill -->
			<div
				class="absolute left-0 top-0 h-full rounded-full bg-[var(--color-ok)]/30"
				style="width: {totalSteps > 1 ? (stepIdx / (totalSteps - 1)) * 100 : 0}%"
			></div>
			<!-- hidden but interactive range -->
			<input
				type="range"
				min="0"
				max={Math.max(totalSteps - 1, 0)}
				step="1"
				value={stepIdx}
				oninput={(e) => onScrub?.(Number((e.target as HTMLInputElement).value))}
				class="absolute inset-0 w-full cursor-pointer opacity-0 disabled:cursor-not-allowed"
				{disabled}
				aria-label="scrub frames"
			/>
		</div>
		<div class="flex items-center justify-between text-[9.5px] text-[var(--color-faint)]">
			<span>{disabled ? 'pick a composite to scrub' : 'oldest'}</span>
			<span class="num text-[var(--color-muted)]">
				{disabled ? '' : `${stepIdx + 1} / ${totalSteps}`}
			</span>
			<span>{disabled ? '' : 'latest'}</span>
		</div>
	</div>
</div>
