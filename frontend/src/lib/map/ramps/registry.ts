/**
 * Which ramp reads a given radar's given moment, when one has been measured.
 *
 * The lookup answers from `data/coverage.json`, which records what
 * `ramp_coverage.py` found when it sampled real frames. A pair that was never
 * measured, or that was measured and failed, has no ramp here and the legend
 * and readout stay off for it. Tying the two together in data rather than in
 * a convention is deliberate: the plan's rule is that a ramp is measured
 * before it ships, and a hand-maintained map would let a ramp reach a readout
 * without that evidence.
 *
 * Two of the measurement's results shape this file. AQPI's X-band
 * reflectivity bar carries the same colors as FLOW's over a different range,
 * -20 to 80 dBZ against -30 to 80, so #988f66 is 4.86 dBZ on one and -2.40 on
 * the other. And the C-band, which publishes no colorbar, shares the
 * X-band reflectivity and velocity ramps while painting ZDR and PhiDP from
 * some other map entirely, with none of its 253 colors matching across a
 * million opaque pixels.
 */

import type { Ramp } from './invert';
import coverage from './data/coverage.json';
import productCoverage from './data/product-coverage.json';
import productReflectivity from './data/aqpi-product-reflectivity.json';
import productTempF from './data/aqpi-product-temp-f.json';
import productFcstAccumIn from './data/aqpi-product-forecast-accum-in.json';
import productFcstAccumMm from './data/aqpi-product-forecast-accum-mm.json';
import productWaterDepth from './data/aqpi-product-water-depth.json';
import productWaterLevel from './data/aqpi-product-water-level.json';
import productQpeIn from './data/aqpi-product-qpe-in.json';
import productQpeRateIn from './data/aqpi-product-qpe-rate-in.json';
import productForecastRateIn from './data/aqpi-product-forecast-rate-in.json';
import flowReflectivity from './data/flow-reflectivity.json';
import flowVelocity from './data/flow-velocity.json';
import flowZdr from './data/flow-differential-reflectivity.json';
import flowPhidp from './data/flow-differential-phase.json';
import aqpiXReflectivity from './data/aqpi-xband-reflectivity.json';
import aqpiXVelocity from './data/aqpi-xband-velocity.json';
import aqpiXZdr from './data/aqpi-xband-differential-reflectivity.json';
import aqpiXPhidp from './data/aqpi-xband-differential-phase.json';
// The C-band's own three. It shares the X-band's reflectivity and
// velocity tables, which its renderer defines identically, and paints
// the other three from custom ramps of its own.
import aqpiCZdr from './data/aqpi-cband-differential-reflectivity.json';
import aqpiCPhidp from './data/aqpi-cband-differential-phase.json';
import aqpiCRhohv from './data/aqpi-cband-rhohv.json';
// radar-display's tilt frames, which paint other fields from other tables
// than Chivo does, and are recorded in their own coverage file.
import tiltCoverage from './data/tilt-coverage.json';
import tiltReflectivity from './data/tilt-reflectivity.json';
import tiltVelocity from './data/tilt-velocity.json';
import tiltCopolar from './data/tilt-copolarcorrelation.json';

/** Every checked-in ramp, by the name coverage.json refers to it by. */
export const RAMPS: Readonly<Record<string, Ramp>> = {
	'flow-reflectivity': flowReflectivity as Ramp,
	'flow-velocity': flowVelocity as Ramp,
	'flow-differential-reflectivity': flowZdr as Ramp,
	'flow-differential-phase': flowPhidp as Ramp,
	'aqpi-xband-reflectivity': aqpiXReflectivity as Ramp,
	'aqpi-xband-velocity': aqpiXVelocity as Ramp,
	'aqpi-xband-differential-reflectivity': aqpiXZdr as Ramp,
	'aqpi-xband-differential-phase': aqpiXPhidp as Ramp,
	'aqpi-cband-differential-reflectivity': aqpiCZdr as Ramp,
	'aqpi-cband-differential-phase': aqpiCPhidp as Ramp,
	'aqpi-cband-rhohv': aqpiCRhohv as Ramp,
	// The composite products. These come from radarca's own colormap
	// images rather than from the mount, since the publisher writes a
	// colorbar per radar moment and nothing per product.
	'aqpi-product-reflectivity': productReflectivity as Ramp,
	'aqpi-product-temp-f': productTempF as Ramp,
	'aqpi-product-forecast-accum-in': productFcstAccumIn as Ramp,
	'aqpi-product-forecast-accum-mm': productFcstAccumMm as Ramp,
	'aqpi-product-water-depth': productWaterDepth as Ramp,
	'aqpi-product-water-level': productWaterLevel as Ramp,
	'aqpi-product-qpe-in': productQpeIn as Ramp,
	'aqpi-product-qpe-rate-in': productQpeRateIn as Ramp,
	'aqpi-product-forecast-rate-in': productForecastRateIn as Ramp,
	'tilt-reflectivity': tiltReflectivity as Ramp,
	'tilt-velocity': tiltVelocity as Ramp,
	'tilt-copolarcorrelation': tiltCopolar as Ramp
};

