<script lang="ts">
	/** The radar console.
	 *
	 *  The map used to sit in the center six columns of the Live page, next
	 *  to four rails competing for the same screen. It gets the viewport and
	 *  the landing slot now. The layer panel alone is 280px, and the controls
	 *  that make this a console rather than a viewer need room the old grid
	 *  never had.
	 *
	 *  Compare mode puts a second pane beside the first. Each owns its
	 *  product, radars, moment, unit and overlays, so the only difference on
	 *  screen is the thing being compared. The two are held together here,
	 *  by the time they show and the camera they look through, because
	 *  neither pane can own state the other also writes.
	 *
	 *  Side by side rather than stacked. The comparison is usually between
	 *  two products over the same ground, and two half-height maps lose the
	 *  north-south extent that most of this network covers.
	 *
	 *  The time strip sits below both panes rather than inside the first.
	 *  Inside, it takes height from that pane alone and the two maps end up
	 *  different sizes, covering different ground at the same zoom.
	 */
	import MapView from '$lib/components/MapView.svelte';
	import TimeControls from '$lib/components/TimeControls.svelte';
	import type { TimeStrip } from '$lib/map/paneTime';
	import type { Camera } from '$lib/map/permalink';
	import { diag } from '$lib/diag';

	let compare = $state(false);
	// Owned here because both panes read them and one pane writes each.
	let sharedTime = $state<string | null>(null);
	let sharedCamera = $state<Camera | null>(null);
	// Published by the primary pane, drawn here.
	let timeStrip = $state<TimeStrip | null>(null);
</script>

<svelte:head><title>AQPI Sentinel</title></svelte:head>

<div class="h-full p-2">
	<section class="panel flex h-full flex-col overflow-hidden">
		{#if diag.map}
			<div class="flex min-h-0 w-full flex-1 gap-px">
				<div class="h-full min-w-0 flex-1">
					<MapView
						pane={0}
						{compare}
						bind:sharedTime
						bind:sharedCamera
						bind:timeStrip
						onToggleCompare={() => { compare = !compare; }}
					/>
				</div>
				{#if compare}
					<!-- A fresh MapView each time compare opens. A MapLibre
					     instance does not survive its container being
					     removed, so reusing one would hand the second pane
					     a torn-down style. -->
					<div
						class="h-full min-w-0 flex-1 border-l border-[var(--color-border-strong)]"
					>
						<MapView
							pane={1}
							{compare}
							bind:sharedTime
							bind:sharedCamera
						/>
					</div>
				{/if}
			</div>
			{#if timeStrip}
				<TimeControls
					stepIdx={timeStrip.stepIdx}
					totalSteps={timeStrip.totalSteps}
					stepLabel={timeStrip.stepLabel}
					playing={timeStrip.playing}
					activity={timeStrip.activity}
					onFirst={timeStrip.onFirst}
					onPrev={timeStrip.onPrev}
					onTogglePlay={timeStrip.onTogglePlay}
					onNext={timeStrip.onNext}
					onLast={timeStrip.onLast}
					onScrub={timeStrip.onScrub}
				/>
			{/if}
		{:else}
			<div class="flex h-full items-center justify-center text-[12px] uppercase
			            tracking-[0.18em] text-[var(--color-muted)]">
				map disabled · ?diag=no-map
			</div>
		{/if}
	</section>
</div>
