/**
 * Points the reader has pinned to watch a value at.
 *
 * The cursor readout answers for wherever the pointer is, which means a
 * reader comparing two places has to remember the first number while
 * moving to the second, and a reader scrubbing time cannot hold a point
 * at all: the value under a still pointer changes with every frame and
 * there is nothing to compare it against.
 *
 * A pin is a ground point, not a screen point and not a value. Every
 * pane marks the same ground and reads its own product there, and the
 * reading is taken again on each frame, so scrubbing time at a pinned
 * point is what the pin is for.
 */

/** One pinned ground point. */
export interface Pin {
	/** Stable across reorders, so a removal takes the one that was clicked. */
	id: string;
	lng: number;
	lat: number;
}

/**
 * How many pins the map will hold.
 *
 * Twelve. Each draws a marker and a number in every open pane, so three
 * panes already put 36 labels on screen, and past that the pins cover
 * the imagery they are reporting on.
 */
export const PIN_LIMIT = 12;

/**
 * Add a pin, dropping the oldest once the map is full.
 *
 * Dropping rather than refusing, because a reader at the limit is
 * pinning a thirteenth point on purpose and a click that does nothing
 * reads as a broken tool.
 *
 * @param pins The pins already on the map.
 * @param lng Longitude in degrees.
 * @param lat Latitude in degrees.
 * @param id The new pin's id, which must not already be in use.
 * @returns A new list. The old one is returned unchanged for a point
 *     that is not on the globe or an id that is already taken.
 */
export function addPin(
	pins: readonly Pin[], lng: number, lat: number, id: string
): Pin[] {
	if (!Number.isFinite(lng) || !Number.isFinite(lat)) return [...pins];
	if (Math.abs(lat) > 90 || Math.abs(lng) > 180) return [...pins];
	if (!id || pins.some((p) => p.id === id)) return [...pins];
	const kept = pins.length >= PIN_LIMIT
		? pins.slice(pins.length - PIN_LIMIT + 1)
		: pins;
	return [...kept, { id, lng, lat }];
}

/**
 * Take one pin off the map.
 *
 * @param pins The pins on the map.
 * @param id Which to remove.
 * @returns A new list without it, or an unchanged copy when no pin has
 *     that id.
 */
export function removePin(pins: readonly Pin[], id: string): Pin[] {
	return pins.filter((p) => p.id !== id);
}

/**
 * What to call a pin on screen.
 *
 * Numbered by position rather than by id, so the labels read 1, 2, 3
 * down the map however many have been added and removed. The id is for
 * the code and the number is for the reader.
 *
 * @param index The pin's place in the list, from zero.
 * @returns The label.
 */
export function pinLabel(index: number): string {
	return String(Math.max(0, Math.trunc(index)) + 1);
}

/** Counts up so two pins made in the same millisecond still differ. */
let counter = 0;

/**
 * A fresh pin id.
 *
 * Not the array index: a pin removed from the middle would renumber
 * every pin after it, and a click would then remove the wrong one.
 *
 * @returns An id no previous call has returned.
 */
export function nextPinId(): string {
	counter += 1;
	return `pin-${Date.now().toString(36)}-${counter}`;
}
