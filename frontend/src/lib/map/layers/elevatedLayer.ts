/**
 * The GL side of the elevated imagery sheet.
 *
 * Split from the geometry in `elevatedRaster` so the arithmetic can be
 * tested in node. This half needs a browser and a GL context, and does
 * as little thinking as possible: it uploads a texture, builds the
 * quad from the geometry module, and draws.
 *
 * MapLibre renders its own raster layers to a texture and drapes that
 * onto the terrain mesh. A custom layer in 3d rendering mode is drawn
 * in the main pass with the depth buffer instead, so a quad at
 * altitude stays at altitude and is occluded by terrain in front of it
 * rather than wrapped around it.
 */
import type { CustomLayerInterface, Map as MlMap } from 'maplibre-gl';
// The default import, as in MapView.svelte. Vite's dev server loads
// maplibre-gl as CommonJS during SSR and refuses a named export from it.
import maplibregl from 'maplibre-gl';
import {
	beamVertices, isDrawable, sheetVertices, type Beam, type Corners
} from './elevatedRaster';

/** What the sheet needs to draw itself. */
export interface SheetSpec {
	/** The layer id, unique in the style. */
	id: string;
	/** The overlay's four corners, as its image source takes them. */
	corners: Corners;
	/**
	 * What to draw.
	 *
	 * A canvas when the console already holds the decoded frame, which
	 * is the composite and whichever radar the readout is sampling. The
	 * sheet then draws exactly the frame the readout reports, and no
	 * second fetch happens. A url for the rest.
	 */
	image: TexImageSource | string;
	/**
	 * Changes whenever `image` holds something new.
	 *
	 * A canvas is reused in place, so its identity says nothing about
	 * whether the pixels moved. The console knows when a frame swapped
	 * and says so here.
	 */
	version: string;
	/** 0 to 1. */
	opacity: number;
	/** How high the sheet sits, in meters. */
	altitudeM: number;
	/**
	 * The radar whose beam this surface follows, for a single tilt.
	 *
	 * Present for a tilt image, which sweeps a cone whose height grows
	 * with range, so the surface curves instead of lying flat. Absent
	 * for a composite, which mixes tilts and ranges and has no beam of
	 * its own to follow; that one gets the nominal sheet.
	 */
	beam?: Beam | null;
}

const VERTEX_SRC = `
	uniform mat4 u_matrix;
	attribute vec3 a_pos;
	attribute vec2 a_uv;
	varying vec2 v_uv;
	void main() {
		v_uv = a_uv;
		gl_Position = u_matrix * vec4(a_pos, 1.0);
	}
`;

const FRAGMENT_SRC = `
	precision mediump float;
	uniform sampler2D u_image;
	uniform float u_opacity;
	varying vec2 v_uv;
	void main() {
		vec4 c = texture2D(u_image, v_uv);
		// The overlay PNGs carry their own alpha and arrive
		// unpremultiplied, so the color is multiplied here rather than
		// trusting the upload path to have done it. Without this the
		// transparent margin of a radar scan draws as a black square.
		gl_FragColor = vec4(c.rgb * c.a, c.a) * u_opacity;
	}
`;

/**
 * Compile one shader, or say which one failed and why.
 *
 * @param gl The context.
 * @param type gl.VERTEX_SHADER or gl.FRAGMENT_SHADER.
 * @param src The source.
 * @returns The compiled shader.
 * @raises Error If it does not compile.
 */
function compile(
	gl: WebGLRenderingContext, type: number, src: string
): WebGLShader {
	const shader = gl.createShader(type);
	if (!shader) throw new Error('could not create a shader');
	gl.shaderSource(shader, src);
	gl.compileShader(shader);
	if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
		const log = gl.getShaderInfoLog(shader) ?? 'no log';
		gl.deleteShader(shader);
		throw new Error(`elevated sheet shader did not compile: ${log}`);
	}
	return shader;
}

