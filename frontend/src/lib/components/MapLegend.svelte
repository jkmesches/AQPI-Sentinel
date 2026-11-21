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

	interface Props {
		ramp: Ramp;
		/** Shown at the end of the bar. Defaults to the ramp's own unit. */
		unit?: string;
		/** Overrides the ramp's own axis label. */
		title?: string;
	}
	let { ramp, unit, title }: Props = $props();

	const uid = $props.id();

	// Bar geometry, in CSS pixels. These are layout choices, so they live here
	// rather than in the geometry module.
	const BAR_H = 10;
	const TICK_H = 3;
	const LABEL_SIZE = 9.5;
	const LABEL_BASELINE = BAR_H + TICK_H + LABEL_SIZE;
	const SVG_H = LABEL_BASELINE + 2;
	/** Clear space between two tick labels, in characters of their own font. */
	const LABEL_GAP_CHARS = 1;
	/** The string the character advance is measured from. */
	const RULER = '0123456789';

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
		void ruler;
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
	class="pointer-events-none rounded-sm border border-[var(--color-border-strong)] bg-[var(--color-canvas)]/90 px-2 py-1.5 shadow-lg backdrop-blur"
>
	<div
		data-legend-title
		class="num mb-1 text-[9.5px] tracking-wide text-[var(--color-faint)]"
	>
		{titleText}
	</div>
	<div bind:clientWidth={barWidth} class="w-full">
		{#if barWidth > 0}
			<svg
				width={barWidth}
				height={SVG_H}
				viewBox="0 0 {barWidth} {SVG_H}"
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
					font-size={LABEL_SIZE}>{RULER}</text
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
						height={BAR_H}
						fill="url(#legend-{uid})"
					/>
				{:else}
					{#each blocks as b (b.lo)}
						<rect
							x={padPx + b.from * barPx}
							y="0"
							width={Math.max(0.5, (b.to - b.from) * barPx)}
							height={BAR_H}
							fill={b.color}
						/>
					{/each}
				{/if}
				<rect
					x={padPx + 0.25}
					y="0.25"
					width={barPx - 0.5}
					height={BAR_H - 0.5}
					fill="none"
					stroke="var(--color-border-strong)"
					stroke-width="0.5"
				/>

				{#each ticks as t (t.value)}
					{@const x = padPx + Math.min(barPx - 0.25,
						Math.max(0.25, t.at * barPx))}
					<line
						x1={x}
						y1={BAR_H}
						x2={x}
						y2={BAR_H + TICK_H}
						stroke="var(--color-faint)"
						stroke-width="0.75"
					/>
					<text
						class="num"
						x={padPx + t.at * barPx}
						y={LABEL_BASELINE}
						font-size={LABEL_SIZE}
						text-anchor="middle"
						fill="var(--color-muted)">{t.label}</text
					>
				{/each}
			</svg>
		{/if}
	</div>
</div>
