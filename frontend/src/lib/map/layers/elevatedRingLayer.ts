/**
 * The GL side of the elevated range rings.
 *
 * Split from the geometry in `elevatedRings` so the arithmetic can be
 * tested in node. This half needs a browser and a GL context.
 *
 * One layer draws every ring, unlike the sheets, which get one layer
 * each because each carries its own texture. A ring carries a color and
 * nothing else, so the six of them are six strips in one buffer and one
 * draw call per ring against one program.
 *
 * The dashes are cut in the fragment shader from the arc length each
 * vertex carries. MapLibre's own `line-dasharray` works on a screen
 * length and has no equivalent here, and cutting them from ground
 * meters is steadier anyway: the dash pattern stays fixed to the ring
 * rather than crawling along it as the camera zooms.
 */
import type { CustomLayerInterface, Map as MlMap } from 'maplibre-gl';
// The default import, as in MapView.svelte. Vite's dev server loads
// maplibre-gl as CommonJS during SSR and refuses a named export from it.
import maplibregl from 'maplibre-gl';
import { ringFan, ringStrip, type Ring } from './elevatedRings';

/** One ring to draw, and how. */
export interface RingSpec {
	/** Where it is and how high, as the geometry module takes it. */
	ring: Ring;
	/** Its line color, as three 0-to-1 channels. */
	color: [number, number, number];
	/** 0 to 1, for the line. */
	opacity: number;
	/** 0 to 1, for the faint disc inside it. */
	fillOpacity: number;
	/** How wide the line draws, in device pixels. */
	widthPx: number;
}

/**
 * The dash period and duty, in ground meters.
 *
 * 4 km on and 4 km off puts about 16 dashes around a 40 km ring, which
 * matches what the draped [2, 2] pattern showed at the zoom this
 * console opens on.
 */
const DASH_PERIOD_M = 8000;
const DASH_DUTY = 0.5;

const VERTEX_SRC = `
	uniform mat4 u_matrix;
	attribute vec3 a_pos;
	attribute float a_arc;
	varying float v_arc;
	void main() {
		v_arc = a_arc;
		gl_Position = u_matrix * vec4(a_pos, 1.0);
	}
`;

