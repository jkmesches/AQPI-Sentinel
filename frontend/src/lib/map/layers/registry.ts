/**
 * One shape for every map overlay, and the loop that applies them.
 *
 * MapView built each overlay inline inside one `refreshGeography`, and the
 * shared function is what made them interfere. Its watershed branch returns
 * early when the theme swaps mid-fetch, and the return leaves the whole
 * function, so the reservoirs that follow are never rebuilt. A module per
 * overlay ends that: a layer that bails or throws takes only itself down.
 *
 * The modules talk to `MapLike` rather than to `maplibregl.Map`, which a
 * real map satisfies structurally. A runner can then drive them against a
 * fake and assert what was added, in what order, and with which `beforeId`,
 * none of which a screenshot or the structural capture can see from outside.
 */
import type { AddLayerObject, SourceSpecification } from 'maplibre-gl';

/** Which basemap is showing. Overlay paint is keyed to it. */
export type Theme = 'light' | 'dark';

/** The part of a MapLibre map an overlay module uses. */
export interface MapLike {
	getLayer(id: string): unknown;
	removeLayer(id: string): void;
	addLayer(layer: AddLayerObject, beforeId?: string): void;
	getSource(id: string): unknown;
	removeSource(id: string): void;
	addSource(id: string, source: SourceSpecification): void;
}

/** What one overlay needs to build itself. */
export interface LayerCtx {
	map: MapLike;
	theme: Theme;
	/** Whether this overlay should be on screen after this pass. */
	enabled: boolean;
	/**
	 * Where one of this module's layers belongs in the paint order.
	 *
	 * Supplied rather than imported, so a caller can place a module's layers
	 * somewhere else without the module knowing. See `layers/stack.ts`.
	 */
	before: (layerId: string) => string | undefined;
	/**
	 * Whether this pass is still the current one.
	 *
	 * A theme swap or a teardown while a fetch is in flight makes it false,
	 * and the module stops rather than adding layers to a style that has
	 * been replaced underneath it.
	 */
	alive: () => boolean;
}

/**
 * Paint already loaded, waiting to be put on the map.
 *
 * The frame barrier exists because applying each overlay as its own image
 * decodes tears the frame: the composite steps forward while the radars are
 * still on the previous one. Every participant loads first and returns one
 * of these, and the caller fires them together.
 */
export type FrameApply = () => void;

/** What a participant needs to prepare one frame. */
export interface FrameCtx {
	map: MapLike;
	/** Still the current frame. False once the scrubber has moved again. */
	alive: () => boolean;
}

/** One map overlay. */
export interface LayerModule {
	/** Stable name, used in errors and in the registry. */
	id: string;
	/**
	 * Layer ids this module adds, topmost first.
	 *
	 * Removed in this order before every rebuild, because MapLibre refuses
	 * to drop a source while a layer still references it.
	 */
	layers: readonly string[];
	/** Source ids this module adds. */
	sources: readonly string[];
	/** Add the layers. Called only when `ctx.enabled`. */
	build(ctx: LayerCtx): Promise<void>;
	/**
	 * Teardown beyond removing `layers` and `sources`.
	 *
	 * Supplied by a module whose source outlives its layer. Terrain is the
	 * one: the DEM feeds both the hillshade layer and the 3D mesh, and
	 * dropping it while the mesh holds it would throw.
	 */
	teardown?(map: MapLike): void;
	/**
	 * Load this overlay's paint for the current frame.
	 *
	 * An array because one module can own several overlays: the radar module
	 * returns one apply per active radar. A module with nothing to swap
	 * returns an empty array, and so does one whose load failed.
	 */
	prepare?(ctx: FrameCtx): Promise<FrameApply[]>;
}

/** What one frame's preparation produced. */
export interface FrameResult {
	/** Every apply, in module order. Empty when the deadline won. */
	applies: FrameApply[];
	/** Modules that threw, by id. */
	errors: Record<string, string>;
	/** Whether the deadline fired before every participant was ready. */
	late: boolean;
}

