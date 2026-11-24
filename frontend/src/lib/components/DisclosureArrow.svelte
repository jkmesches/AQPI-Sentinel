<script lang="ts">
	/** Point right while a row is closed and down while it is open. */
	interface Props {
		open: boolean;
	}

	let { open }: Props = $props();
</script>

<!-- An SVG rather than the ▶ character: Poppins has no such glyph, and the
     fallback font that has one draws it as an orange color emoji. Rotated
     rather than swapped, so the arrow animates and the row never reflows. -->
<svg
	viewBox="0 0 12 12"
	width="12"
	height="12"
	aria-hidden="true"
	class="shrink-0 text-[var(--color-muted)] transition-transform duration-150"
	style:transform={open ? 'rotate(90deg)' : 'rotate(0deg)'}
>
	<path d="M4 2.5 L8.5 6 L4 9.5 Z" fill="currentColor" />
</svg>
