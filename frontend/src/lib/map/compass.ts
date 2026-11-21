/**
 * Which way the map is facing, for the compass that says so.
 *
 * The console opens at a bearing of 22 degrees, chosen to fit the Bay's
 * diagonal into a landscape pane. That is a good default and a bad
 * surprise: every distance and direction a reader takes off the screen is
 * rotated, and nothing on the map said so until this control.
 *
 * Pure, so the needle's geometry and the label are tested without a map.
 */

import { compassPoint } from './measure';

/**
 * How far from north the map may face and still read as north up.
 *
 * Half a degree, which is under the rounding of the degree the label
 * shows. A map a degree off north is not north up, and saying it is
 * would make the control agree with itself.
 */
export const NORTH_TOLERANCE_DEG = 0.5;

/**
 * Fold any heading into 0 up to 360.
 *
 * @param deg A heading in degrees, of any sign or size.
 * @returns The same heading in 0 to 360, or 0 when it is not finite.
 */
export function normalizeBearing(deg: number): number {
	if (!Number.isFinite(deg)) return 0;
	return ((deg % 360) + 360) % 360;
}

/**
 * Whether the map is facing north closely enough to say so.
 *
 * @param deg The map's bearing.
 * @returns True when the needle is within the tolerance of north.
 */
export function isNorthUp(deg: number): boolean {
	const at = normalizeBearing(deg);
	return at <= NORTH_TOLERANCE_DEG || at >= 360 - NORTH_TOLERANCE_DEG;
}

/**
 * How far to rotate the needle so it keeps pointing at true north.
 *
 * The map rotates clockwise by its bearing, so the needle rotates the
 * other way by the same amount.
 *
 * @param deg The map's bearing.
 * @returns Degrees to rotate the needle, in 0 to 360.
 */
export function needleRotation(deg: number): number {
	return normalizeBearing(-normalizeBearing(deg));
}

/**
 * The heading the map is facing, in words and degrees.
 *
 * @param deg The map's bearing.
 * @returns "north up" when it is, otherwise a reading like "022° NNE".
 */
export function bearingLabel(deg: number): string {
	const at = normalizeBearing(deg);
	if (isNorthUp(at)) return 'north up';
	const rounded = Math.round(at) % 360;
	const padded = String(rounded).padStart(3, '0');
	return `${padded}° ${compassPoint(at)}`;
}

/**
 * What clicking the compass should do.
 *
 * Returning the camera rather than applying it, so the caller owns the
 * map and this stays testable. The pitch comes back with the bearing: a
 * pitched view is as much a departure from the plan view a reader
 * measures on, and leaving it tilted after asking for north gives a
 * compass that says north on a map still drawn in perspective.
 *
 * @param bearing The map's bearing.
 * @param pitch The map's pitch.
 * @returns The camera to ease to, or null when it is already there.
 */
export function resetCamera(
	bearing: number,
	pitch: number
): { bearing: number; pitch: number } | null {
	const level = Number.isFinite(pitch) ? pitch : 0;
	if (isNorthUp(bearing) && Math.abs(level) <= NORTH_TOLERANCE_DEG) {
		return null;
	}
	return { bearing: 0, pitch: 0 };
}
