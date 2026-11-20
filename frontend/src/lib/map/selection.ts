/**
 * Which radars are selected, and where that set sits on the map.
 *
 * Pure, so a runner can reach it. The console frames the camera on whatever
 * a preset selects, and the two faults available here both look like a
 * working map: a camera that jumps somewhere plausible but wrong, and a
 * selection that silently holds a radar twice.
 *
 * The caller owns the camera and the overlays. This module answers what the
 * set is and what box contains it.
 */
import { radarExtent, type RadarSite } from '$lib/radarGeo';

/** A geographic box, west/east/south/north in degrees. */
export interface Bounds {
	west: number;
	east: number;
	south: number;
	north: number;
}

/**
 * The ids with duplicates removed, in first-seen order.
 *
 * Order is kept because the overlay stack is drawn in selection order, and a
 * Set reordered by insertion would reshuffle which radar draws on top.
 *
 * @param ids Radar ids, possibly with repeats.
 * @returns A fresh array, so a caller reassigning state gets a new reference.
 */
export function uniqueIds(ids: readonly string[]): string[] {
	return [...new Set(ids)];
}

/**
 * The selection with one radar added or removed.
 *
 * Appends to the end rather than restoring an earlier position, so a radar
 * toggled off and on again draws on top. That matches what a reader expects
 * from clicking it last.
 *
 * @param ids The current selection.
 * @param id The radar to flip.
 * @returns A fresh array.
 */
export function toggleId(ids: readonly string[], id: string): string[] {
	return ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id];
}

/**
 * The box containing every selected radar's range ring.
 *
 * Null for an empty selection, which the caller reads as "leave the camera
 * alone". Seeding a fold with Infinity and returning the result unchecked
 * would hand `fitBounds` an inverted box and send the camera to the far side
 * of the world.
 *
 * @param radars Every radar the deployment draws.
 * @param ids The selected ids. Ids with no matching radar are ignored.
 * @returns The union of the selected radars' extents, or null.
 */
export function selectionBounds(
	radars: readonly RadarSite[],
	ids: readonly string[]
): Bounds | null {
	const wanted = new Set(ids);
	const chosen = radars.filter((r) => wanted.has(r.id));
	if (chosen.length === 0) return null;
	const boxes = chosen.map(radarExtent);
	return {
		west: Math.min(...boxes.map((b) => b.west)),
		east: Math.max(...boxes.map((b) => b.east)),
		south: Math.min(...boxes.map((b) => b.south)),
		north: Math.max(...boxes.map((b) => b.north))
	};
}
