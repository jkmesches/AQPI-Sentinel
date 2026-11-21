/**
 * Where everything goes in an exported image, and what it says.
 *
 * A screenshot of this console pasted into a thread loses the two things
 * that make it readable: which minute it is and what the colors mean. The
 * reader downstream has no address bar and no legend panel, so a frame of
 * reflectivity becomes a picture of green blobs over the Bay, and a frame
 * from three hours ago is indistinguishable from the current one. Both of
 * those have been the subject of real confusion on this project, so the
 * export burns them into the pixels.
 *
 * Geometry and words only. The drawing is the component's, because a
 * canvas context cannot be exercised in the node runner, and splitting it
 * this way puts every number that can be wrong on the testable side.
 */

import type { Ramp } from './ramps/invert';

/** Space around the map inside the exported image, in pixels. */
export const PADDING = 16;

/** Height of the band under the map that carries the caption. */
export const FOOTER_HEIGHT = 64;

/** Height of the color bar drawn in the footer. */
export const LEGEND_HEIGHT = 10;

/** Width of the color bar, as a fraction of the image. */
export const LEGEND_WIDTH_FRACTION = 0.38;

/** The widest the color bar may get, in pixels. */
export const LEGEND_MAX_WIDTH = 460;

/** A rectangle in the exported image. */
export interface Box {
	x: number;
	y: number;
	width: number;
	height: number;
}

/** Where each part of the exported image sits. */
export interface ExportLayout {
	width: number;
	height: number;
	map: Box;
	footer: Box;
	/** The color bar, or null when there is no measured ramp to draw. */
	legend: Box | null;
}

/** What the caption says, line by line. */
export interface ExportCaption {
	/** The product and the moment the picture shows. */
	title: string;
	/** The frame's time, which a screenshot otherwise loses. */
	when: string;
	/** Where it came from, so the image can be traced back. */
	credit: string;
}

/** What the console was showing when the export was taken. */
export interface ExportView {
	site: string;
	product: string;
	moment: string | null;
	unit: string | null;
	/** The frame's label, as the time strip shows it. */
	stepLabel: string;
	radars: readonly string[];
	source: string;
}

/**
 * Where the map, the caption and the color bar go.
 *
 * The map keeps its own pixels at their own size. Scaling it here would
 * resample imagery that is already one pixel per rendered pixel, and a
 * resampled radar frame reads as a blurrier product rather than as a
 * smaller one.
 *
 * @param mapWidth The map canvas's width in pixels.
 * @param mapHeight Its height.
 * @param hasLegend Whether a measured ramp is available to draw.
 * @returns The geometry of the whole image.
 */
export function exportLayout(
	mapWidth: number,
	mapHeight: number,
	hasLegend: boolean
): ExportLayout {
	// Math.max(1, NaN) is NaN, so the finite check comes first. A canvas
	// reports NaN while it is being resized and after a lost WebGL
	// context, and a NaN width makes a canvas the browser refuses to
	// create, which fails as a silent no-op rather than an error.
	const side = (v: number) => (Number.isFinite(v) ? Math.max(1, Math.round(v)) : 1);
	const w = side(mapWidth);
	const h = side(mapHeight);
	const width = w + PADDING * 2;
	const height = h + PADDING * 2 + FOOTER_HEIGHT;
	const footer: Box = {
		x: PADDING,
		y: PADDING + h,
		width: w,
		height: FOOTER_HEIGHT
	};
	const barWidth = Math.min(LEGEND_MAX_WIDTH,
		Math.round(w * LEGEND_WIDTH_FRACTION));
	return {
		width,
		height,
		map: { x: PADDING, y: PADDING, width: w, height: h },
		footer,
		legend: hasLegend
			? {
				x: footer.x + footer.width - barWidth,
				y: footer.y + 14,
				width: barWidth,
				height: LEGEND_HEIGHT
			}
			: null
	};
}

/**
 * What the caption says.
 *
 * The time is never omitted, even when the console has no frame label,
 * because a picture with no time is the failure this exists to prevent and
 * "time unknown" is a usable answer where a blank is not.
 *
 * @param view What the console was showing.
 * @returns The three lines.
 */
export function exportCaption(view: ExportView): ExportCaption {
	const parts = [view.product];
	if (view.moment) parts.push(view.moment);
	if (view.unit) parts.push(view.unit);
	const radars = view.radars.length > 0
		? view.radars.join(', ')
		: 'no radar overlays';
	return {
		title: parts.join(' · '),
		when: view.stepLabel.trim() || 'frame time unknown',
		credit: `${view.site} · ${view.source} · ${radars}`
	};
}

/**
 * A file name that says what the image is.
 *
 * Only characters that survive every file system this lands on, since the
 * images go into chat threads, tickets and email. A product id with a
 * slash in it would otherwise make a path.
 *
 * @param product The product id.
 * @param stepLabel The frame label, which may be empty.
 * @param now The clock, used when there is no frame label.
 * @returns The file name, with its extension.
 */
export function exportFileName(
	product: string,
	stepLabel: string,
	now: Date = new Date()
): string {
	const safe = (s: string) => s
		.trim()
		.replace(/[^A-Za-z0-9]+/g, '-')
		.replace(/^-+|-+$/g, '')
		.toLowerCase();
	const when = safe(stepLabel) || safe(now.toISOString().slice(0, 16));
	const what = safe(product) || 'map';
	return `sentinel-${what}-${when}.png`;
}

/**
 * The value a legend tick sits at, as a fraction of the bar.
 *
 * The exported bar is drawn from the same ramp the on-screen legend uses,
 * so the two cannot disagree about what a color means.
 *
 * @param ramp The ramp being drawn.
 * @returns The lowest and highest values on the bar.
 */
export function legendRange(ramp: Ramp): { lo: number; hi: number } {
	const stops = ramp.stops;
	if (stops.length === 0) return { lo: 0, hi: 0 };
	return { lo: stops[0].lo, hi: stops[stops.length - 1].hi };
}
