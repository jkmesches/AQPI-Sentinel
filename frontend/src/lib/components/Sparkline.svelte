<!--
  Time-based hybrid sparkline.

  Y axis carries TWO signals at once:

    1. For non-empty buckets, height is the mean of `value` across the
       samples that landed in that bucket, normalized against the
       observed min/max in the visible window. This brings back the
       per-check shape that pure-count sparklines homogenized away —
       a radar producing 10 scans/min looks different from one
       producing 2, a product whose age_s drifts looks different from
       a fresh one.

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

	let {
		data = [],
		cadenceS = null,
		width = 80,
		height = 18,
		stroke = 'currentColor',
		showLabel = true,
		tickMs = 5_000
	}: {
		data?: { ts: number; value: number }[];
		cadenceS?: number | null;
		width?: number;
		height?: number;
		stroke?: string;
		showLabel?: boolean;
		tickMs?: number;
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

	const result = $derived.by(() => {
		const windowMs = win.ms;
		// One bucket per cadence interval — when healthy, that's roughly
		// one sample per bucket. Floor at 15 s so a wonky 0-cadence input
		// doesn't make the bucket count blow up.
		const baseBucket = Math.max(15_000, (cadenceS ?? 60) * 1000);
		const nBuckets = Math.max(8, Math.min(48, Math.floor(windowMs / baseBucket)));
		const bucketMs = windowMs / nBuckets;
		// Track sums + counts so we can derive per-bucket means. Buckets
		// that received no samples stay at count=0 and render at baseline.
		const sums = new Array(nBuckets).fill(0);
		const counts = new Array(nBuckets).fill(0);
		const left = now - windowMs;
		for (const d of data) {
			if (d.ts < left || d.ts > now) continue;
			const i = Math.min(nBuckets - 1, Math.floor((d.ts - left) / bucketMs));
			if (i >= 0) {
				sums[i] += d.value;
				counts[i]++;
			}
		}
		const total = counts.reduce((a, b) => a + b, 0);
		// Per-bucket mean, or null for an empty bucket (renders at baseline).
		const means: (number | null)[] = sums.map((s, i) => (counts[i] > 0 ? s / counts[i] : null));
		const nonEmpty = means.filter((v): v is number => v !== null);
		// Local autoscale: each sparkline's variation gets the full y range,
		// so a radar producing 2-vs-12 scans/min looks visibly different
		// from one producing 50-vs-60. Floors prevent div-by-zero when the
		// metric is constant or the window is entirely empty.
		const minV = nonEmpty.length > 0 ? Math.min(...nonEmpty) : 0;
		const maxV = nonEmpty.length > 0 ? Math.max(...nonEmpty) : 1;
		const span = Math.max(1e-9, maxV - minV);
		const dx = nBuckets > 1 ? width / (nBuckets - 1) : 0;
		const pts = means.map((v, i) => {
			const x = i * dx;
			// Empty → baseline. Non-empty → linearly scaled within the
			// observed range so the bucket variation reads as shape.
			const y =
				v === null
					? height - 1
					: height - ((v - minV) / span) * (height - 2) - 1;
			return [x, y] as [number, number];
		});
		const strokeD = pts
			.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`)
			.join(' ');
		// Area fill spans the full width with baseline at height — so the
		// "trace at zero" state still draws as a thin sliver, visibly
		// distinct from an empty/unrendered sparkline.
		const fillD = pts.length > 1
			? `${strokeD} L${width.toFixed(1)},${height} L0,${height} Z`
			: '';
		return { strokeD, fillD, total };
	});
</script>

<span class="inline-flex items-center gap-1.5 align-middle">
	<svg class="spark" {width} {height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
		{#if result.fillD}
			<path class="fill" d={result.fillD} fill={stroke} fill-opacity="0.18" />
		{/if}
		<path class="stroke" d={result.strokeD} fill="none" {stroke} stroke-width="1" />
	</svg>
	{#if showLabel}
		<span class="num text-[10px] text-[var(--color-muted)] tabular-nums">{result.total}/{win.label}</span>
	{/if}
</span>
