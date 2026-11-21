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
	 *
	 *  The keyboard listener lives here for the same reason. One press must
	 *  produce one act, and two panes each holding a listener would step
	 *  time twice. Time keys go to the published strip, which only the
	 *  primary pane owns; the per-pane keys go to whichever pane the pointer
	 *  is over.
	 */
	import MapView from '$lib/components/MapView.svelte';
	import TimeControls from '$lib/components/TimeControls.svelte';
	import type { TimeStrip } from '$lib/map/paneTime';
	import type { Camera } from '$lib/map/permalink';
	import { paneForKey, type PaneControls } from '$lib/map/paneControls';
	import { actionForKey, panOffset, SHORTCUTS, ZOOM_STEP } from '$lib/map/keys';
	import { diag } from '$lib/diag';

	let compare = $state(false);
	// Owned here because both panes read them and one pane writes each.
	let sharedTime = $state<string | null>(null);
	let sharedCamera = $state<Camera | null>(null);
	// Published by the primary pane, drawn here.
	let timeStrip = $state<TimeStrip | null>(null);
	// Published by each pane, driven from here.
	let controlsA = $state<PaneControls | null>(null);
	let controlsB = $state<PaneControls | null>(null);
	// Which pane the pointer is over, for the keys that belong to one pane.
	let hoveredPane = $state<number | null>(null);
	let helpOpen = $state(false);

	// A pane that has closed leaves its last published controls behind, and
	// those close over a map that MapLibre has torn down. Reading the second
	// pane only while compare is on keeps a dead map out of the dispatch.
	const panes = $derived([controlsA, compare ? controlsB : null]);

	/**
	 * Act on a key press, and say whether anything was done.
	 *
	 * The return value drives `preventDefault`. Space scrolls the page, the
	 * arrows scroll it and Home and End jump it, so a handled key has to be
	 * taken from the browser; an unhandled one must not be.
	 *
	 * @param e The press.
	 * @returns True when the console acted on it.
	 */
	function handleKey(e: KeyboardEvent): boolean {
		const action = actionForKey({
			key: e.key,
			ctrlKey: e.ctrlKey,
			metaKey: e.metaKey,
			altKey: e.altKey,
			shiftKey: e.shiftKey,
			target: e.target as EventTarget & {
				tagName?: string;
				isContentEditable?: boolean;
				getAttribute?: (n: string) => string | null;
			} | null
		});
		if (!action) return false;

		if (action === 'help') { helpOpen = !helpOpen; return true; }
		if (action === 'close') {
			if (!helpOpen) return false;
			helpOpen = false;
			return true;
		}
		if (action === 'compare') { compare = !compare; return true; }

		if (timeStrip) {
			if (action === 'play') { timeStrip.onTogglePlay(); return true; }
			if (action === 'step-back') { timeStrip.onPrev(); return true; }
			if (action === 'step-forward') { timeStrip.onNext(); return true; }
			if (action === 'first') { timeStrip.onFirst(); return true; }
			if (action === 'live') { timeStrip.onLast(); return true; }
		}

		const target = paneForKey(panes, hoveredPane);
		if (!target) return false;
		if (action === 'product-prev') { target.stepProduct(-1); return true; }
		if (action === 'product-next') { target.stepProduct(1); return true; }
		if (action === 'panel') { target.togglePanel(); return true; }
		if (action === 'zoom-in') { target.zoomBy(ZOOM_STEP); return true; }
		if (action === 'zoom-out') { target.zoomBy(-ZOOM_STEP); return true; }

		const { width, height } = target.size();
		const offset = panOffset(action, width, height);
		if (!offset) return false;
		target.panBy(offset[0], offset[1]);
		return true;
	}

	function onKeydown(e: KeyboardEvent): void {
		if (handleKey(e)) e.preventDefault();
	}
</script>

<svelte:head><title>AQPI Sentinel</title></svelte:head>
<svelte:window onkeydown={onKeydown} />

<div class="h-full p-2">
	<section class="panel relative flex h-full flex-col overflow-hidden">
		{#if diag.map}
			<div class="flex min-h-0 w-full flex-1 gap-px">
				<!-- svelte-ignore a11y_no_static_element_interactions -->
				<div
					class="h-full min-w-0 flex-1"
					data-pane-hover={hoveredPane === 0 ? 'on' : 'off'}
					onpointerenter={() => { hoveredPane = 0; }}
					onpointerleave={() => { if (hoveredPane === 0) hoveredPane = null; }}
				>
					<MapView
						pane={0}
						{compare}
						bind:sharedTime
						bind:sharedCamera
						bind:timeStrip
						bind:paneControls={controlsA}
						onToggleCompare={() => { compare = !compare; }}
					/>
				</div>
				{#if compare}
					<!-- A fresh MapView each time compare opens. A MapLibre
					     instance does not survive its container being
					     removed, so reusing one would hand the second pane
					     a torn-down style. -->
					<!-- svelte-ignore a11y_no_static_element_interactions -->
					<div
						class="h-full min-w-0 flex-1 border-l border-[var(--color-border-strong)]"
						data-pane-hover={hoveredPane === 1 ? 'on' : 'off'}
						onpointerenter={() => { hoveredPane = 1; }}
						onpointerleave={() => { if (hoveredPane === 1) hoveredPane = null; }}
					>
						<MapView
							pane={1}
							{compare}
							bind:sharedTime
							bind:sharedCamera
							bind:paneControls={controlsB}
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
					times={timeStrip.times}
					speed={timeStrip.speed}
					onFirst={timeStrip.onFirst}
					onPrev={timeStrip.onPrev}
					onTogglePlay={timeStrip.onTogglePlay}
					onNext={timeStrip.onNext}
					onLast={timeStrip.onLast}
					onScrub={timeStrip.onScrub}
					onSpeed={timeStrip.onSpeed}
					onHelp={() => { helpOpen = !helpOpen; }}
				/>
			{/if}
			{#if helpOpen}
				<!-- Centered over the panes rather than inside one, since the
				     shortcuts are the console's and not a pane's. -->
				<div
					data-help-panel
					class="absolute left-1/2 top-1/2 z-30 w-[22rem] -translate-x-1/2
					       -translate-y-1/2 rounded-sm border
					       border-[var(--color-border-strong)]
					       bg-[var(--color-surface)]/97 p-4 shadow-lg backdrop-blur"
					role="dialog"
					aria-label="Keyboard shortcuts"
				>
					<div class="mb-3 flex items-center justify-between">
						<span class="text-[10px] uppercase tracking-[0.18em]
						             text-[var(--color-muted)]">
							Keyboard Shortcuts
						</span>
						<button
							class="text-[11px] text-[var(--color-default)]
							       hover:text-[var(--color-bright)]"
							onclick={() => { helpOpen = false; }}
							title="Close (Esc)"
						>close</button>
					</div>
					<dl class="grid grid-cols-[8.5rem_1fr] gap-x-3 gap-y-1.5">
						{#each SHORTCUTS as s (s.keys)}
							<dt class="num text-[11px] text-[var(--color-bright)]">
								{s.keys}
							</dt>
							<dd class="text-[11px] text-[var(--color-default)]">
								{s.does}
							</dd>
						{/each}
					</dl>
				</div>
			{/if}
		{:else}
			<div class="flex h-full items-center justify-center text-[12px] uppercase
			            tracking-[0.18em] text-[var(--color-muted)]">
				map disabled · ?diag=no-map
			</div>
		{/if}
	</section>
</div>
