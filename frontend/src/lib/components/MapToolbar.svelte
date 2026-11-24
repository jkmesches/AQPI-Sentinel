<!--
  The console's tools, across the top.

  These sat in the layers panel, which made them the left pane's tools as
  soon as a second pane opened: a panel is attached to one map, and
  measure and the value probe act on whichever map you point at. Across
  the top they read as console chrome, and the pane under the pointer
  takes the action. That also survives a third pane, where a rail down
  one edge would be pointing at the wrong map two thirds of the time.

  The buttons report the state of the pane the pointer is over, so a
  toolbar beside two panes is never claiming that both are measuring.
-->
<script lang="ts">
	import {
		DEFAULT_UI_SCALE, UI_SCALES, scaleName
	} from '$lib/map/uiScale';

	interface Props {
		/** How many panes are open. */
		panes: number;
		/** The most panes this console will open. */
		maxPanes: number;
		/** Whether the pane in focus is measuring. */
		measuring: boolean;
		/** Whether a save is running on the pane in focus. */
		exporting: boolean;
		/** Whether the value probe is on. */
		probing: boolean;
		/** How many points the reader has pinned. */
		pins?: number;
		/** The most the map will hold. */
		pinLimit?: number;
		/** The size every pane draws its overlays at. */
		uiScale?: number;
		/** Whether the panes share one camera and one cursor. */
		linked?: boolean;
		onaddpane: () => void;
		onremovepane: () => void;
		/** Called to link the panes, or to let each move on its own. */
		onlink?: () => void;
		onmeasure: () => void;
		onprobe: () => void;
		onexport: () => void;
		onreset: () => void;
		/** Called to take every pinned probe off the map. */
		onclearpins: () => void;
		/** Called with the size the reader picked. */
		onscale: (scale: number) => void;
	}
	let {
		panes, maxPanes, measuring, exporting, probing, pins = 0,
		pinLimit = 0,
		uiScale = DEFAULT_UI_SCALE,
		linked = true,
		onaddpane, onremovepane, onmeasure, onprobe, onexport, onreset, onscale,
		onclearpins, onlink = () => {}
	}: Props = $props();

	let settingsOpen = $state(false);

	// Closes the settings panel on a click anywhere else. It sits over the
	// first pane's map, so a reader who has finished with it clicks the
	// map, not the control that opened it.
	$effect(() => {
		if (typeof window === 'undefined' || !settingsOpen) return;
		const away = (e: PointerEvent) => {
			const el = e.target as HTMLElement | null;
			if (!el?.closest('[data-settings-menu]')) settingsOpen = false;
		};
		const esc = (e: KeyboardEvent) => {
			if (e.key === 'Escape') settingsOpen = false;
		};
		window.addEventListener('pointerdown', away, true);
		window.addEventListener('keydown', esc);
		return () => {
			window.removeEventListener('pointerdown', away, true);
			window.removeEventListener('keydown', esc);
		};
	});

	const base = 'border px-2 py-0.5 text-[12px] uppercase tracking-wider '
		+ 'transition-colors disabled:opacity-40';
	const off = 'border-[var(--color-border-strong)] text-[var(--color-muted)] '
		+ 'hover:bg-[var(--color-elevated)] hover:text-[var(--color-bright)]';
	const on = 'border-[var(--color-ok)] bg-[var(--color-ok)]/20 '
		+ 'text-[var(--color-bright)]';
</script>

<div
	data-map-toolbar
	class="flex flex-wrap items-center gap-1.5 border-b border-[var(--color-border)] bg-[var(--color-surface)]/95 px-3 py-1.5"
