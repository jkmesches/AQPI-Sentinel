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
	import MapToolbar from '$lib/components/MapToolbar.svelte';
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
	import {
		shouldStackPanes, panesToShow, maxPanesFor, shouldClosePanel
	} from '$lib/map/responsive';
	import { diag } from '$lib/diag';
	import { PIN_LIMIT, type Pin } from '$lib/map/probe';
	import {
		DEFAULT_UI_SCALE, normalizeScale, readScale, writeScale
	} from '$lib/map/uiScale';

	// How many panes the reader has asked for, and how many fit. A third
	// pane needs a wide page, so the window can take one away; see
	// $lib/map/responsive.
	let panesWanted = $state(1);
	// Owned here because both panes read them and one pane writes each.
	let sharedTime = $state<string | null>(null);
	let sharedCamera = $state<Camera | null>(null);
	// Published by the primary pane, drawn here.
	let timeStrip = $state<TimeStrip | null>(null);
	// Published by each pane, driven from here. One slot per possible
	// pane, so a pane's controls land in its own index rather than in a
	// list whose order depends on which mounted first.
	let paneSlots = $state<(PaneControls | null)[]>([null, null, null]);
	// Whether clicking the map pins a value probe. Console-wide, because
	// the toolbar that turns it on is, and a reader who turned it on
	// means it for whichever map they then click.
	let probing = $state(false);
	// The ground point under the pointer, shared so every pane marks the
	// same place. Two panes showing one spot are comparing it; two
	// showing a crosshair each under its own pointer are not.
	let sharedCursor = $state<{ lng: number; lat: number } | null>(null);
	// Whether the panes move as one and mark one cursor. Unlinked, each pane
	// pans and zooms on its own and draws only its own pointer, while time
	// and pins stay shared. Back to linked whenever one pane is left, so a
	// pane opened later is locked as the + button says.
	let panesLinked = $state(true);
	// The points the reader has pinned. Console-wide, so one pin marks
	// one place in every pane and each pane reports its own product
	// there; see $lib/map/probe.
	let sharedPins = $state<Pin[]>([]);
	// How large every pane draws the things laid over its map. Console-wide
	// and persisted per browser: a reader who sized the legend for the
	// display in front of them meant it for the next visit as well.
	let uiScale = $state(DEFAULT_UI_SCALE);
	// Whether the dock is showing, and which pane it drives. One dock
	// beside the panes rather than one inside each: three copies of these
	// controls would take more of the console than the maps do.
	let dockOpen = $state(true);
	let dockPane = $state(0);
	const DOCK_KEY = 'sentinel-dock-open';
	$effect(() => {
		uiScale = normalizeScale(readScale());
		try {
			// Closed on a narrow first visit, where 300 px of dock leaves a
			// sliver of map. Only on a first visit: a reader who opened it
			// on a narrow screen meant to.
			const kept = localStorage.getItem(DOCK_KEY);
			dockOpen = kept === null
				? !shouldClosePanel(window.innerWidth,
				                    localStorage.getItem('sentinel-map-prefs') !== null)
				: kept !== 'closed';
		} catch { /* storage unavailable — open is the right default */ }
	});

	/**
	 * Show or hide the dock, and keep the choice.
	 */
	function toggleDock(): void {
		dockOpen = !dockOpen;
		try {
			localStorage.setItem(DOCK_KEY, dockOpen ? 'open' : 'closed');
		} catch { /* a browser that will not store it still has to run */ }
	}
	/**
	 * Keep the chosen size for the next visit.
	 *
	 * @param next The size the reader picked.
	 */
	function setScale(next: number): void {
		uiScale = normalizeScale(next);
		writeScale(uiScale);
	}
	// Which pane the pointer is over, for the keys that belong to one pane.
	let hoveredPane = $state<number | null>(null);
	let helpOpen = $state(false);
	// Two 200 px panes are not a comparison, so below the threshold in
	// $lib/map/responsive they stack. Side by side loses nothing above it,
	// and stacking costs the north-south extent this network covers.
	let pageWidth = $state(0);
	const paneCount = $derived(panesToShow(panesWanted, pageWidth));
	const maxPanes = $derived(maxPanesFor(pageWidth));
	// `compare` to a pane is "you are one of several", which is what
	// decides its label and whether it shares a camera. It does not mean
	// two any more.
	const compare = $derived(paneCount > 1);
	// A pane the reader closed cannot keep the dock, and the dock cannot
	// point at nothing, so it falls back to the first.
	const dockTarget = $derived(dockPane < paneCount ? dockPane : 0);
	const stacked = $derived(compare && shouldStackPanes(pageWidth));
	$effect(() => {
		if (paneCount <= 1) panesLinked = true;
	});

	/**
	 * Link the panes, or let each move on its own.
	 *
	 * Linking gathers every pane on the view of the pane the dock drives.
	 * The shared camera stopped moving while they were apart, and jumping
	 * them all back to it would undo whatever the reader just looked at.
	 * The cursor is cleared both ways, so no pane keeps a mark from before.
	 */
	function toggleLink(): void {
		if (!panesLinked) {
			const here = paneSlots[dockTarget]?.camera() ?? null;
			if (here) sharedCamera = here;
		}
		sharedCursor = null;
		panesLinked = !panesLinked;
	}

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
		const held = setExpected(gate, paneCount);
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
	// those close over a map that MapLibre has torn down. Reading only the
	// open slots keeps a dead map out of the dispatch.
	const panes = $derived(
		paneSlots.map((c, i) => (i < paneCount ? c : null)));
	// The pane a toolbar press acts on: the one under the pointer, or the
	// first when the pointer is on the toolbar itself, which is where it
	// has to be to press anything.
	const focused = $derived(paneForKey(panes, hoveredPane));

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
		// The key still toggles between one pane and two, which is
		// what a single shortcut can mean. Three is a toolbar press.
		if (action === 'compare') {
			panesWanted = paneCount > 1 ? 1 : 2;
			return true;
		}

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
		if (action === 'panel') { toggleDock(); return true; }
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
			<MapToolbar
				panes={paneCount}
				{maxPanes}
				measuring={focused?.measuring() ?? false}
				exporting={focused?.exporting() ?? false}
				{probing}
				{uiScale}
				pins={sharedPins.length}
				pinLimit={PIN_LIMIT}
				onclearpins={() => { sharedPins = []; }}
				onscale={setScale}
				linked={panesLinked}
				onlink={toggleLink}
				onaddpane={() => { panesWanted = Math.min(maxPanes, paneCount + 1); }}
				onremovepane={() => { panesWanted = Math.max(1, paneCount - 1); }}
				onmeasure={() => focused?.toggleMeasure()}
				onprobe={() => { probing = !probing; }}
				onexport={() => focused?.exportImage()}
				onreset={() => {
					for (const c of panes) c?.reset();
					// Back to one pane too. Reset means the console as it
					// opens, and it opens with one map; leaving three open
					// made the one control that promises a known state
					// the one that did not deliver it.
					panesWanted = 1;
					panesLinked = true;
					dockPane = 0;
					sharedPins = [];
					probing = false;
				}}
			/>
			<div class="flex min-h-0 w-full flex-1">
			<!-- THE DOCK. Every control a pane has, expanded, beside the
			     maps rather than over them. The panel it replaces was
			     280 px of checkboxes laid on top of the weather, and it
			     was laid on top of all three panes at once.
			     data-panel says which way it is, so a harness driving the
			     `[` key reads the state rather than inferring it. -->
			<aside
				data-panel={dockOpen ? 'open' : 'closed'}
				class="flex shrink-0 flex-col border-r border-[var(--color-border-strong)] bg-[var(--color-surface)] {dockOpen
					? 'w-[min(19rem,45vw)]'
					: 'w-8'}"
			>
				<div
					class="flex items-center gap-1 border-b border-[var(--color-border)] px-2 py-1.5"
					class:justify-center={!dockOpen}
				>
					{#if dockOpen}
						{#if compare}
							<!-- Which pane the dock drives. Each pane owns its
							     own product, radars and overlays, which is why
							     a reader opens a second one. -->
							<span class="label mr-1 tracking-[0.2em] text-[var(--color-faint)]">
								pane
							</span>
							{#each Array.from({ length: paneCount }, (_, i) => i) as i (i)}
								<button
									data-dock-tab={i}
									aria-pressed={dockTarget === i}
									title="drive pane {'ABC'[i]} from the dock"
									class="num h-5 w-5 rounded-sm border text-[12px] transition-colors {dockTarget === i
										? 'border-[var(--color-ok)] bg-[var(--color-ok)]/20 text-[var(--color-bright)]'
										: 'border-[var(--color-border-strong)] text-[var(--color-muted)] hover:text-[var(--color-bright)]'}"
									onclick={() => { dockPane = i; }}
								>{'ABC'[i]}</button>
							{/each}
						{:else}
							<span class="label tracking-[0.2em] text-[var(--color-default)]">
								Display
							</span>
						{/if}
					{/if}
					<button
						data-dock-toggle
						class="ml-auto inline-flex h-5 w-5 items-center justify-center rounded-sm text-[var(--color-muted)] hover:bg-[var(--color-elevated)] hover:text-[var(--color-bright)]"
						class:ml-0={!dockOpen}
						title={dockOpen ? 'hide the controls ([)' : 'show the controls ([)'}
						aria-label={dockOpen ? 'hide the controls' : 'show the controls'}
						onclick={toggleDock}
					>
						<svg viewBox="0 0 10 10" width="10" height="10" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
							{#if dockOpen}
								<polyline points="6.5,1.5 3,5 6.5,8.5" />
							{:else}
								<polyline points="3.5,1.5 7,5 3.5,8.5" />
							{/if}
						</svg>
					</button>
				</div>
				<!-- Where each pane's controls land. The pane draws them and
				     moves them here, so they keep reading that pane's state
				     rather than a copy of it; see $lib/map/portal. -->
				<div
					data-dock-slot
					class="min-h-0 flex-1 overflow-y-auto"
					class:hidden={!dockOpen}
				></div>
			</aside>

			<div
				class="flex min-h-0 w-full flex-1 gap-px {stacked
					? 'flex-col'
					: 'flex-row'}"
				data-pane-layout={stacked ? 'stacked' : 'side-by-side'}
			>
				<!-- A fresh MapView per pane, keyed on the index. A MapLibre
				     instance does not survive its container being removed,
				     so reusing one would hand a reopened pane a torn-down
				     style. -->
				{#each Array.from({ length: paneCount }, (_, i) => i) as i (i)}
					<!-- svelte-ignore a11y_no_static_element_interactions -->
					<div
						class="min-h-0 min-w-0 flex-1 {stacked ? 'w-full' : 'h-full'} {i > 0
							? (stacked ? 'border-t' : 'border-l')
							: ''} border-[var(--color-border-strong)]"
						data-pane-hover={hoveredPane === i ? 'on' : 'off'}
						onpointerenter={() => { hoveredPane = i; }}
						onpointerleave={() => { if (hoveredPane === i) hoveredPane = null; }}
					>
						<MapView
							pane={i}
							{compare}
							linked={panesLinked}
							{probing}
							{uiScale}
							bind:sharedPins
							inDock={dockOpen && dockTarget === i}
							bind:sharedTime
							bind:sharedCamera
							bind:sharedCursor
							bind:timeStrip={
								() => (i === 0 ? timeStrip : null),
								(v) => { if (i === 0) timeStrip = v; }
							}
							bind:paneControls={
								() => paneSlots[i],
								(v) => { paneSlots[i] = v; }
							}
							{frameGate}
						/>
					</div>
				{/each}
			</div>
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
						<span class="text-[12px] uppercase tracking-[0.18em]
						             text-[var(--color-muted)]">
							Keyboard Shortcuts
						</span>
						<button
							class="text-[12px] text-[var(--color-default)]
							       hover:text-[var(--color-bright)]"
							onclick={() => { helpOpen = false; }}
							title="Close (Esc)"
						>close</button>
					</div>
					<dl class="grid grid-cols-[8.5rem_1fr] gap-x-3 gap-y-1.5">
						{#each SHORTCUTS as s (s.keys)}
							<dt class="num text-[12px] text-[var(--color-bright)]">
								{s.keys}
							</dt>
							<dd class="text-[12px] text-[var(--color-default)]">
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
