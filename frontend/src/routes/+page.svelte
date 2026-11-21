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
	import { onDestroy } from 'svelte';
	import {
		paneForKey, type PaneControls, type PaneGate
	} from '$lib/map/paneControls';
	import {
		arrive, drain, isWaiting, newGate, setExpected, GATE_WAIT_MS
	} from '$lib/map/frameGate';
	import { actionForKey, panOffset, SHORTCUTS, ZOOM_STEP } from '$lib/map/keys';
	import { shouldStackPanes } from '$lib/map/responsive';
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
	// Two 200 px panes are not a comparison, so below the threshold in
	// $lib/map/responsive they stack. Side by side loses nothing above it,
	// and stacking costs the north-south extent this network covers.
	let pageWidth = $state(0);
	const stacked = $derived(compare && shouldStackPanes(pageWidth));

	// The frame rendezvous. Each pane already swaps its own layers
	// together; without this the pane that loads first paints the new
	// minute while the other is still fetching, so a comparison shows one
	// product a frame ahead of the other. See $lib/map/frameGate.
	const gate = newGate();
	let gateTimer: ReturnType<typeof setTimeout> | undefined;

	// Counted so the barrier can be measured rather than assumed. A gate
	// whose releases are nearly all timeouts is not holding the panes
	// together, it is delaying one of them, and the two look identical
	// from outside. Published under ?diag=probe only, like the map's own
	// snapshot: a page that handed every visitor a lever on this would be
	// exposing a surface meant to be read.
	const gateStats = {
		released: 0, together: 0, timedOut: 0, waitedMs: 0,
		/** Milliseconds between the two panes' arrivals, newest last. */
		gaps: [] as number[]
	};
	/** When each pane last arrived, for measuring the stagger. */
	const lastArrival = new Map<number, number>();
	/** Kept short: this is a diagnostic, not a log. */
	const MAX_GAPS = 40;

	/** Fire whatever is waiting and forget the timer. */
	function releaseGate(): void {
		if (gateTimer !== undefined) {
			clearTimeout(gateTimer);
			gateTimer = undefined;
		}
		const held = drain(gate);
		if (held.length > 0) {
			gateStats.released += 1;
			gateStats.timedOut += 1;
		}
		for (const apply of held) apply();
	}

	const frameGate: PaneGate = {
		arrive(pane, applies) {
			const at = performance.now();
			// The stagger between the panes, measured whether or not the
			// gate managed to release them together. The wait below is set
			// from this, so it has to be visible even when the wait is too
			// short for it.
			for (const [other, when] of lastArrival) {
				if (other !== pane) {
					gateStats.gaps.push(Math.round(at - when));
					if (gateStats.gaps.length > MAX_GAPS) gateStats.gaps.shift();
				}
			}
			lastArrival.set(pane, at);
			const waiting = isWaiting(gate);
			const ready = arrive(gate, pane, applies, at);
			if (ready) {
				if (gateTimer !== undefined) {
					clearTimeout(gateTimer);
					gateTimer = undefined;
				}
				gateStats.released += 1;
				if (waiting) {
					gateStats.together += 1;
					gateStats.waitedMs += Math.round(performance.now() - at);
				}
				for (const apply of ready) apply();
				return;
			}
			// A pane whose product has no frame inside the shared window
			// never arrives at all, so the wait has to end on a clock
			// rather than on the other pane.
			if (gateTimer === undefined) {
				gateTimer = setTimeout(releaseGate, GATE_WAIT_MS);
			}
		}
	};

	// The pane count is what the gate waits for, and a pane that has
	// closed will never arrive, so anything held for it is released here.
	$effect(() => {
		const held = setExpected(gate, compare ? 2 : 1);
		for (const apply of held) apply();
	});

	$effect(() => {
		if (!diag.probe || typeof window === 'undefined') return;
		(window as unknown as { __sentinelGate?: () => typeof gateStats })
			.__sentinelGate = () => ({ ...gateStats });
	});

	onDestroy(() => {
		if (gateTimer !== undefined) clearTimeout(gateTimer);
	});

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
			// The list first, then the measure tool, so one Escape closes
			// whatever is most recently in the way rather than both.
			if (helpOpen) { helpOpen = false; return true; }
			return paneForKey(panes, hoveredPane)?.cancelMeasure() ?? false;
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
		if (action === 'measure') { target.toggleMeasure(); return true; }
		if (action === 'measure-undo') { target.undoMeasure(); return true; }
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
<svelte:window onkeydown={onKeydown} bind:innerWidth={pageWidth} />

<div class="h-full p-2">
	<section class="panel relative flex h-full flex-col overflow-hidden">
		{#if diag.map}
			<div
				class="flex min-h-0 w-full flex-1 gap-px {stacked
					? 'flex-col'
					: 'flex-row'}"
				data-pane-layout={stacked ? 'stacked' : 'side-by-side'}
			>
				<!-- svelte-ignore a11y_no_static_element_interactions -->
				<div
					class="min-h-0 min-w-0 flex-1 {stacked ? 'w-full' : 'h-full'}"
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
						{frameGate}
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
						class="min-h-0 min-w-0 flex-1 {stacked
							? 'w-full border-t'
							: 'h-full border-l'} border-[var(--color-border-strong)]"
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
							{frameGate}
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
