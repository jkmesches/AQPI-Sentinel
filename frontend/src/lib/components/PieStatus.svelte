<script lang="ts">
	/** Filled status proportion pie.
	 *
	 *  Switched 2026-05-18 from a thin-stroke donut to a fully-filled pie
	 *  whose slices extend to the center — the donut variant proved
	 *  unreadable at 13px (user feedback). The fill version stays
	 *  recognizable down to ~10px.
	 *
	 *  Slices are ordered red → yellow → green (grouped, never interleaved)
	 *  so "this section has problems" reads at a glance.
	 */
	let {
		counts,
		size = 16
	}: {
		counts: { pass?: number; warn?: number; fail?: number; error?: number; skip?: number };
		size?: number;
	} = $props();

	const fail = $derived((counts.fail ?? 0) + (counts.error ?? 0));
	const warn = $derived(counts.warn ?? 0);
	const pass = $derived(counts.pass ?? 0);
	const skip = $derived(counts.skip ?? 0);

	const totalActive = $derived(fail + warn + pass);
	const total = $derived(totalActive > 0 ? totalActive : skip);

	const cx = $derived(size / 2);
	const cy = $derived(size / 2);
	const r  = $derived(size / 2 - 0.5);   // tiny inset so the outer stroke doesn't clip

	type Slice = { color: string; fraction: number };
	const slices = $derived.by<Slice[]>(() => {
		if (total === 0) return [];
		if (totalActive > 0) {
			return [
				{ color: 'var(--color-fail)', fraction: fail / totalActive },
				{ color: 'var(--color-warn)', fraction: warn / totalActive },
				{ color: 'var(--color-ok)',   fraction: pass / totalActive }
			].filter((s) => s.fraction > 0);
		}
		return [{ color: 'var(--color-faint)', fraction: 1 }];
	});

	// SVG arc helper. Returns a path string for a wedge from `a0` to `a1`
	// (radians, 0 = 12-o'clock-like since we apply a -PI/2 offset).
	function wedgePath(a0: number, a1: number): string {
		// Full circle as one wedge: SVG won't draw a 360° arc, so emit two halves.
		if (Math.abs(a1 - a0) >= Math.PI * 2 - 1e-6) {
			return `M ${cx} ${cy - r} A ${r} ${r} 0 1 1 ${cx - 0.001} ${cy - r} Z`;
		}
		const x0 = cx + r * Math.cos(a0);
		const y0 = cy + r * Math.sin(a0);
		const x1 = cx + r * Math.cos(a1);
		const y1 = cy + r * Math.sin(a1);
		const large = (a1 - a0) > Math.PI ? 1 : 0;
		return `M ${cx} ${cy} L ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1} Z`;
	}

	const wedges = $derived.by(() => {
		if (slices.length === 0) return [];
		const out: { d: string; color: string }[] = [];
		const offset = -Math.PI / 2;
		let acc = 0;
		for (const s of slices) {
			const a0 = offset + acc * 2 * Math.PI;
			const a1 = offset + (acc + s.fraction) * 2 * Math.PI;
			out.push({ d: wedgePath(a0, a1), color: s.color });
			acc += s.fraction;
		}
		return out;
	});
</script>

{#if total === 0}
	<svg viewBox="0 0 {size} {size}" width={size} height={size} aria-hidden="true">
		<circle cx={cx} cy={cy} r={r} fill="var(--color-elevated)" stroke="var(--color-border)" stroke-width="1" />
	</svg>
{:else}
	<svg viewBox="0 0 {size} {size}" width={size} height={size} aria-hidden="true">
		{#each wedges as w}
			<path d={w.d} fill={w.color} />
		{/each}
		<!-- Thin outer border so adjacent slices remain distinct on any background. -->
		<circle cx={cx} cy={cy} r={r} fill="none" stroke="var(--color-canvas)" stroke-width="0.75" />
	</svg>
{/if}