/** One row of the coverage record. */
export interface CoveragePair {
	radar: string;
	moment: string;
	ramp: string | null;
	frames: number;
	pixels: number;
	/** Matched over distinct, as the tool printed it. */
	colors: string;
	/**
	 * What the coverage rests on.
	 *
	 * "frames" is the default and the stronger evidence: every distinct
	 * color in this radar's own frames read back through the table.
	 * "colorbar" means the table was extracted from this radar's own
	 * published colorbar and the frame check has not run, which is the
	 * case for a moment whose frames are empty. A radar whose filtered
	 * PhiDP is masked to nothing on a quiet day still publishes the bar
	 * that says what its colors mean.
	 */
	basis?: 'frames' | 'colorbar';
	/** The colorbar's md5 prefix, where the basis is that bar. */
	colorbar_md5?: string;
	covered: boolean;
	note?: string;
}

interface CoverageFile {
	measured_on: string;
	pairs: CoveragePair[];
	shared_colorbars: Record<string, { md5: string; radars: string[] }>;
}

const FILE = coverage as unknown as CoverageFile;

/** The date the coverage record was produced, ISO 8601. */
export const MEASURED_ON: string = FILE.measured_on;

/**
 * Build the lookup, refusing a record that points at a ramp that is not here.
 *
 * Runs at import time so a renamed or deleted ramp file fails the build
 * rather than silently dropping a legend.
 *
 * @returns The covered pairs, keyed by radar and moment.
 * @raises Error If a covered pair names a ramp that is not checked in.
 */
function buildIndex(): Map<string, Ramp> {
	const index = new Map<string, Ramp>();
	for (const p of FILE.pairs) {
		if (!p.covered || p.ramp === null) continue;
		const ramp = RAMPS[p.ramp];
		if (!ramp) {
			throw new Error(
				`coverage.json covers ${p.radar}/${p.moment} with ramp ` +
				`"${p.ramp}", which is not in RAMPS`);
		}
		index.set(keyOf(p.radar, p.moment), ramp);
	}
	return index;
}

/**
 * The lookup key for a radar and moment.
 *
 * Case-folded, because the console spells a moment as its label while the
 * published tree spells it as a directory or a filename part.
 *
 * @param radar The radar's short name.
 * @param moment The moment, as the published tree names it.
 * @returns The key.
 */
export function keyOf(radar: string, moment: string): string {
	return `${radar.toLowerCase()}\u0000${moment.toLowerCase()}`;
}

const INDEX = buildIndex();

/** One row of the product coverage record. */
export interface ProductCoverage {
	product: string;
	unit: string | null;
	ramp: string | null;
	frames: number;
	/** Share of painted area that read back, as a percentage. */
	pixels: number;
	/** Whether the frames' commonest colors sat on stops exactly. */
	exact_common: boolean;
	covered: boolean;
	note?: string;
}

interface ProductFile {
	measured_on: string;
	products: ProductCoverage[];
}

const PRODUCT_FILE = productCoverage as unknown as ProductFile;

/**
 * Build the product lookup, refusing a row that names an absent ramp.
 *
 * @returns Product and unit to ramp.
 * @raises Error If a covered row points at a ramp that is not in RAMPS.
 */
function buildProductIndex(): Map<string, Ramp> {
	const index = new Map<string, Ramp>();
	for (const p of PRODUCT_FILE.products) {
		if (!p.covered || p.ramp === null) continue;
		const ramp = RAMPS[p.ramp];
		if (!ramp) {
			throw new Error(
				`product-coverage.json covers ${p.product} with ramp ` +
				`"${p.ramp}", which is not in RAMPS`);
		}
		index.set(productKey(p.product, p.unit), ramp);
	}
	return index;
}

/**
 * The lookup key for a product and unit.
 *
 * A product published in two units has a bar for each, and the two carry
 * different numbers against the same colors, so the unit is part of the
 * key rather than a detail of the ramp it finds.
 *
 * @param product The product id.
 * @param unit The unit, or null for a product published in one unit.
 * @returns The key.
 */
function productKey(product: string, unit: string | null): string {
	return `${product.toLowerCase()}\u0000${(unit ?? '').toLowerCase()}`;
}

const PRODUCT_INDEX = buildProductIndex();

