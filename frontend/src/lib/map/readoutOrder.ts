/**
 * Which image overlay the cursor readout reads, by the order the map paints them.
 *
 * The readout has to report the pixel the reader is looking at. The image
 * overlays (the composite, a tilt, each radar) all sit in one band under the
 * radar markers, in the order they were added, so a radar selected after the
 * composite draws over it. Until 2026-11-23 the readout read only the
 * composite whenever one was on, through the composite's ramp, and a radar's
 * own returns drawn over it read as nothing.
 *
 * So the readout walks the overlays top down, each through its own ramp, and
 * stops at the first one painted under the cursor. A transparent pixel lets
 * the reader see the layer below, and the walk goes on to it.
 *
 * Pure, so a runner can reach it. The caller owns the map and the frames.
 */

/** One overlay the readout can read, named by its map layer. */
export interface Paintable {
	layerId: string;
}

/**
 * Order the readable overlays top first, dropping any not on the map.
 *
 * A layer that is not on the map is not on screen, and its held frame would
 * answer for imagery nobody can see. The tilt radar's own overlay is one,
 * since the tilt replaces it on the map while its frame stays held.
 *
 * @param layers The candidates, in any order.
 * @param order The map's layer ids, bottom to top, as `getLayersOrder` gives them.
 * @returns The candidates that are on the map, topmost first.
 */
export function topFirst<T extends Paintable>(
	layers: readonly T[],
	order: readonly string[]
): T[] {
	const at = new Map(order.map((id, i) => [id, i] as const));
	return layers
		.filter((l) => at.has(l.layerId))
		.sort((a, b) => (at.get(b.layerId) ?? 0) - (at.get(a.layerId) ?? 0));
}

/**
 * Read down through the overlays until one is painted at the point.
 *
 * @param layers The overlays, topmost first.
 * @param read Reads one overlay at the point, null where it is transparent
 *     or the point is off its frame. Any other answer, the off-scale text
 *     included, is what the reader sees there and ends the walk.
 * @returns The first reading and the overlay that gave it, or null.
 */
export function firstReading<T, R>(
	layers: readonly T[],
	read: (layer: T) => R | null
): { layer: T; reading: R } | null {
	for (const layer of layers) {
		const reading = read(layer);
		if (reading !== null) return { layer, reading };
	}
	return null;
}
