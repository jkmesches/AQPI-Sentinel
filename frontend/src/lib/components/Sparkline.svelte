<script lang="ts">
	let {
		data = [],
		width = 80,
		height = 18,
		stroke = 'currentColor',
		fill = true
	}: {
		data?: number[];
		width?: number;
		height?: number;
		stroke?: string;
		fill?: boolean;
	} = $props();

	const paths = $derived.by(() => {
		if (!data.length) return { stroke: '', fill: '' };
		const min = Math.min(...data);
		const max = Math.max(...data);
		const range = max - min || 1;
		const dx = data.length > 1 ? width / (data.length - 1) : 0;
		const pts = data.map((v, i) => {
			const x = i * dx;
			const y = height - ((v - min) / range) * (height - 2) - 1;
			return [x, y] as [number, number];
		});
		const strokeD = pts
			.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`)
			.join(' ');
		const fillD =
			pts.length > 1
				? `${strokeD} L${(width).toFixed(1)},${height} L0,${height} Z`
				: '';
		return { stroke: strokeD, fill: fillD };
	});
</script>

<svg class="spark" {width} {height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
	{#if fill && paths.fill}
		<path class="fill" d={paths.fill} fill={stroke} />
	{/if}
	<path class="stroke" d={paths.stroke} fill="none" {stroke} stroke-width="1" />
</svg>
