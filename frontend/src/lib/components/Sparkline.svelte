<!--
  Time-based "data flow" sparkline.

  Y axis is NOT the raw metric value — it's the COUNT of samples that
  landed in each time bucket. So the trace is a heartbeat: tall when
  data is flowing, flat at the baseline when it's not. When an
  upstream stops, the rightmost buckets sum to 0 and the trace visibly
  drops to the bottom rather than freezing at the last value — which
  is what the earlier "plot the value at sample index" version did,
  and which the operator confused for healthy flow during an upstream
  outage on 2026-05-19.

  Window length auto-adapts from the check's cadence (windowFromCadence
  in format.ts): ~30 cadence intervals visible, snapped to 30m / h /
  3h / 6h / 12h. The trailing label reads "N/{window}" e.g. "16/h" or
  "0/6h" so the time scale is always obvious.

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
		const counts = new Array(nBuckets).fill(0);
		const left = now - windowMs;
		for (const d of data) {
			if (d.ts < left || d.ts > now) continue;
			const i = Math.min(nBuckets - 1, Math.floor((d.ts - left) / bucketMs));
			if (i >= 0) counts[i]++;
		}
		const total = counts.reduce((a, b) => a + b, 0);
		// Y autoscale: peak count drives the top. Floor at 1 so the all-
		// zero case still draws at the baseline (y = height-1) rather than
		// collapsing into a div-by-zero.
		const max = Math.max(1, ...counts);
		const dx = nBuckets > 1 ? width / (nBuckets - 1) : 0;
		const pts = counts.map((c, i) => {
			const x = i * dx;
			const y = height - (c / max) * (height - 2) - 1;
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
