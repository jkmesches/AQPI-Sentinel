/**
 * One status per radar, collapsed from every stage that observes one.
 *
 * A radar has up to three independent readings — `L2` (what radarca declares),
 * `LB2` (raw volumes landing on disk), `LB3` (whether it is still feeding the
 * composite) — and which of them EXIST is a property of the profile, not of
 * the radar. XQPI registers no `L2` at all: `layer2_radar` is listed in
 * `config.RADARCA_ONLY_MODULES` because nothing serves FLOW over HTTP. So a
 * reducer that reads `L2` alone sees an empty stage there and learns nothing.
 *
 * That was a live bug rather than a hypothetical. The desktop map read `L2`
 * alone and defaulted an unmatched radar to `pass`, so FLOW rendered green on
 * XQPI whatever the radar was doing, and would have stayed green through an
 * outage of any length. The mobile map had already grown the correct reducer
 * inline and the desktop copy was never updated — two copies of one rule, one
 * of them fixed. This module is that rule, lifted so there is one copy and a
 * test can reach it.
 *
 * The default is the substance of the fix, not a detail of it: a radar nothing
 * reported is `skip`, never `pass`. `worstOf([])` already answers this way for
 * rows, and markers need the same answer, because a green pin drawn on no
 * evidence is the one failure mode monitoring must not have — a stale fail
 * makes an operator look at the radar, a false pass makes them look away from
 * it.
 */
import { isFleetCheck } from './format';
import { worstOf } from './grouping';

/**
 * The stages carrying a per-radar reading, in causal order: arrival (`LB2`)
 * precedes composite participation (`LB3`), and `L2` is the display tier's own
 * opinion of the same radar.
 *
 * A list rather than a prefix test, so a new radar-observing stage has to be
 * added here deliberately instead of being picked up by its spelling. The
 * order does not change the result — `worstOf` is order-independent — but it
 * is the order the pipeline runs in.
 */
export const RADAR_STATUS_STAGES = ['L2', 'LB2', 'LB3'] as const;

/**
 * What a radar gets when no stage reported it.
 *
 * Exported so a caller states this default by name rather than inlining a
 * literal, which is how the desktop map came to inline `pass`.
 */
export const NO_READING = 'skip';

/** The shape this reducer needs from a rollup row (`StatusRow` satisfies it). */
export interface RadarReading {
	check_id: string;
	target: string;
	status: string;
}

/**
 * Worst reading per radar target, across `RADAR_STATUS_STAGES`.
 *
 * Two kinds of row share those stages without describing a single radar, and
 * both are dropped:
 *
 *   - the fleet correlation verdicts (`isFleetCheck`), which describe the
 *     fleet as a whole and would otherwise render as a phantom radar;
 *   - `layer3.backend.drops`, which is the DROPS producer rather than a radar.
 *
 * Takes the `stages` map rather than a single stage's rows so the caller cannot
 * pass one stage and get a confident-looking answer about a radar that stage
 * never saw.
 */
export function radarStatusByTarget(
	stages: Record<string, RadarReading[]> | null | undefined
): Record<string, string> {
	const out: Record<string, string> = {};
	const st = stages ?? {};
	for (const stage of RADAR_STATUS_STAGES) {
		for (const r of st[stage] ?? []) {
			if (isFleetCheck(r.check_id)) continue;
			if (r.check_id === 'layer3.backend.drops') continue;
			out[r.target] = worstOf([out[r.target], r.status].filter(Boolean) as string[]);
		}
	}
	return out;
}

/**
 * One radar's status out of `radarStatusByTarget`, defaulting to `NO_READING`.
 *
 * The accessor exists so the default lives in one place. Callers must not
 * inline `?? 'pass'`: an unmonitored radar is unknown, not healthy, and the
 * map has no way to tell the difference once it has been painted green.
 */
export function radarStatusOf(
	byTarget: Record<string, string>,
	radarId: string
): string {
	return byTarget[radarId] ?? NO_READING;
}