const FRAGMENT_SRC = `
	precision mediump float;
	uniform vec3 u_color;
	uniform float u_opacity;
	uniform float u_period;
	uniform float u_duty;
	varying float v_arc;
	void main() {
		// u_period of 0 draws solid, which is what the fill uses.
		if (u_period > 0.0 && fract(v_arc / u_period) > u_duty) discard;
		gl_FragColor = vec4(u_color * u_opacity, u_opacity);
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
		throw new Error(`elevated ring shader did not compile: ${log}`);
	}
	return shader;
}

/**
 * Ground and altitude into the mercator space the matrix expects.
 *
 * @param lng Longitude in degrees.
 * @param lat Latitude in degrees.
 * @param altitudeM Height above sea level, in meters.
 * @returns The mercator triple.
 */
function toMercator(
	lng: number, lat: number, altitudeM: number
): [number, number, number] {
	const m = maplibregl.MercatorCoordinate.fromLngLat({ lng, lat }, altitudeM);
	return [m.x, m.y, m.z];
}

/** Every range ring, drawn at the height of the imagery it bounds. */
export class ElevatedRings implements CustomLayerInterface {
	readonly type = 'custom' as const;
	readonly renderingMode = '3d' as const;
	readonly id: string;

	private specs: RingSpec[] = [];
	/** The nominal sheet altitude, for a ring with no tilt engaged. */
	private sheetM = 0;
	private map: MlMap | null = null;
	private program: WebGLProgram | null = null;
	private buffer: WebGLBuffer | null = null;
	private aPos = -1;
	private aArc = -1;
	private uMatrix: WebGLUniformLocation | null = null;
	private uColor: WebGLUniformLocation | null = null;
	private uOpacity: WebGLUniformLocation | null = null;
	private uPeriod: WebGLUniformLocation | null = null;
	private uDuty: WebGLUniformLocation | null = null;

	/**
	 * @param id The layer id, unique in the style.
	 */
	constructor(id: string) {
		this.id = id;
	}

	/**
	 * Replace what this layer draws.
	 *
	 * @param specs The rings.
	 * @param sheetM The nominal sheet altitude, in meters.
	 */
	setRings(specs: RingSpec[], sheetM: number): void {
		this.specs = specs;
		this.sheetM = sheetM;
		this.map?.triggerRepaint();
	}

	/**
	 * Build the program and the buffer.
	 *
	 * @param map The map this was added to.
	 * @param gl Its context.
	 */
	onAdd(map: MlMap, gl: WebGLRenderingContext): void {
		this.map = map;
		const program = gl.createProgram();
		if (!program) throw new Error('could not create a GL program');
		gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERTEX_SRC));
		gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SRC));
		gl.linkProgram(program);
		if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
			const log = gl.getProgramInfoLog(program) ?? 'no log';
			throw new Error(`elevated ring program did not link: ${log}`);
		}
		this.program = program;
		this.aPos = gl.getAttribLocation(program, 'a_pos');
		this.aArc = gl.getAttribLocation(program, 'a_arc');
		this.uMatrix = gl.getUniformLocation(program, 'u_matrix');
		this.uColor = gl.getUniformLocation(program, 'u_color');
		this.uOpacity = gl.getUniformLocation(program, 'u_opacity');
		this.uPeriod = gl.getUniformLocation(program, 'u_period');
		this.uDuty = gl.getUniformLocation(program, 'u_duty');
		this.buffer = gl.createBuffer();
	}

	/**
	 * Release the program and the buffer.
	 *
	 * @param _map The map this was removed from.
	 * @param gl Its context.
	 */
	onRemove(_map: MlMap, gl: WebGLRenderingContext): void {
		if (this.program) gl.deleteProgram(this.program);
		if (this.buffer) gl.deleteBuffer(this.buffer);
		this.program = null;
		this.buffer = null;
		this.map = null;
	}

	/**
	 * Draw every ring at its own height.
	 *
	 * @param gl The context.
	 * @param args What MapLibre hands a custom layer, carrying the
	 *     matrix under one of two names depending on its version.
	 */
	render(gl: WebGLRenderingContext, args: unknown): void {
		if (!this.program || !this.buffer || this.specs.length === 0) return;
		const matrix = matrixOf(args);
		if (!matrix) return;
		gl.useProgram(this.program);
		gl.uniformMatrix4fv(this.uMatrix, false, matrix);
		gl.enable(gl.BLEND);
		gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
		// No depth writes, for the same reason the sheets make none: a
		// ring is an annotation over the scene rather than part of it,
		// and writing depth would have it hide the imagery behind it.
		gl.depthMask(false);
		gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
		for (const spec of this.specs) {
			this.drawFill(gl, spec);
			this.drawLine(gl, spec);
		}
		gl.depthMask(true);
	}

	/**
	 * The faint disc inside one ring.
	 *
	 * @param gl The context.
	 * @param spec The ring.
	 */
	private drawFill(gl: WebGLRenderingContext, spec: RingSpec): void {
		if (spec.fillOpacity <= 0) return;
		const fan = ringFan(spec.ring, this.sheetM, toMercator);
		gl.bufferData(gl.ARRAY_BUFFER, fan, gl.DYNAMIC_DRAW);
		gl.enableVertexAttribArray(this.aPos);
		gl.vertexAttribPointer(this.aPos, 3, gl.FLOAT, false, 12, 0);
		if (this.aArc >= 0) gl.disableVertexAttribArray(this.aArc);
		gl.uniform3fv(this.uColor, spec.color);
		gl.uniform1f(this.uOpacity, spec.fillOpacity);
		// Solid: a dashed fill would be a stipple nobody asked for.
		gl.uniform1f(this.uPeriod, 0);
		gl.uniform1f(this.uDuty, 1);
		gl.drawArrays(gl.TRIANGLE_FAN, 0, fan.length / 3);
	}

	/**
	 * The dashed ring itself.
	 *
	 * @param gl The context.
	 * @param spec The ring.
	 */
	private drawLine(gl: WebGLRenderingContext, spec: RingSpec): void {
		const strip = ringStrip(spec.ring, this.sheetM, toMercator);
		gl.bufferData(gl.ARRAY_BUFFER, strip, gl.DYNAMIC_DRAW);
		gl.enableVertexAttribArray(this.aPos);
		gl.vertexAttribPointer(this.aPos, 3, gl.FLOAT, false, 16, 0);
		if (this.aArc >= 0) {
			gl.enableVertexAttribArray(this.aArc);
			gl.vertexAttribPointer(this.aArc, 1, gl.FLOAT, false, 16, 12);
		}
		gl.uniform3fv(this.uColor, spec.color);
		gl.uniform1f(this.uOpacity, spec.opacity);
		gl.uniform1f(this.uPeriod, DASH_PERIOD_M);
		gl.uniform1f(this.uDuty, DASH_DUTY);
		// lineWidth above 1 is ignored by most drivers and is not worth
		// building a quad strip for a reference mark; the active ring
		// reads as brighter rather than thicker.
		gl.lineWidth(spec.widthPx);
		gl.drawArrays(gl.LINE_STRIP, 0, strip.length / 4);
	}
}

/**
 * The view matrix, under whichever name this MapLibre hands over.
 *
 * @param args The render argument.
 * @returns The matrix, or null when it is not there.
 */
function matrixOf(args: unknown): Float32Array | number[] | null {
	if (typeof args !== 'object' || args === null) return null;
	const a = args as {
		defaultProjectionData?: { mainMatrix?: Float32Array | number[] };
		projectionMatrix?: Float32Array | number[];
	};
	return a.defaultProjectionData?.mainMatrix ?? a.projectionMatrix ?? null;
}
