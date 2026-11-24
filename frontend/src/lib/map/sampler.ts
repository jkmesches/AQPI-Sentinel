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
 *
 * A 2D canvas stores color premultiplied by alpha, so a partly transparent
 * pixel comes back rounded: by up to 29, 75 and 41 summed channel units at
 * the faintest edges of one comp_ref frame in Chromium, Firefox and WebKit.
 * A fully opaque pixel came back unchanged in all three. So a partly
 * transparent sample reads the file's own bytes instead, through WebGL
 * with premultiplication off, which returned every byte of that frame
 * unchanged in all three engines. The canvas answers when that read
 * fails, and `exact` says whether the last sample was the file's own.
 */

/** One pixel, as the canvas stores it. */
export type Rgba = [number, number, number, number];

/** Reads a decoded frame's own RGBA bytes, or null where it cannot. */
export type ExactReader = (img: Drawable & CanvasImageSource) => Uint8Array | null;

/**
 * The WebGL context exact reads go through, made on first use and shared.
 *
 * Module state, because a page gets few WebGL contexts and Chromium drops
 * the oldest past 16. The map holds one of its own, and a context per held
 * frame would put it at risk.
 */
let readContext: WebGLRenderingContext | null | undefined;

/**
 * The shared context, or null where WebGL is unavailable.
 *
 * A lost context is dropped and a new one made on a fresh canvas, since a
 * lost one answers every call with nothing until the browser restores it,
 * which it does only for a page that asked.
 *
 * @returns The context.
 */
function readGl(): WebGLRenderingContext | null {
	if (readContext?.isContextLost()) readContext = undefined;
	if (readContext === undefined) {
		try {
			const canvas = typeof document !== 'undefined'
				? document.createElement('canvas')
				: typeof OffscreenCanvas !== 'undefined' ? new OffscreenCanvas(1, 1) : null;
			const gl = canvas?.getContext('webgl', {
				premultipliedAlpha: false, antialias: false, depth: false, stencil: false
			});
			readContext = typeof WebGLRenderingContext !== 'undefined'
				&& gl instanceof WebGLRenderingContext ? gl : null;
		} catch {
			readContext = null;
		}
	}
	return readContext;
}

/**
 * A frame's own bytes, read through WebGL with premultiplication off.
 *
 * @param img The decoded frame.
 * @returns Its RGBA bytes, top row first, or null when WebGL is missing or
 *     lost, the frame is larger than a texture may be, or any step fails.
 */
export function readExactBytes(img: Drawable & CanvasImageSource): Uint8Array | null {
	const gl = readGl();
	if (!gl) return null;
	const w = img.naturalWidth;
	const h = img.naturalHeight;
	const max = gl.getParameter(gl.MAX_TEXTURE_SIZE) as number;
	if (w > max || h > max) return null;
	const texture = gl.createTexture();
	const framebuffer = gl.createFramebuffer();
	try {
		gl.bindTexture(gl.TEXTURE_2D, texture);
		gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
		gl.pixelStorei(gl.UNPACK_COLORSPACE_CONVERSION_WEBGL, gl.NONE);
		gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
		gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE,
			img as TexImageSource);
		gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
		gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0,
			gl.TEXTURE_2D, texture, 0);
		if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) {
			return null;
		}
		const out = new Uint8Array(w * h * 4);
		gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, out);
		return gl.getError() === gl.NO_ERROR ? out : null;
	} catch {
		// A tainted or undecodable frame throws on upload. The canvas
		// path answers instead.
		return null;
	} finally {
		gl.bindFramebuffer(gl.FRAMEBUFFER, null);
		gl.deleteFramebuffer(framebuffer);
		gl.deleteTexture(texture);
	}
}

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
 *
 * The exact bytes are read the first time a partly transparent pixel of a
 * frame is sampled. The read took 6 to 11 ms on a
 * 843 by 1108 composite frame in a headless loop and up to 103 ms on a first
 * hover, and holds the frame's bytes, 21 MB for a 1801 by 2872 water frame.
 * Playback swaps frames whether or not a cursor is over the map, and most
 * of a frame is opaque. Each frame is its own decoded image, never reused
 * for another URL, so holding it until then is safe.
 */
export class FrameSampler {
	private canvas: SamplerCanvas | null = null;
	private ctx: CanvasRenderingContext2D | null = null;
	private width = 0;
	private height = 0;
	private readonly make: () => SamplerCanvas;
	private readonly readExact: ExactReader;
	/** The frame drawn last, until its bytes have been read. */
	private pending: (Drawable & CanvasImageSource) | null = null;
	/** Its own bytes, or null where they could not be read. */
	private bytes: Uint8Array | null = null;
	/** Whether the pixel `at` returned last was the file's own. */
	private lastExact = false;

	/**
	 * @param make Builds a canvas. Defaults to a detached document canvas,
	 *     and is injectable so a runner can supply one.
	 * @param readExact Reads a frame's own bytes. Defaults to WebGL, and is
	 *     injectable for the same reason.
	 */
	constructor(make?: () => SamplerCanvas, readExact: ExactReader = readExactBytes) {
		this.make = make ?? ((): SamplerCanvas => document.createElement('canvas'));
		this.readExact = readExact;
	}

	/**
	 * Whether the pixel `at` returned last was the file's own.
	 *
	 * False before any sample, and for a partly transparent pixel when the
	 * exact read failed and the canvas, which rounds it, answered instead.
	 */
	get exact(): boolean {
		return this.lastExact;
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
			this.pending = img;
			this.bytes = null;
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
		this.pending = null;
		this.bytes = null;
		this.lastExact = false;
	}

	/** Read the held frame's own bytes, once, if that has not been tried. */
	private settle(): void {
		if (!this.pending) return;
		const img = this.pending;
		this.pending = null;
		const got = this.readExact(img);
		this.bytes = got !== null && got.length === this.width * this.height * 4
			? got
			: null;
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
		this.lastExact = false;
		if (!this.ctx || this.width === 0 || this.height === 0) return null;
		if (!Number.isInteger(x) || !Number.isInteger(y)) return null;
		if (x < 0 || y < 0 || x >= this.width || y >= this.height) return null;
		if (!this.bytes) {
			let pixel: Rgba;
			try {
				const d = this.ctx.getImageData(x, y, 1, 1).data;
				pixel = [d[0], d[1], d[2], d[3]];
			} catch {
				return null;
			}
			// The canvas rounds only a partly transparent pixel.
			if (pixel[3] === 255 || pixel[3] === 0) {
				this.lastExact = true;
				return pixel;
			}
			this.settle();
			if (!this.bytes) return pixel;
		}
		const i = (y * this.width + x) * 4;
		this.lastExact = true;
		return [this.bytes[i], this.bytes[i + 1], this.bytes[i + 2], this.bytes[i + 3]];
	}
}