/**
 * An image overlay drawn as a flat sheet at altitude.
 *
 * One instance per overlay. The spec is replaced rather than mutated,
 * so a frame swap is one assignment and the next render picks it up.
 */
export class ElevatedSheet implements CustomLayerInterface {
	readonly type = 'custom' as const;
	readonly renderingMode = '3d' as const;
	readonly id: string;

	private spec: SheetSpec;
	private map: MlMap | null = null;
	private program: WebGLProgram | null = null;
	private buffer: WebGLBuffer | null = null;
	private texture: WebGLTexture | null = null;
	private aPos = -1;
	private aUv = -1;
	private uMatrix: WebGLUniformLocation | null = null;
	private uImage: WebGLUniformLocation | null = null;
	private uOpacity: WebGLUniformLocation | null = null;
	/** Which version the texture holds, so a repaint does not reload it. */
	private loaded: string | null = null;
	/** Set while an image is in flight, so one load runs at a time. */
	private loading = false;

	constructor(spec: SheetSpec) {
		this.id = spec.id;
		this.spec = spec;
	}

	/**
	 * Point the sheet at a new frame, altitude or opacity.
	 *
	 * @param spec What to draw now.
	 */
	update(spec: SheetSpec): void {
		const was = this.spec.version;
		this.spec = spec;
		if (spec.version !== was) this.loaded = null;
		this.map?.triggerRepaint();
	}

	/** What the sheet is drawing, for a caller deciding whether to update. */
	current(): SheetSpec {
		return this.spec;
	}

