<script lang="ts">
	/** Time controls strip — full-width band beneath the map. */
	import { DEFAULT_SPEED, SPEEDS, speedLabel } from '$lib/map/playback';
	import { buildAxis, gapLabel, nearestStep } from '$lib/map/timeAxis';
	import { shouldWrapStrip } from '$lib/map/responsive';

	let {
		stepIdx = 0,
		totalSteps = 0,
		stepLabel = '',
		playing = false,
		activity = [],
		times = [],
		speed = DEFAULT_SPEED,
		onFirst,
		onPrev,
		onTogglePlay,
		onNext,
		onLast,
		onScrub,
		onSpeed,
		onHelp
	}: {
		stepIdx?: number;
		totalSteps?: number;
		stepLabel?: string;
		playing?: boolean;
		activity?: readonly number[];
		/** Each frame's timestamp, so the axis can place it by time. */
		times?: readonly (string | null)[];
		speed?: number;
		onFirst?: () => void;
		onPrev?: () => void;
		onTogglePlay?: () => void;
		onNext?: () => void;
		onLast?: () => void;
		onScrub?: (i: number) => void;
		onSpeed?: (s: number) => void;
		/** Shows the shortcut list. Without a control for it, the keyboard
		 *  is reachable only by already knowing that `?` opens it. */
		onHelp?: () => void;
	} = $props();

	const disabled = $derived(totalSteps === 0);
	const maxAct = $derived(activity.length ? Math.max(0.005, ...activity) : 0.005);
	const hasActivity = $derived(
		activity.length > 0 && activity.length === totalSteps);

	// A times list of another length describes another product. Spacing this
	// one by those timestamps would place frames at times they do not have.
	const axis = $derived(
		buildAxis(times.length === totalSteps ? times : Array(totalSteps).fill(null)));
	const position = $derived(axis.positions[stepIdx] ?? 0);

	let trackEl: HTMLDivElement | undefined = $state();
	let scrubbing = $state(false);
	let stripWidth = $state(0);
	const wrapped = $derived(shouldWrapStrip(stripWidth));

	/**
	 * Move to the frame nearest a point on the track.
	 *
	 * By time rather than by index, because the bars are placed by time and
	 * a click inside a gap has no frame under it at all.
	 *
	 * @param clientX Where the pointer is, in client coordinates.
	 */
	function scrubToPointer(clientX: number): void {
		if (disabled || !trackEl) return;
		const box = trackEl.getBoundingClientRect();
		if (box.width <= 0) return;
		const f = (clientX - box.left) / box.width;
		const i = nearestStep(axis.positions, Math.min(1, Math.max(0, f)));
		if (i >= 0 && i !== stepIdx) onScrub?.(i);
	}

	function startScrub(e: PointerEvent): void {
		scrubbing = true;
		(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
		scrubToPointer(e.clientX);
	}

	function dragScrub(e: PointerEvent): void {
		if (scrubbing) scrubToPointer(e.clientX);
	}

	function endScrub(e: PointerEvent): void {
		scrubbing = false;
		(e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId);
	}

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

<!-- data-time-controls lets a harness count the strips. Compare mode
     must have exactly one, since two would disagree about the time the
     panes share. -->
<div
	bind:clientWidth={stripWidth}
	data-time-controls
	data-time-strip={JSON.stringify({ stepIdx, totalSteps, playing, speed })}
	data-strip-layout={wrapped ? 'wrapped' : 'one-row'}
	class="relative z-10 flex gap-x-4 gap-y-1 border-t border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-2 {wrapped
		? 'flex-wrap items-start'
		: 'items-center'}"
