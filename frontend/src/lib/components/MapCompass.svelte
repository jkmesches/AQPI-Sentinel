<!--
  Which way the map is facing, and a way back to north.

  The console opens at a bearing of 22 degrees so the Bay fits a landscape
  pane. Every direction read off a rotated map is wrong by that much, and
  the measure tool's own harness found it the hard way: a reference leg
  came out 0.8 km off over 216 km until the camera was pinned north up.

  Drawn rather than taken from MapLibre's NavigationControl, because that
  one brings its own button chrome and zoom buttons the console already
  has elsewhere.
-->
<script lang="ts">
	import { bearingLabel, isNorthUp, needleRotation } from '$lib/map/compass';
	import { DEFAULT_UI_SCALE, scaled } from '$lib/map/uiScale';

	interface Props {
		/** The map's bearing in degrees, clockwise from north. */
		bearing: number;
		/** The size the console draws its overlays at. */
		scale?: number;
		/** Called when the reader asks for north. */
		onreset: () => void;
	}
	let { bearing, scale = DEFAULT_UI_SCALE, onreset }: Props = $props();

	const rotation = $derived(needleRotation(bearing));
	const northUp = $derived(isNorthUp(bearing));
	const label = $derived(bearingLabel(bearing));

	// 56 px across with a 40 px needle, which is what the drawing below is
	// laid out for. Everything else here is that pair scaled.
	const dial = $derived(scaled(56, scale));
	const needle = $derived(scaled(40, scale));
	const text = $derived(scaled(12, scale));
	const textTop = $derived(scaled(38, scale));
</script>

<button
	type="button"
	data-compass
	data-compass-bearing={Math.round(bearing)}
	data-compass-north={northUp ? 'yes' : 'no'}
	title="{label} · click for north"
	aria-label="Map heading {label}. Activate to face north."
	onclick={onreset}
	class="grid place-items-center rounded-full border border-[var(--color-border-strong)] bg-[var(--color-canvas)]/95 shadow-lg backdrop-blur transition-colors hover:border-[var(--color-accent)]"
	style="height: {dial}px; width: {dial}px"
>
	<svg
		width={needle}
		height={needle}
		viewBox="-13 -13 26 26"
		aria-hidden="true"
		style="transform: rotate({rotation}deg)"
	>
		<!-- The south half stays quiet so the north arrow reads at a
		     glance; a two-tone needle at 26 px turns into a lozenge.
		     The accent rather than a red: every red in this console is
		     a status, and a compass is not reporting a fault. -->
		<polygon points="0,-9 4,1 0,-1 -4,1" fill="var(--color-accent)" />
		<polygon points="0,9 4,1 0,-1 -4,1" fill="var(--color-faint)" />
		<circle r="1.2" fill="var(--color-default)" />
	</svg>
	<span
		class="num pointer-events-none absolute tracking-wider"
		class:text-[var(--color-faint)]={northUp}
		class:text-[var(--color-default)]={!northUp}
		style="margin-top: {textTop}px; font-size: {text}px"
	>
		{northUp ? 'N' : `${Math.round(bearing)}°`}
	</span>
</button>