	/**
	 * Build the program and buffers.
	 *
	 * @param map The map this was added to.
	 * @param gl Its context.
	 */
	onAdd(map: MlMap, gl: WebGLRenderingContext): void {
		this.map = map;
		const program = gl.createProgram();
		if (!program) throw new Error('could not create a GL program');
		const vs = compile(gl, gl.VERTEX_SHADER, VERTEX_SRC);
		const fs = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SRC);
		gl.attachShader(program, vs);
		gl.attachShader(program, fs);
		gl.linkProgram(program);
		if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
			const log = gl.getProgramInfoLog(program) ?? 'no log';
			throw new Error(`elevated sheet program did not link: ${log}`);
		}
		gl.deleteShader(vs);
		gl.deleteShader(fs);
		this.program = program;
		this.aPos = gl.getAttribLocation(program, 'a_pos');
		this.aUv = gl.getAttribLocation(program, 'a_uv');
		this.uMatrix = gl.getUniformLocation(program, 'u_matrix');
		this.uImage = gl.getUniformLocation(program, 'u_image');
		this.uOpacity = gl.getUniformLocation(program, 'u_opacity');
		this.buffer = gl.createBuffer();
		this.texture = gl.createTexture();
	}

	/**
	 * Give back everything this took.
	 *
	 * @param _map The map, unused.
	 * @param gl Its context.
	 */
	onRemove(_map: MlMap, gl: WebGLRenderingContext): void {
		if (this.program) gl.deleteProgram(this.program);
		if (this.buffer) gl.deleteBuffer(this.buffer);
		if (this.texture) gl.deleteTexture(this.texture);
		this.program = null;
		this.buffer = null;
		this.texture = null;
		this.map = null;
	}

	/**
	 * Fetch the current frame into the texture, once per url.
	 *
	 * @param gl The context.
	 */
	private upload(gl: WebGLRenderingContext, src: TexImageSource): void {
		if (!this.texture) return;
		gl.bindTexture(gl.TEXTURE_2D, this.texture);
		// Clamped because the sheet is one quad and a wrapped edge
		// would repeat the far side of the scan across the margin.
		gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
		gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
		gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
		gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
		gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA,
			gl.UNSIGNED_BYTE, src);
		this.loaded = this.spec.version;
	}

	/**
	 * Put the current frame in the texture, once per version.
	 *
	 * @param gl The context.
	 */
	private ensureTexture(gl: WebGLRenderingContext): void {
		const { image, version } = this.spec;
		if (this.loaded === version || this.loading || !image) return;
		if (typeof image !== 'string') {
			this.upload(gl, image);
			return;
		}
		this.loading = true;
		const img = new Image();
		img.crossOrigin = 'anonymous';
		const want = version;
		img.onload = () => {
			this.loading = false;
			// The frame may have moved on while this was in flight, and
			// uploading it then would put an old frame on screen under
			// a new frame's number.
			if (this.spec.version !== want) return;
			this.upload(gl, img);
			this.map?.triggerRepaint();
		};
		img.onerror = () => {
			this.loading = false;
			// Left unloaded on purpose. The sheet then draws nothing,
			// which is the same as the draped layer failing to fetch,
			// and the console's own frame error path already reports a
			// frame that will not load.
		};
		img.src = image;
	}

	/**
	 * Draw the sheet.
	 *
	 * @param gl The context.
	 * @param args The render arguments, carrying the projection matrix.
	 */
	render(gl: WebGLRenderingContext, args: { defaultProjectionData: {
		mainMatrix: number[]; projectionMatrix?: number[]; } } | unknown): void {
		this.ensureTexture(gl);
		if (!this.program || !this.buffer || this.loaded === null) return;
		if (!isDrawable(this.spec.corners)) return;
		const matrix = matrixOf(args);
		if (!matrix) return;

		const project = (lng: number, lat: number, alt: number):
			[number, number, number] => {
			const m = maplibregl.MercatorCoordinate.fromLngLat({ lng, lat }, alt);
			return [m.x, m.y, m.z];
		};
		const verts = this.spec.beam
			? beamVertices(this.spec.corners, this.spec.beam, project)
			: sheetVertices(this.spec.corners, this.spec.altitudeM, project);
		const count = verts.length / 5;

		gl.useProgram(this.program);
		gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
		gl.bufferData(gl.ARRAY_BUFFER, verts, gl.DYNAMIC_DRAW);
		const stride = 5 * 4;
		gl.enableVertexAttribArray(this.aPos);
		gl.vertexAttribPointer(this.aPos, 3, gl.FLOAT, false, stride, 0);
		gl.enableVertexAttribArray(this.aUv);
		gl.vertexAttribPointer(this.aUv, 2, gl.FLOAT, false, stride, 3 * 4);
		gl.uniformMatrix4fv(this.uMatrix, false, matrix);
		gl.activeTexture(gl.TEXTURE0);
		gl.bindTexture(gl.TEXTURE_2D, this.texture);
		gl.uniform1i(this.uImage, 0);
		gl.uniform1f(this.uOpacity, Math.min(1, Math.max(0, this.spec.opacity)));
		gl.enable(gl.BLEND);
		// Premultiplied, to match what the fragment shader writes.
		gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
		// Tested against the depth buffer so terrain in front of the
		// sheet hides it, and not written to, so two sheets at
		// different altitudes both draw and blend.
		gl.enable(gl.DEPTH_TEST);
		gl.depthMask(false);
		gl.drawArrays(gl.TRIANGLES, 0, count);
		gl.depthMask(true);
	}
}

/**
 * The projection matrix out of whichever shape MapLibre handed over.
 *
 * MapLibre has passed the matrix directly and, in later versions, in a
 * projection-data object. Reading both keeps this working across an
 * upgrade instead of drawing the sheet at the origin.
 *
 * @param args Whatever render was called with.
 * @returns The 4x4 matrix, or null when there is none.
 */
function matrixOf(args: unknown): number[] | Float32Array | null {
	if (Array.isArray(args) || args instanceof Float32Array) return args;
	if (!args || typeof args !== 'object') return null;
	const data = (args as { defaultProjectionData?: {
		mainMatrix?: number[]; projectionMatrix?: number[];
	} }).defaultProjectionData;
	return data?.mainMatrix ?? data?.projectionMatrix ?? null;
}
