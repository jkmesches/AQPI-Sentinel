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
import flowReflectivity from './data/flow-reflectivity.json';
import flowVelocity from './data/flow-velocity.json';
import flowZdr from './data/flow-differential-reflectivity.json';
import flowPhidp from './data/flow-differential-phase.json';
import aqpiXReflectivity from './data/aqpi-xband-reflectivity.json';
import aqpiXVelocity from './data/aqpi-xband-velocity.json';
import aqpiXZdr from './data/aqpi-xband-differential-reflectivity.json';
import aqpiXPhidp from './data/aqpi-xband-differential-phase.json';

/** Every checked-in ramp, by the name coverage.json refers to it by. */
export const RAMPS: Readonly<Record<string, Ramp>> = {
	'flow-reflectivity': flowReflectivity as Ramp,
	'flow-velocity': flowVelocity as Ramp,
	'flow-differential-reflectivity': flowZdr as Ramp,
	'flow-differential-phase': flowPhidp as Ramp,
	'aqpi-xband-reflectivity': aqpiXReflectivity as Ramp,
	'aqpi-xband-velocity': aqpiXVelocity as Ramp,
	'aqpi-xband-differential-reflectivity': aqpiXZdr as Ramp,
	'aqpi-xband-differential-phase': aqpiXPhidp as Ramp
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