/**
 * The ramp that reads one composite product, or null when none was measured.
 *
 * Null for a product whose colorbar was never measured against its frames
 * and for one the measurement rejected, and the caller then draws no
 * legend and reports no value. radarca publishes a bar for every product,
 * and for several of them the bar and the frames turn out to come from
 * different colormaps, so having a bar is not evidence that it reads the
 * pixels.
 *
 * @param product The product id, as the console holds it.
 * @param unit The unit in view, or null for a single-unit product.
 * @returns The ramp, or null.
 */
export function rampForProduct(product: string,
	unit: string | null = null): Ramp | null {
	return PRODUCT_INDEX.get(productKey(product, unit))
		?? PRODUCT_INDEX.get(productKey(product, null))
		?? null;
}

/**
 * What the measurement found for one product, whether or not it passed.
 *
 * Lets the console say why a product reports nothing instead of leaving
 * the reader to wonder whether the feature is broken.
 *
 * @param product The product id.
 * @param unit The unit in view, or null.
 * @returns The record, or null when the product was never measured.
 */
export function productCoverageFor(product: string,
	unit: string | null = null): ProductCoverage | null {
	const want = productKey(product, unit);
	const bare = productKey(product, null);
	return PRODUCT_FILE.products.find(
		(p) => productKey(p.product, p.unit) === want) ?? PRODUCT_FILE.products.find(
		(p) => productKey(p.product, p.unit) === bare) ?? null;
}

/**
 * The ramp that reads one radar's one moment, or null when none was measured.
 *
 * Null is the answer for an unmeasured pair and for a pair the measurement
 * rejected, and the caller draws no legend and reports no value. A guess here
 * would put a confident number under a cursor.
 *
 * @param radar The radar's short name.
 * @param moment The moment, as the published tree names it.
 * @returns The ramp, or null.
 */
export function rampFor(radar: string, moment: string): Ramp | null {
	return INDEX.get(keyOf(radar, moment)) ?? null;
}

interface TiltCoverageFile {
	measured_on: string;
	pairs: CoveragePair[];
}

const TILT_FILE = tiltCoverage as unknown as TiltCoverageFile;

/**
 * Build the tilt lookup, refusing a row that names an absent ramp.
 *
 * Its own index rather than rows in coverage.json, because the case-folded
 * key would make a tilt "velocity" and a Chivo "Velocity" the same pair.
 *
 * @returns Radar and tilt moment to ramp.
 * @raises Error If a covered row points at a ramp that is not in RAMPS.
 */
function buildTiltIndex(): Map<string, Ramp> {
	const index = new Map<string, Ramp>();
	for (const p of TILT_FILE.pairs) {
		if (!p.covered || p.ramp === null) continue;
		const ramp = RAMPS[p.ramp];
		if (!ramp) {
			throw new Error(
				`tilt-coverage.json covers ${p.radar}/${p.moment} with ramp ` +
				`"${p.ramp}", which is not in RAMPS`);
		}
		index.set(keyOf(p.radar, p.moment), ramp);
	}
	return index;
}

const TILT_INDEX = buildTiltIndex();

/** The covered tilt pairs, for a test that every ramp has evidence. */
export const TILT_PAIRS: readonly CoveragePair[] = TILT_FILE.pairs;

/**
 * The ramp that reads one radar's radar-display tilt, or null when unmeasured.
 *
 * @param radar The radar's short name.
 * @param moment The tilt moment as radar-display names its files, e.g.
 *     "copolarcorrelation".
 * @returns The ramp, or null.
 */
export function tiltRampFor(radar: string, moment: string): Ramp | null {
	return TILT_INDEX.get(keyOf(radar, moment)) ?? null;
}

/**
 * What the measurement found for one pair, whether or not it passed.
 *
 * Lets a panel say why a legend is missing instead of showing nothing.
 *
 * @param radar The radar's short name.
 * @param moment The moment, as the published tree names it.
 * @returns The record, or null when the pair was never measured.
 */
export function coverageFor(radar: string, moment: string): CoveragePair | null {
	const want = keyOf(radar, moment);
	return FILE.pairs.find((p) => keyOf(p.radar, p.moment) === want) ?? null;
}

/**
 * Radars known to paint from the same colorbar as a measured one.
 *
 * Their colorbar PNGs are byte-identical to the measured radar's, which is
 * the evidence that they share its table. Separate from `rampFor`, so a
 * caller can tell a measured pair from an inferred one.
 *
 * @param ramp The ramp's name.
 * @returns The radar names, empty when nothing is recorded.
 */
export function radarsSharing(ramp: string): readonly string[] {
	return FILE.shared_colorbars[ramp]?.radars ?? [];
}
