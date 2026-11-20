<script lang="ts">
	/** The radar console.
	 *
	 *  The map used to sit in the center six columns of the Live page, next
	 *  to four rails competing for the same screen. It gets the viewport and
	 *  the landing slot now. The layer panel alone is 280px, and the controls
	 *  that make this a console rather than a viewer need room the old grid
	 *  never had.
	 *
	 *  Nothing about the map's behavior changes here. MapView is the same
	 *  component the dashboard rendered, given a bigger box.
	 */
	import MapView from '$lib/components/MapView.svelte';
	import { diag } from '$lib/diag';
</script>

<svelte:head><title>AQPI Sentinel</title></svelte:head>

<div class="h-full p-2">
	<section class="panel flex h-full flex-col overflow-hidden">
		{#if diag.map}
			<MapView />
		{:else}
			<div class="flex h-full items-center justify-center text-[12px] uppercase
			            tracking-[0.18em] text-[var(--color-muted)]">
				map disabled · ?diag=no-map
			</div>
		{/if}
	</section>
</div>
