/**
 * Frame URLs, and the corners an image overlay is placed on.
 *
 * Pure, so a runner can reach it. A wrong cache rule draws a map that looks
 * correct, and assertions are the only way to see one.
 *
 * A frame URL is keyed on the frame's own content, never on `Date.now()`. A
 * per-render cache-buster made every render a unique URL, so nothing was
 * ever reused. Measured on the composite, revisiting five already-seen
 * steps cost five more server hits and dropped the scrub to about 17 fps
 * with 1.2 s main-thread blocks.
 *
 * Keying on the step's timestamp is also safer than keying on its index. The
 * window rolls, so a given index maps to a new timestamp as frames age out,
 * and a URL built from the index alone would serve a stale frame under a
 * shifted index.
 */

/** A geographic box, west/east/south/north in degrees. */
export interface Extent {
	west: number;
	east: number;
	south: number;
	north: number;
}

/**
 * MapLibre's ImageSource corners, clockwise from the top left.
 *
 * A plain `[number, number][]` loses the arity and will not typecheck
 * against `addSource`.
 */
export type ImageCorners = [
	[number, number], [number, number], [number, number], [number, number]
];

/**
 * The four corners of an extent, in the order an image source wants.
 *
 * @param e The box.
 * @returns Corners clockwise from the north-west.
 */
export function extentCorners(e: Extent): ImageCorners {
	return [
		[e.west, e.north],
		[e.east, e.north],
		[e.east, e.south],
		[e.west, e.south]
	];
}

/** One composite frame. */
export interface CompositeFrame {
	/** The product id, as the deployment publishes it. */
	product: string;
	/** Index into the step list. */
	step: number;
	/**
	 * The step's own timestamp, which identifies the frame.
	 *
	 * Null where a step list carries none, which drops the parameter rather
	 * than sending the string "null".
	 */
	ts?: string | null;
	/**
	 * A 30-second-quantized value that forces a genuine refetch.
	 *
	 * Quantized rather than `Date.now()`, so several force-refreshes inside
	 * one window share a cache hit. Zero or absent leaves it off the URL.
	 */
	buster?: number;
}

/**
 * The path and query for one composite frame.
 *
 * @param f Which frame.
 * @returns A path relative to the API origin.
 */
export function compositeImagePath(f: CompositeFrame): string {
	const q = new URLSearchParams({ product_id: f.product, step: String(f.step) });
	if (f.ts) q.set('ts', f.ts);
	if (f.buster) q.set('_t', String(f.buster));
	return `/api/upstream/product_image.png?${q.toString()}`;
}

/** One X-band scan frame. */
export interface ScanFrame {
	radar: string;
	moment: string;
	/** The frame's time, as the display tier indexes them. */
	time?: string | null;
	/** See `CompositeFrame.buster`. */
	buster?: number;
}

/**
 * The path and query for one radar scan.
 *
 * No cache-buster is added by default, because the radar, the moment and
 * the time are already the dedup key. Every paint tick and every toggle used
 * to trigger a fresh proxy fetch, because the URL kept changing.
 *
 * @param f Which scan.
 * @returns A path relative to the API origin.
 */
export function scanImagePath(f: ScanFrame): string {
	const q = new URLSearchParams({ radar: f.radar, moment: f.moment });
	if (f.time) q.set('time', f.time);
	if (f.buster) q.set('_t', String(f.buster));
	return `/api/upstream/xband_scan.png?${q.toString()}`;
}

/** One per-elevation tilt frame. */
export interface TiltFrame {
	radar: string;
	/** Elevation angle in degrees. */
	elevation: number;
	/** The moment as the display tier names it, which differs from ours. */
	moment: string;
	/** The display tier's frame index, where 0 is newest. */
	frame: number;
}

/**
 * The path and query for one tilt frame.
 *
 * @param f Which frame.
 * @returns A path relative to the API origin.
 */
export function tiltImagePath(f: TiltFrame): string {
	const q = new URLSearchParams({
		radar: f.radar,
		el: String(f.elevation),
		moment: f.moment,
		frame: String(f.frame)
	});
	return `/api/upstream/tilt_image.png?${q.toString()}`;
}
