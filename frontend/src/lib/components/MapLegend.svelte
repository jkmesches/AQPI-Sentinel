<!--
  The map's color scale, drawn as themed SVG.

  A proxied colorbar PNG would be one image in one theme at one size. This
  reads in both themes, scales at phone width, and can carry a converted unit
  once slice 7 lands, because the numbers are text rather than pixels.

  Everything about where the ticks go is in $lib/map/ramps/legend.ts and
  tested there. This file measures the two things only the browser knows, the
  bar's width and the font's character advance, and draws the result.
-->
<script lang="ts">
	import type { Ramp } from '$lib/map/ramps/invert';
	import {
		rampKind, legendBlocks, legendGradient, barLayout
	} from '$lib/map/ramps/legend';
	import { DEFAULT_UI_SCALE, scaled } from '$lib/map/uiScale';

	interface Props {
		ramp: Ramp;
		/** Shown at the end of the bar. Defaults to the ramp's own unit. */
		unit?: string;
		/** Overrides the ramp's own axis label. */
		title?: string;
		/** The size the console draws its overlays at. */
		scale?: number;
	}
	let { ramp, unit, title, scale = DEFAULT_UI_SCALE }: Props = $props();

	const uid = $props.id();

	// Bar geometry at the default size, in CSS pixels. These are layout
	// choices, so they live here rather than in the geometry module.
	//
	// Sized up on 2026-11-21 after the first version read as decoration: a
	// 10 px band carrying 22 blocks gave each one 8 px, and four tick
	// labels at 9.5 px in the faint color left the reader counting
	// squares to find a value. The band is now thick enough to judge a
	// color against the map, and the numbers are in the same ink as the
	// rest of the console rather than in its quietest grey.
	const BAR_H = 20;
	const TICK_H = 4;
	const LABEL_SIZE = 12;
	/** Clear space between two tick labels, in characters of their own font. */
	const LABEL_GAP_CHARS = 1;
	/** The string the character advance is measured from. */
	const RULER = '0123456789';

	const barH = $derived(scaled(BAR_H, scale));
	const tickH = $derived(scaled(TICK_H, scale));
	const labelSize = $derived(scaled(LABEL_SIZE, scale));
	const labelBaseline = $derived(barH + tickH + labelSize);
	const svgH = $derived(labelBaseline + 2);

	let barWidth = $state(0);
	let charPx = $state(0);
	let ruler: SVGTextElement | undefined = $state();

	/**
	 * Measure one character in the font the browser actually resolved.
	 *
	 * `--font-mono` is a stack, so the face that wins differs by machine, and
	 * a label laid out against the wrong advance either overlaps its neighbor
	 * or wastes half the bar. Re-measured once fonts settle, since the first
	 * measurement can land on the fallback face.
	 */
	function measureChar(): void {
		if (!ruler) return;
		const w = ruler.getComputedTextLength();
		if (w > 0) charPx = w / RULER.length;
	}

	$effect(() => {
		// Re-measured when the size changes as well as when the node
		// appears. A layout kept from the old size packs the labels of an
		// 11 px font into a bar drawn for a 22 px one.
		void ruler; void labelSize;
		measureChar();
		if (typeof document !== 'undefined' && document.fonts) {
			void document.fonts.ready.then(measureChar);
		}
	});

	const kind = $derived(rampKind(ramp));
	const blocks = $derived(kind === 'discrete' ? legendBlocks(ramp) : []);
	const gradient = $derived(kind === 'continuous' ? legendGradient(ramp) : []);

	// The layout waits for both measurements. Drawing against a guessed
	// character advance is the one failure this component can produce on its
	// own, and an empty axis for one frame is the cheaper wrong answer.
	const layout = $derived(
		barWidth > 2 * charPx && charPx > 0
			? barLayout(ramp, barWidth, charPx, LABEL_GAP_CHARS)
			: null
	);
	const barPx = $derived(layout?.barPx ?? 0);
	const padPx = $derived(layout?.padPx ?? 0);
	const ticks = $derived(layout?.ticks ?? []);

	const unitText = $derived(unit ?? ramp.unit);
	const titleText = $derived(title ?? ramp.label);

</script>

<div
	data-legend
	class="pointer-events-none rounded border border-[var(--color-border-strong)] bg-[var(--color-canvas)]/95 shadow-lg backdrop-blur"
	style="padding: {scaled(8, scale)}px {scaled(10, scale)}px"
>
	<div
		data-legend-title
		class="num tracking-wide text-[var(--color-default)]"
		style="font-size: {labelSize}px; margin-bottom: {scaled(6, scale)}px"
	>
		{titleText}
	</div>
	<div bind:clientWidth={barWidth} class="w-full">
		{#if barWidth > 0}
			<svg
				width={barWidth}
				height={svgH}
				viewBox="0 0 {barWidth} {svgH}"
				role="img"
				aria-label="{titleText} color scale, {ramp.min} to {ramp.max} {unitText}"
			>
				<!-- Measured off-screen rather than hidden, because a display:none
				     text node reports a length of zero. -->
				<text
					bind:this={ruler}
					class="num"
					x="-9999"
					y="-9999"
					font-size={labelSize}>{RULER}</text
				>

				{#if kind === 'continuous'}
					<defs>
						<linearGradient id="legend-{uid}" x1="0" y1="0" x2="1" y2="0">
							{#each gradient as s (s.offset + s.color)}
								<stop offset={s.offset} stop-color={s.color} />
							{/each}
						</linearGradient>
					</defs>
					<rect
						x={padPx}
						y="0"
						width={barPx}
						height={barH}
						fill="url(#legend-{uid})"
					/>
				{:else}
					<!-- Keyed on where the block sits, which is unique because
					     the blocks tile the bar. Keying on its value range
					     threw each_key_duplicate on a segmented ramp, where
					     ten colors report one band, and the error took the
					     whole scale off the map. -->
					{#each blocks as b (b.from + b.color)}
						<rect
							x={padPx + b.from * barPx}
							y="0"
							width={Math.max(0.5, (b.to - b.from) * barPx)}
							height={barH}
							fill={b.color}
						/>
					{/each}
				{/if}
				<rect
					x={padPx + 0.25}
					y="0.25"
					width={barPx - 0.5}
					height={barH - 0.5}
					fill="none"
					stroke="var(--color-border-strong)"
					stroke-width="0.5"
				/>

				{#each ticks as t (t.value)}
					{@const x = padPx + Math.min(barPx - 0.25,
						Math.max(0.25, t.at * barPx))}
					<line
						x1={x}
						y1={barH}
						x2={x}
						y2={barH + tickH}
						stroke="var(--color-muted)"
						stroke-width="1"
					/>
					<text
						class="num"
						x={padPx + t.at * barPx}
						y={labelBaseline}
						font-size={labelSize}
						text-anchor="middle"
						fill="var(--color-default)">{t.label}</text
					>
				{/each}
			</svg>
		{/if}
	</div>
</div>
