<!--
  Time-based hybrid sparkline.

  Y axis carries TWO signals at once:

    1. For non-empty buckets, height is the mean of `value` across the
       samples that landed in that bucket, scaled either against a
       FIXED `domain` (what the dashboard passes for `headroom`) or,
       with no domain, against the observed min/max in the window.

       Prefer a fixed domain wherever the metric has one. Autoscale
       rescales every trace to fill the box, which erases exactly what
       the sparkline is for: a perfectly steady series and a dead one
       both land on the baseline — identical pixel for pixel — and a
       2% wobble draws the same full-height zigzag as a 6x swing.
       Autoscale remains the default only because a bare count has no
       natural ceiling to scale against.

    2. For empty buckets (no samples in that interval), the trace
       drops to the baseline. This preserves the outage-detection
       behavior the earlier rewrite was for — when an upstream stops,
       the rightmost buckets visibly flatline instead of freezing at
       the last value (the failure mode that fooled the operator
       during the 2026-05-19 outage).

  Window length auto-adapts from the check's cadence (windowFromCadence
  in format.ts): ~30 cadence intervals visible, snapped to 30m / h /
  3h / 6h / 12h. The trailing label reads "N/{window}" e.g. "16/h" or
  "0/6h" — N is still the sample count, so flow rate stays legible.

  An internal ticker advances `now` every few seconds so older buckets
  drift leftward and eventually fall off, even when no new samples
  arrive — the "shrinkage on the right" IS the staleness indicator.
-->
<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { windowFromCadence } from '$lib/format';
	import { sparklineGeometry } from '$lib/sparklineGeometry';

	let {
		data = [],
		cadenceS = null,
		width = 80,
		height = 18,
		stroke = 'currentColor',
		showLabel = true,
		tickMs = 5_000,
		domain = null,
		warnAt = null
	}: {
		data?: { ts: number; value: number }[];
		cadenceS?: number | null;
		width?: number;
		height?: number;
		stroke?: string;
		showLabel?: boolean;
		tickMs?: number;
		/** Fixed [min, max] y range. Null autoscales to the window. */
		domain?: [number, number] | null;
		/** Draw a dashed rule at this value, in the metric's own units. */
		warnAt?: number | null;
	} = $props();

	const win = $derived(windowFromCadence(cadenceS));

	let now = $state(Date.now());
	let timer: ReturnType<typeof setInterval> | undefined;
	onMount(() => {
		const start = () => {
			if (timer) return;
			timer = setInterval(() => (now = Date.now()), tickMs);
		};
		const stop = () => {
			if (timer) { clearInterval(timer); timer = undefined; }
		};
		if (typeof document !== 'undefined') {
			document.addEventListener('visibilitychange', () => {
				if (document.hidden) stop(); else start();
			});
		}
		start();
	});
	onDestroy(() => { if (timer) clearInterval(timer); });

	// All of it lives in $lib/sparklineGeometry so it can be tested without a
	// DOM. See that module for why the fixed-domain path exists.
	const result = $derived(
		sparklineGeometry({
			data,
			now,
			windowMs: win.ms,
			cadenceS,
			width,
			height,
			domain,
			warnAt
		})
	);
</script>

<span class="inline-flex items-center gap-1.5 align-middle">
	<svg class="spark" {width} {height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
		{#if result.warnY !== null}
			<path
				class="warn-rule"
				d={`M0,${result.warnY} L${width},${result.warnY}`}
				stroke="var(--color-border-strong)"
				stroke-width="1"
				stroke-dasharray="2 2"
				fill="none"
			/>
		{/if}
		{#if result.fillD}
			<path class="fill" d={result.fillD} fill={stroke} fill-opacity="0.18" />
		{/if}
		<path class="stroke" d={result.strokeD} fill="none" {stroke} stroke-width="1" />
	</svg>
	{#if showLabel}
		<span class="num text-[10px] text-[var(--color-muted)] tabular-nums">{result.total}/{win.label}</span>
	{/if}
</span>
