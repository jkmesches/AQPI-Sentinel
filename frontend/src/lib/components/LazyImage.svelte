<!--
  Visibility-gated image loader.

  Why this exists: Firefox's native `loading="lazy"` aggressively pre-fetches
  images that aren't really visible (e.g. inside a collapsed scroll container
  or a freshly-mounted dialog). When the cell-detail panel mounts up to 50
  L4 runs at once, the browser would kick off 50 simultaneous PNG fetches +
  decodes — chewing CPU on `MOZ_Z_inflate_fast` and `PremultiplyChunk_SSE2`
  for images the user might never look at, then continuing to decode them
  even after the panel closes.

  This component renders an empty placeholder of the requested height until
  an IntersectionObserver confirms the placeholder is genuinely on-screen.
  Only then is the real `<img>` element swapped in. When the component
  unmounts (panel close / cell change), the `<img>` is removed and the
  browser aborts any in-flight decode.
-->
<script lang="ts">
	import { onMount, onDestroy } from 'svelte';

	let {
		src,
		alt = '',
		minHeight = 200,
		rootMargin = '200px 0px',
		errorSnippet
	}: {
		src: string;
		alt?: string;
		minHeight?: number;
		rootMargin?: string;
		errorSnippet?: import('svelte').Snippet;
	} = $props();

	let host: HTMLDivElement | undefined = $state();
	let visible = $state(false);
	let errored = $state(false);
	let io: IntersectionObserver | undefined;

	onMount(() => {
		if (!host) return;
		io = new IntersectionObserver(
			(entries) => {
				if (entries[0]?.isIntersecting) {
					visible = true;
					io?.disconnect();
				}
			},
			{ rootMargin }
		);
		io.observe(host);
	});
	onDestroy(() => io?.disconnect());
</script>

<div bind:this={host} style="min-height:{minHeight}px;">
	{#if errored}
		{#if errorSnippet}
			{@render errorSnippet()}
		{:else}
			<div class="p-4 text-center text-[11px] text-[var(--color-muted)]">image unavailable</div>
		{/if}
	{:else if visible}
		<img
			{src}
			{alt}
			class="block w-full h-auto"
			decoding="async"
			fetchpriority="low"
			onerror={() => (errored = true)}
		/>
	{/if}
</div>
