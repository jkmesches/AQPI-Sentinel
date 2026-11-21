/**
 * Hillshade relief, and the DEM it shares with the 3D mesh.
 *
 * One source feeds two consumers. The hillshade layer draws relief in two
 * dimensions, and `setTerrain` drapes the whole scene over the same tiles.
 * MapLibre refuses to drop a source either still holds, so the teardown
 * here checks both rather than removing it outright.
 *
 * The 3D mesh is not a layer and has no entry in the style's layer list, so
 * it stays on the component beside the camera pitch it moves with.
 */
import type { LayerCtx, LayerModule, MapLike, Theme } from './registry';

/** The part of a MapLibre map the DEM needs beyond the common surface. */
export interface TerrainMapLike extends MapLike {
	getTerrain?: () => unknown;
}

export const TERRAIN_SOURCE = 'terrain-dem';
export const HILLSHADE_LAYER = 'hillshade-layer';

/** Terrarium-encoded DEM tiles, served by AWS from Mapzen's open data. */
export const DEM_ATTRIBUTION = 'Terrain: AWS / Mapzen Open Data';

/** Free public tiles, no API key. */
export const DEM_TILE_URL =
	'https://s3.amazonaws.com/elevation-tiles-prod/v2/terrarium/{z}/{x}/{y}.png';

/**
 * Below the radar range rings.
 *
 * Relief is background, and hillshade over the rings would wash out the one
 * thing on the map that says how far a radar can see.
 */
export const HILLSHADE_BELOW = 'range-fill';

const SHADOW = { light: '#3a3a3a', dark: '#000000' };
const HIGHLIGHT = { light: '#ffffff', dark: '#3a3a3a' };
const ACCENT = { light: '#202020', dark: '#0a0a0a' };

const pick = (table: Record<Theme, string>, t: Theme) => table[t];

/**
 * Add the DEM source when nothing has already.
 *
 * Exported because the 3D mesh needs it too, and it is called before
 * `setTerrain` on a map that may have no hillshade layer.
 *
 * @param map The map.
 * @param tileUrl The DEM tile template. Defaults to the public AWS tiles.
 */
export function ensureTerrainSource(map: MapLike, tileUrl: string = DEM_TILE_URL): void {
	if (map.getSource(TERRAIN_SOURCE)) return;
	map.addSource(TERRAIN_SOURCE, {
		type: 'raster-dem',
		tiles: [tileUrl],
		encoding: 'terrarium',
		tileSize: 256,
		maxzoom: 15,
		attribution: DEM_ATTRIBUTION
	});
}

/**
 * Drop the DEM when neither the hillshade layer nor the 3D mesh holds it.
 *
 * Removing a source that something still references throws, and both
 * references are invisible from the source itself.
 *
 * @param map The map.
 */
export function removeTerrainSourceIfUnused(map: TerrainMapLike): void {
	if (map.getLayer(HILLSHADE_LAYER)) return;
	if (map.getTerrain?.()) return;
	if (map.getSource(TERRAIN_SOURCE)) map.removeSource(TERRAIN_SOURCE);
}

/**
 * The hillshade overlay.
 *
 * @param tileUrl The DEM tile template. Defaults to the public AWS tiles.
 * @returns A module the registry can apply.
 */
export function terrainLayer(tileUrl: string = DEM_TILE_URL): LayerModule {
	return {
		id: 'terrain',
		layers: [HILLSHADE_LAYER],
		sources: [TERRAIN_SOURCE],
		teardown(map: MapLike): void {
			removeTerrainSourceIfUnused(map as TerrainMapLike);
		},
		build(ctx: LayerCtx): Promise<void> {
			ensureTerrainSource(ctx.map, tileUrl);
			ctx.map.addLayer(
				{
					id: HILLSHADE_LAYER,
					type: 'hillshade',
					source: TERRAIN_SOURCE,
					paint: {
						'hillshade-exaggeration': 0.55,
						'hillshade-shadow-color': pick(SHADOW, ctx.theme),
						'hillshade-highlight-color': pick(HIGHLIGHT, ctx.theme),
						'hillshade-accent-color': pick(ACCENT, ctx.theme)
					}
				},
				ctx.map.getLayer(HILLSHADE_BELOW) ? HILLSHADE_BELOW : undefined
			);
			return Promise.resolve();
		}
	};
}
