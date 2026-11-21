/**
 * Where each vector overlay sits in the paint order.
 *
 * Every overlay used to be added with `beforeId: 'radar-halo'`, which puts
 * each one immediately under the radar markers and leaves their order among
 * themselves to whichever fetch returned first. The visual baseline caught
 * it: `watershed-line` landed either side of `burn-scar-line` across runs of
 * identical code, so a reader saw watershed outlines over a burn perimeter
 * on one load and under it on the next.
 *
 * The order is declared here instead. Each layer is inserted before the
 * first layer above it that is already on the map, so a layer toggled on
 * late still lands in its declared place.
 *
 * Pure, so a runner can reach it. The caller owns the map and answers which
 * layers exist.
 */

/**
 * The vector overlays, bottom to top.
 *
 * Cartographic stacking: area fills under lines, lines under point markers,
 * and labels last so a marker never covers its own name. The burn-scar fill
 * is the only large translucent area, and anything under it would be read
 * through a wash of color.
 */
export const VECTOR_STACK = [
	'burn-scar-fill',
	'burn-scar-line',
	'watershed-line',
	'reservoir-marker',
	'stream-gauge-marker',
	'reservoir-label'
] as const;

/**
 * The lowest radar-marker layer.
 *
 * The vector overlays all sit under the markers, because an overlay drawn
 * over the radar dots makes them unreadable and unclickable, which is the
 * whole interaction on this map.
 */
export const MARKER_TOP = 'radar-halo';

/**
 * The `beforeId` that puts one overlay in its declared place.
 *
 * Returns the first layer above it in the stack that is already on the map.
 * With none of those present the markers hold the position, and with those
 * absent too the answer is undefined, which MapLibre reads as top of stack.
 * Early in a style swap the markers have not been added yet, and top is
 * right then because the markers go above afterwards.
 *
 * A `beforeId` naming a layer that is not on the map makes MapLibre throw,
 * so every candidate is checked before it is returned.
 *
 * @param id The layer being added. One not in the stack sits under the
 *     markers, which is where an unplaced overlay belongs.
 * @param hasLayer Whether a layer id is currently on the map.
 * @returns The id to insert before, or undefined for top of stack.
 */
export function beforeIdFor(
	id: string,
	hasLayer: (layerId: string) => boolean
): string | undefined {
	const at = (VECTOR_STACK as readonly string[]).indexOf(id);
	const above = at < 0 ? [] : VECTOR_STACK.slice(at + 1);
	for (const candidate of above) {
		if (hasLayer(candidate)) return candidate;
	}
	return hasLayer(MARKER_TOP) ? MARKER_TOP : undefined;
}
