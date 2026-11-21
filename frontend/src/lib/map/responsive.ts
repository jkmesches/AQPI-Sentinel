/**
 * What the console does when there is not enough room for all of it.
 *
 * The layer panel is 280 px, which is most of a phone. On a 390 px screen
 * it leaves 110 px of map, so a console that opens it by default opens
 * onto a sliver of weather behind a wall of checkboxes.
 *
 * The numbers here are the widths the layout actually breaks at, measured
 * rather than taken from a framework's breakpoint table. They are in one
 * place because the panel, the panes and the time strip have to agree:
 * two of them choosing different thresholds gives a band of widths where
 * the panes have stacked and the panel still thinks it has room.
 */

/**
 * Below this pane width, the panel covers too much of the map to open by
 * default.
 *
 * 560 px, which is where the 280 px panel stops being half the pane. Above
 * it the reader still sees as much map as panel, and below it the panel is
 * the larger of the two.
 */
export const PANEL_AUTOCLOSE_PX = 560;

/**
 * Below this page width, two panes side by side are not a comparison.
 *
 * 900 px gives each pane 450 px. Narrower than that leaves a pane under
 * the 280 px panel plus a usable map, so the comparison is between two
 * slivers. Stacking loses the north-south extent this network mostly
 * covers, so the panes stay side by side at every width above it.
 */
export const PANE_STACK_PX = 900;

/**
 * Below this width, the time strip puts its axis on its own row.
 *
 * 720 px. The timestamp, the transport, the speed and the help control
 * take about 360 px together, and an axis narrower than the 31 frames it
 * draws is a row of hairlines.
 */
export const STRIP_WRAP_PX = 720;

/**
 * Whether the layer panel should start closed.
 *
 * Only on a first visit. A reader who opened it on a narrow screen meant
 * to open it, and reversing that on every load would make the control
 * look broken.
 *
 * @param width The pane's width in CSS pixels.
 * @param hasStoredPrefs Whether this browser has been here before.
 * @returns True when the panel should start closed.
 */
export function shouldClosePanel(
	width: number,
	hasStoredPrefs: boolean
): boolean {
	if (hasStoredPrefs) return false;
	if (!Number.isFinite(width) || width <= 0) return false;
	return width < PANEL_AUTOCLOSE_PX;
}

/**
 * Whether the comparison panes should stack.
 *
 * @param width The page's width in CSS pixels.
 * @returns True when they should be one above the other.
 */
export function shouldStackPanes(width: number): boolean {
	if (!Number.isFinite(width) || width <= 0) return false;
	return width < PANE_STACK_PX;
}

/**
 * The narrowest page that can hold three panes in a row.
 *
 * 1280 px gives each pane 426 px, which is the same floor PANE_STACK_PX
 * sets for two: enough that a pane is more than its own panel once the
 * panel is open. Below it the console offers two, and a reader who had
 * three and narrowed the window drops to two rather than to three
 * useless slivers.
 */
export const THIRD_PANE_PX = 1280;

/** The most panes this console will open, and the fewest it will keep. */
export const MIN_PANES = 1;
export const MAX_PANES = 3;

/**
 * How many panes this width can carry.
 *
 * @param width The page width in CSS pixels.
 * @returns 2 on a narrow page and 3 on a wide one, never less than 1.
 */
export function maxPanesFor(width: number): number {
	if (!Number.isFinite(width) || width <= 0) return 2;
	return width >= THIRD_PANE_PX ? MAX_PANES : 2;
}

/**
 * How many panes to show, given what is open and what fits.
 *
 * Narrowing the window takes a pane away rather than letting three share
 * 900 px, and widening it again does not bring one back: a pane that
 * reappeared on a resize would reopen a map the reader had closed.
 *
 * @param open How many panes the reader has asked for.
 * @param width The page width in CSS pixels.
 * @returns The count to render.
 */
export function panesToShow(open: number, width: number): number {
	const want = Math.max(MIN_PANES, Math.min(MAX_PANES, Math.trunc(open) || 1));
	return Math.min(want, maxPanesFor(width));
}

/**
 * Whether the time strip should put its axis on its own row.
 *
 * @param width The strip's width in CSS pixels.
 * @returns True when the axis needs the full width.
 */
export function shouldWrapStrip(width: number): boolean {
	if (!Number.isFinite(width) || width <= 0) return false;
	return width < STRIP_WRAP_PX;
}