>
	<span class="label mr-1 tracking-[0.2em] text-[var(--color-faint)]">Tools</span>

	<button
		data-measure-toggle
		class="{base} {measuring ? on : off}"
		title={measuring
			? 'stop measuring (Esc)'
			: 'measure distance and bearing between points (M)'}
		aria-pressed={measuring}
		onclick={onmeasure}
	>measure</button>

	<button
		data-probe-toggle
		class="{base} {probing ? on : off}"
		title={probing
			? 'stop dropping value probes'
			: 'click the map to pin the value under a point'}
		aria-pressed={probing}
		onclick={onprobe}
	>probe{pins > 0 ? ` ${pins}` : ''}</button>

	<!-- Only once there is something to clear. A control that is always
	     there and does nothing most of the time is one a reader stops
	     reading. -->
	{#if pins > 0}
		<button
			data-probe-clear
			class="{base} {off}"
			title="take all {pins} pinned {pins === 1 ? 'probe' : 'probes'} off the map{pins >= pinLimit
				? `; the map holds ${pinLimit}, and the next pin drops the oldest`
				: ''}"
			aria-label="clear pinned probes"
			onclick={onclearpins}
		>clear</button>
	{/if}

	<button
		data-export
		class="{base} {off}"
		title="save a PNG with the timestamp and legend burned in"
		disabled={exporting}
		onclick={onexport}
	>{exporting ? '...' : 'export'}</button>

	<button
		class="{base} {off}"
		title="reset radars, products, layers and view to defaults"
		aria-label="reset map settings"
		onclick={onreset}
	>reset</button>

	<!-- Where the pane that fills the dock draws the place search. The
	     slot is here and the control is the pane's, so the field keeps
	     reading that pane's own datasets; see $lib/map/portal. -->
	<span class="mx-1 h-3 w-px bg-[var(--color-border-strong)]"></span>
	<div data-toolbar-search-slot class="flex items-center"></div>

	<span class="mx-1 h-3 w-px bg-[var(--color-border-strong)]"></span>

	<!-- Panes as a count rather than a compare switch. Two was a toggle;
	     three needs a way to say which way you are going. -->
	<span class="num text-[12px] text-[var(--color-faint)]">panes</span>
	<button
		data-pane-remove
		class="{base} {off}"
		title="close a pane"
		aria-label="close a pane"
		disabled={panes <= 1}
		onclick={onremovepane}
	>&minus;</button>
	<span data-pane-count class="num w-3 text-center text-[12px] text-[var(--color-default)]">
		{panes}
	</span>
	<button
		data-pane-add
		class="{base} {off}"
		title={linked
			? 'open another pane, locked to the others in time and view'
			: 'open another pane, locked to the others in time only'}
		aria-label="open another pane"
		disabled={panes >= maxPanes}
		onclick={onaddpane}
	>+</button>
	<!-- Only with more than one pane, since one pane has nothing to link.
	     Named for what a press will do, and pressed-looking while the
	     panes are apart, so the state reads off the button either way. -->
	{#if panes > 1}
		<button
			data-pane-link={linked ? 'linked' : 'unlinked'}
			class="{base} {linked ? off : on} ml-1"
			aria-pressed={!linked}
			title={linked
				? 'Unlink the panes: each pans and zooms on its own and draws only its own cursor. Time stays shared.'
				: 'Link the panes again: every pane moves to the view of the pane the dock is driving, and the cursor shows in each.'}
			onclick={onlink}
		>{linked ? 'unlink panes' : 'link panes'}</button>
	{/if}

	<!-- Settings, at the far end. The console's own preferences rather
	     than one pane's display, so they belong here and not on a pane's
	     bar, where three panes would carry three copies of one setting. -->
	<div data-settings-menu class="relative ml-auto">
		<button
			data-settings-toggle
			class="{base} {settingsOpen ? on : off}"
			title="console settings"
			aria-label="console settings"
			aria-expanded={settingsOpen}
			onclick={() => { settingsOpen = !settingsOpen; }}
		>settings</button>

		<!-- Mounted whether or not it is open, because the opacity sliders
		     are published into it from the pane that owns them and a
		     portal cannot place a node in a host that is not there yet.
		     Hidden with the attribute rather than torn down, so the
		     sliders keep their values across an open and a close. -->
		<div
			data-settings-panel
			hidden={!settingsOpen}
			class="absolute right-0 top-[calc(100%+0.35rem)] z-40 w-[17rem] rounded-sm border border-[var(--color-border-strong)] bg-[var(--color-surface)]/97 p-3 shadow-xl backdrop-blur"
		>
				<div class="label tracking-[0.2em] text-[var(--color-default)]">
					Element size
				</div>
				<!-- One size for the legend, the compass, the display bar and
				     the cursor readout together. Each overlay with its own
				     size control is a console whose overlays never agree. -->
				<p class="mt-1 text-[12px] leading-tight text-[var(--color-faint)]">
					How large the legend, compass, display bar and cursor
					readout are drawn, in every pane.
				</p>
				<div
					class="mt-2 flex gap-px overflow-hidden rounded-sm border border-[var(--color-border-strong)]"
				>
					{#each UI_SCALES as s (s)}
						<button
							data-ui-scale={s}
							aria-pressed={uiScale === s}
							title="{scaleName(s)} ({Math.round(s * 100)}%)"
							class="num flex-1 py-1 text-[12px] font-medium transition-colors {uiScale === s
								? 'bg-[var(--color-ok)]/20 text-[var(--color-bright)] ring-1 ring-inset ring-[var(--color-ok)]/70'
								: 'text-[var(--color-muted)] hover:bg-[var(--color-elevated)]/50 hover:text-[var(--color-default)]'}"
							onclick={() => onscale(s)}
						>{scaleName(s)}</button>
					{/each}
				</div>
				<div
					data-ui-scale-current
					class="num mt-1.5 text-[12px] text-[var(--color-muted)]"
				>
					{scaleName(uiScale)} · {Math.round(uiScale * 100)}%
				</div>

				<!-- Where the pane publishes its opacity sliders. They were a
				     group in the dock, which put a control a reader touches
				     a few times a session in the same column as the product
				     and the radar list, which they touch constantly. -->
				<div
					class="label mt-3 border-t border-[var(--color-border)] pt-3 tracking-[0.2em] text-[var(--color-default)]"
				>
					Layer opacity
				</div>
				<p class="mt-1 text-[12px] leading-tight text-[var(--color-faint)]">
					How hard each raster layer is painted in the pane you have
					selected. The three stack on the same ground.
				</p>
				<div data-settings-slot class="mt-2"></div>
		</div>
	</div>
</div>