/**
 * Load every participant's frame, then hand back the applies as one batch.
 *
 * Races a deadline, because one slow radar must not hold the whole frame.
 * Past it the caller keeps the frame it has rather than tearing in a
 * partial one, and the next scrub tries again.
 *
 * A participant that throws is recorded and the rest still paint. The three
 * hardcoded participants this replaces each caught to null or to an empty
 * array, which made a failed load and an idle layer indistinguishable.
 *
 * @param modules The overlays to ask. Those with no `prepare` are skipped.
 * @param ctx What they need, including whether the frame is still current.
 * @param deadlineMs How long to wait before giving up on the batch.
 * @returns The applies to fire, with whatever failed.
 */
export async function prepareFrame(
	modules: readonly LayerModule[],
	ctx: FrameCtx,
	deadlineMs: number
): Promise<FrameResult> {
	const participants = modules.filter((m) => m.prepare);
	const errors: Record<string, string> = {};
	const work = Promise.all(participants.map(async (mod) => {
		try {
			return await mod.prepare!(ctx);
		} catch (e) {
			errors[mod.id] = e instanceof Error ? e.message : String(e);
			return [];
		}
	}));
	const late = new Promise<null>((resolve) => {
		setTimeout(() => resolve(null), deadlineMs);
	});
	const settled = await Promise.race([work, late]);
	if (settled === null) return { applies: [], errors, late: true };
	return { applies: settled.flat(), errors, late: false };
}

/** What happened to one module in one pass. */
export interface LayerOutcome {
	id: string;
	/** The error, when the module threw. Absent on success. */
	error?: string;
	/** Whether the module built, as opposed to only tearing down. */
	built: boolean;
}

/**
 * Remove a module's layers, then whatever holds its sources.
 *
 * @param map The map.
 * @param mod The module to clear.
 */
export function clearLayer(map: MapLike, mod: LayerModule): void {
	for (const id of mod.layers) {
		if (map.getLayer(id)) map.removeLayer(id);
	}
	if (mod.teardown) {
		mod.teardown(map);
		return;
	}
	for (const id of mod.sources) {
		if (map.getSource(id)) map.removeSource(id);
	}
}

/**
 * Clear one overlay and rebuild it when it is on.
 *
 * Always clears first, including when the overlay stays on, because the
 * paint of every one of these is keyed to the theme and a swap has to
 * replace it rather than leave the old colors.
 *
 * @param mod The overlay.
 * @param ctx What it needs, including whether it should be on.
 * @returns What happened, with any error as a string rather than a throw.
 */
export async function applyLayer(
	mod: LayerModule,
	ctx: LayerCtx
): Promise<LayerOutcome> {
	try {
		clearLayer(ctx.map, mod);
		if (!ctx.enabled || !ctx.alive()) return { id: mod.id, built: false };
		await mod.build(ctx);
		return { id: mod.id, built: true };
	} catch (e) {
		return {
			id: mod.id,
			built: false,
			error: e instanceof Error ? e.message : String(e)
		};
	}
}

/**
 * Apply every overlay, each independent of the others.
 *
 * Concurrent, because each one's slow part is its own fetch and they share
 * nothing. The paint order does not depend on which finishes first, since
 * each layer is placed by `ctx.before` rather than by arrival.
 *
 * A module that throws is reported and the rest still run. The old shared
 * function could not do that, and one layer's mid-fetch bail silently cost
 * the next one its rebuild.
 *
 * @param modules The overlays to apply.
 * @param ctxFor What each one needs, by module id.
 * @returns One outcome per module, in the order given.
 */
export async function applyLayers(
	modules: readonly LayerModule[],
	ctxFor: (mod: LayerModule) => LayerCtx
): Promise<LayerOutcome[]> {
	return Promise.all(modules.map((mod) => applyLayer(mod, ctxFor(mod))));
}
