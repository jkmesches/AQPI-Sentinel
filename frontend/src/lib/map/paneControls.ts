/**
 * What a pane publishes so the page can drive it from the keyboard.
 *
 * The listener belongs to the page and not to the pane. Two panes each
 * holding their own keydown listener would both answer one press, so a step
 * key would move time twice and a product key would switch both panes.
 *
 * The time keys already have a single owner in `TimeStrip`, which only the
 * primary pane publishes. These are the acts that belong to one pane rather
 * than to the console, so the page sends them to the pane the pointer is
 * over and falls back to the primary when it is over neither.
 */

import type { FrameApply } from './layers/registry';

/** How a pane hands its frame swaps to the page's rendezvous. */
export interface PaneGate {
	/**
	 * This pane's frame is ready.
	 *
	 * The page decides when it is painted, which is once every open pane
	 * has arrived or once the wait has run out.
	 *
	 * @param pane Which pane.
	 * @param applies Its swaps.
	 */
	arrive: (pane: number, applies: readonly FrameApply[]) => void;
}

/** A pane's size in CSS pixels. */
export interface PaneSize {
	width: number;
	height: number;
}

/** The per-pane acts a key press can ask for. */
export interface PaneControls {
	/** Move the product selection by one, wrapping at either end. */
	stepProduct: (step: number) => void;
	/** Show or hide this pane's layer panel. */
	togglePanel: () => void;
	/** Pan by a screen offset in CSS pixels. */
	panBy: (dx: number, dy: number) => void;
	/** Change the zoom by a delta, clamped by the map's own limits. */
	zoomBy: (dz: number) => void;
	/** The map's current size, for a pan that scales with what is shown. */
	size: () => PaneSize;
	/** Start or stop measuring, clearing the path on the way out. */
	toggleMeasure: () => void;
	/** Drop the last measured point, or leave the tool if there is none. */
	undoMeasure: () => void;
	/**
	 * Stop measuring if it is running.
	 *
	 * Reports whether it did anything, so the page can let Escape fall
	 * through to whatever else it closes rather than swallowing the key.
	 */
	cancelMeasure: () => boolean;
}

/**
 * Which pane a press should act on.
 *
 * The pointer decides, because with two maps on screen there is no other
 * signal for which one someone means, and clicking a map to read a value
 * already moves the pointer there. A pane that has gone away between the
 * pointer leaving and the key arriving falls back to the primary, so a
 * press is never dropped silently.
 *
 * @param panes The published controls, indexed by pane number.
 * @param hovered The pane the pointer is over, or null.
 * @returns The controls to act on, or null when no pane has published any.
 */
export function paneForKey(
	panes: readonly (PaneControls | null)[],
	hovered: number | null
): PaneControls | null {
	if (hovered !== null) {
		const target = panes[hovered];
		if (target) return target;
	}
	return panes[0] ?? null;
}