>
	<!-- Timestamp readout: time prominent, date subdued -->
	<div class="flex min-w-[10rem] flex-col leading-tight">
		<span class="num text-[18px] tracking-wide text-[var(--color-bright)]">
			{labelParts.time || '—:—:—'}
		</span>
		<span class="num text-[12px] text-[var(--color-muted)]">
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
		<!-- Playback rate. Four fixed steps rather than a slider, because the
		     dwell is a floor and not a rate: the loop waits for the frame and
		     then waits out what is left, so above about 2x on six radars the
		     load sets the interval and finer control would be a fiction. -->
		<select
			data-speed
			class="ml-2 rounded-sm border border-[var(--color-border-control)] bg-[var(--color-surface)] px-1 py-0.5 text-[12px] num text-[var(--color-default)] hover:text-[var(--color-bright)] disabled:opacity-30 focus:outline-none focus:border-[var(--color-info)]"
			value={String(speed)}
			onchange={(e) =>
				onSpeed?.(Number((e.target as HTMLSelectElement).value))}
			title="playback speed"
			aria-label="playback speed"
			{disabled}
		>
			{#each SPEEDS as s (s)}
				<option value={String(s)}>{speedLabel(s)}</option>
			{/each}
		</select>
		<!-- The only sign the shortcuts exist. Without it the keyboard is
		     reachable only by already knowing that `?` opens the list. -->
		{#if onHelp}
			<button
				data-help-toggle
				class="ml-1 inline-flex h-5 w-5 items-center justify-center rounded-sm border border-[var(--color-border-strong)] text-[12px] num text-[var(--color-muted)] hover:text-[var(--color-bright)]"
				onclick={onHelp}
				title="keyboard shortcuts (?)"
				aria-label="keyboard shortcuts"
			>?</button>
		{/if}
	</div>

	<!-- Timeline, placed by time rather than one even slot per frame. An
	     hour with nothing published used to be one slot the same width as
	     every other, so the control that picks the frame was saying the
	     record was continuous. See $lib/map/timeAxis.
	     On a narrow strip it takes its own row, because an axis narrower
	     than the 31 frames it draws is a row of hairlines. -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="flex flex-col gap-1 {wrapped ? 'w-full' : 'flex-1'}"
		data-time-axis={JSON.stringify({
			even: axis.even,
			gaps: axis.gaps.length,
			cadenceMs: axis.cadenceMs
		})}
	>
		<!-- activity bars: one per frame, height = painted-pixel fraction -->
		<div
			bind:this={trackEl}
			class="relative h-3 cursor-pointer overflow-hidden"
			title="weather activity per frame, placed by time"
			onpointerdown={startScrub}
			onpointermove={dragScrub}
			onpointerup={endScrub}
			onpointercancel={endScrub}
		>
			<!-- A gap is drawn before the bars, so a bar at its edge stays
			     readable on top of it. -->
			{#each axis.gaps as g (g.after)}
				<div
					class="absolute bottom-0 top-0 border-x border-[var(--color-warn)]/40 bg-[var(--color-warn)]/10"
					style="left: {g.start * 100}%; width: {g.width * 100}%"
					title="nothing published for {gapLabel(g.ms)}"
				></div>
			{/each}
			{#if hasActivity}
				{#each axis.bars as b (b.index)}
					<!-- The 1px taken off the width is the gutter the old
					     even strip got from `gap-px`. Without it the bars
					     abut and 31 frames read as one solid band. -->
					<div
						class="absolute bottom-0 transition-colors {b.index === stepIdx
							? 'bg-[var(--color-ok)]'
							: b.index < stepIdx
								? 'bg-[var(--color-ok)]/45'
								: 'bg-[var(--color-faint)]/45'}"
						style="left: {b.start * 100}%;
						       width: calc({b.width * 100}% - 1px);
						       min-width: 1px;
						       height: {Math.max(8, (activity[b.index] / maxAct) * 100)}%"
					></div>
				{/each}
			{:else if totalSteps > 0}
				<div class="absolute inset-0 self-center text-center text-[12px] leading-4 text-[var(--color-faint)]">
					measuring activity…
				</div>
			{/if}
		</div>
		<div class="relative h-2">
			<!-- track -->
			<div class="absolute inset-0 rounded-full bg-[var(--color-border)]"></div>
			<!-- gaps again, so the hole is visible on the track as well -->
			{#each axis.gaps as g (g.after)}
				<div
					class="absolute top-0 h-full bg-[var(--color-surface)]"
					style="left: {g.start * 100}%; width: {g.width * 100}%"
				></div>
			{/each}
			<!-- one tick per frame, at its own time -->
			{#if totalSteps > 1}
				{#each axis.positions as p, i}
					<span
						class="absolute h-[6px] w-px {i <= stepIdx
							? 'bg-[var(--color-ok)]'
							: 'bg-[var(--color-faint)]'} top-1/2 -translate-y-1/2"
						style="left:{p * 100}%"
					></span>
				{/each}
			{/if}
			<!-- progress fill -->
			<div
				class="absolute left-0 top-0 h-full rounded-full bg-[var(--color-ok)]/30"
				style="width: {position * 100}%"
			></div>
			<!-- The keyboard and screen-reader control. Pointer events go to
			     the track above instead, because one step of this input is
			     one frame while one pixel of the track is a moment in time,
			     and inside a gap there is no frame at that pixel at all. -->
			<input
				type="range"
				min="0"
				max={Math.max(totalSteps - 1, 0)}
				step="1"
				value={stepIdx}
				oninput={(e) => onScrub?.(Number((e.target as HTMLInputElement).value))}
				class="pointer-events-none absolute inset-0 w-full opacity-0"
				{disabled}
				aria-label="scrub frames"
			/>
		</div>
		<div class="flex items-center justify-between text-[12px] text-[var(--color-faint)]">
			<span>{disabled ? 'pick a composite to scrub' : 'oldest'}</span>
			<span class="num text-[var(--color-muted)]">
				{disabled ? '' : `${stepIdx + 1} / ${totalSteps}`}
				{#if axis.gaps.length}
					<span class="text-[var(--color-warn)]">
						· {axis.gaps.length === 1
							? `${gapLabel(axis.gaps[0].ms)} gap`
							: `${axis.gaps.length} gaps`}
					</span>
				{/if}
			</span>
			<span>{disabled ? '' : 'latest'}</span>
		</div>
	</div>
</div>
