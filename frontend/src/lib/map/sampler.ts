/**
 * Reading pixels back out of the overlay frame that is on screen.
 *
 * The frame is drawn to an offscreen canvas once, when it swaps, and sampled
 * one pixel at a time on pointer move. Drawing per move would redo a 700 by
 * 700 blit for every mouse event; sampling a single pixel from a canvas
 * already drawn costs nothing measurable.
 *
 * The geometry is in `./readout` and the color lookup in `./ramps/invert`.
 * This holds only the part that cannot be pure, which is the canvas.
 *
 * Every overlay URL this samples is same-origin through the backend proxy,
 * and `preloadImage` sets `crossOrigin` before `src`, so the canvas is never
 * tainted. A tainted canvas throws on read, and `at` reports that as no
 * reading rather than letting it escape into a pointer handler.
 */

/** One pixel, as the canvas stores it. */
export type Rgba = [number, number, number, number];

/** What a sampler needs from a drawable frame. */
export interface Drawable {
	naturalWidth: number;
	naturalHeight: number;
}

/** The 2D canvas operations a sampler uses, so a test can stand in for one. */
export interface SamplerCanvas {
	width: number;
	height: number;
	getContext(id: '2d'): CanvasRenderingContext2D | null;
}

/**
 * Holds the current frame's pixels for the cursor readout.
 *
 * One canvas for the life of the map, resized when a frame of a different
 * size arrives, because allocating a canvas per frame churns memory during
 * playback.
 */
export class FrameSampler {
	private canvas: SamplerCanvas | null = null;
	private ctx: CanvasRenderingContext2D | null = null;
	private width = 0;
	private height = 0;
	private readonly make: () => SamplerCanvas;

	/**
	 * @param make Builds a canvas. Defaults to a detached document canvas,
	 *     and is injectable so a runner can supply one.
	 */
	constructor(make?: () => SamplerCanvas) {
		this.make = make ?? ((): SamplerCanvas => document.createElement('canvas'));
	}

	/** The current frame's size in pixels, or null when nothing is held. */
	get size(): { width: number; height: number } | null {
		return this.width > 0 && this.height > 0
			? { width: this.width, height: this.height }
			: null;
	}

	/**
	 * The frame itself, for a caller that has to draw it rather than
	 * read one pixel of it.
	 *
	 * The elevated sheet uploads this as a GL texture. Taking it from
	 * here rather than fetching the image again means the sheet draws
	 * exactly the frame the readout is reporting, so a value and the
	 * pixel it came from can never be one step apart.
	 *
	 * @returns The canvas holding the frame, or null when none is held.
	 */
	get frame(): TexImageSource | null {
		if (!this.size || !this.canvas) return null;
		// Narrowed rather than cast, because a test runner supplies a
		// stub canvas that holds pixels and is not a DOM object, and GL
		// cannot upload one of those. The map always has a real canvas
		// here, so this returns null only under a runner, where nothing
		// is drawing anyway.
		const held: unknown = this.canvas;
		if (typeof HTMLCanvasElement !== 'undefined'
			&& held instanceof HTMLCanvasElement) return held;
		if (typeof OffscreenCanvas !== 'undefined'
			&& held instanceof OffscreenCanvas) return held;
		return null;
	}

	/**
	 * Take a frame, replacing whatever was held.
	 *
	 * @param img The decoded frame.
	 * @returns True when the frame was drawn and can be sampled.
	 */
	draw(img: (Drawable & CanvasImageSource) | null): boolean {
		if (!img || img.naturalWidth <= 0 || img.naturalHeight <= 0) {
			this.clear();
			return false;
		}
		try {
			if (!this.canvas) {
				this.canvas = this.make();
				this.ctx = this.canvas.getContext('2d');
			}
			if (!this.ctx) {
				this.clear();
				return false;
			}
			if (this.canvas.width !== img.naturalWidth
				|| this.canvas.height !== img.naturalHeight) {
				this.canvas.width = img.naturalWidth;
				this.canvas.height = img.naturalHeight;
			}
			this.ctx.clearRect(0, 0, img.naturalWidth, img.naturalHeight);
			this.ctx.drawImage(img, 0, 0);
			this.width = img.naturalWidth;
			this.height = img.naturalHeight;
			return true;
		} catch {
			// A frame that cannot be drawn leaves no stale pixels behind. A
			// readout off the previous frame would be a number from a
			// different minute.
			this.clear();
			return false;
		}
	}

	/** Forget the current frame, so nothing reads back until one is drawn. */
	clear(): void {
		this.width = 0;
		this.height = 0;
	}

	/**
	 * The pixel at one position, or null when there is nothing to read.
	 *
	 * @param x Column, from the left.
	 * @param y Row, from the top.
	 * @returns The channels, or null when no frame is held, the position is
	 *     outside it, or the canvas refuses the read.
	 */
	at(x: number, y: number): Rgba | null {
		if (!this.ctx || this.width === 0 || this.height === 0) return null;
		if (!Number.isInteger(x) || !Number.isInteger(y)) return null;
		if (x < 0 || y < 0 || x >= this.width || y >= this.height) return null;
		try {
			const d = this.ctx.getImageData(x, y, 1, 1).data;
			return [d[0], d[1], d[2], d[3]];
		} catch {
			return null;
		}
	}
}
