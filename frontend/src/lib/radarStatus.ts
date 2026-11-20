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
 * added here deliberately instead of being picked up by its spelling.
 */
export const RADAR_STATUS_STAGES = ['L2', 'LB2', 'LB3'] as const;

/** Data landing on disk. The question a pin answers when it can. */
export const ARRIVAL_STAGE = 'LB2';

/** Still feeding the composite. A radar can arrive perfectly and fail this. */
export const PROCESSING_STAGE = 'LB3';

/** What the display tier declares about the same radar. */
export const DISPLAY_STAGE = 'L2';

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

/** A radar's collapsed verdict, and which stage decided it. */
export interface RadarVerdict {
	status: string;
	/** `LB2`, `LB3`, `L2`, or `none` when nothing reported the radar. */
	from: string;
}

/**
 * Rows of one stage that actually describe a single radar.
 *
 * Two kinds of row share these stages without being about one radar:
 * the fleet correlation verdicts (`isFleetCheck`), which describe the fleet
 * and would render as a phantom radar, and `layer3.backend.drops`, which is
 * the DROPS producer.
 */
function radarRows(stages: Record<string, RadarReading[]>, stage: string): RadarReading[] {
	return (stages[stage] ?? []).filter(
		(r) => !isFleetCheck(r.check_id) && r.check_id !== 'layer3.backend.drops'
	);
}

/** The worst status among rows for one target, or undefined if there are none. */
function worstFor(rows: RadarReading[], target: string): string | undefined {
	const mine = rows.filter((r) => r.target === target).map((r) => r.status);
	return mine.length ? worstOf(mine) : undefined;
}

/**
 * One verdict per radar, preferring what the filesystem says.
 *
 * This took the worst of every stage, which let radarca paint a pin red while
 * the volumes were landing on disk normally. radarca is slow and wrong often
 * enough that its verdict is the one to drop, measured at a p90 of 8-13 s on
 * its own status endpoint with episodes reaching 42 s. On a deployment that
 * reads the filesystem, radarca therefore never paints a pin.
 *
 * The order is arrival, then processing, then nothing:
 *
 *   - `LB2` decides wherever it has a row, because "is this radar delivering"
 *     is the question a pin should answer.
 *   - `LB3` decides where `LB2` has no row. The three NEXRADs are why. They
 *     reach the composite by a path with no arrival check, so composite input
 *     is the only thing Sentinel observes about them, and it stands as their
 *     arrival reading rather than as a second kind of fault.
 *   - A radar with no row in either stage reads `NO_READING`. Sentinel's
 *     backend does not see it, and radarca's opinion of it is not evidence.
 *
 * A radar that is arriving fine and has dropped out of the composite keeps its
 * `LB2` verdict here. That fault belongs to the composite rather than to the
 * radar, and it shows on the Products rail, named by which radars are missing.
 * Each rail answers one question.
 *
 * An `error` row reads through like any other status, because a check that
 * broke is not the same as a check that was never registered, and `skip` would
 * claim the second.
 *
 * A deployment with no backend row at all keeps reading `L2`.
 * the test deployment is that case, and graying out every pin there would
 * answer a different question. The tier is decided by rows rather than by a
 * stage key, so an empty `LB2` falls back instead of graying the fleet.
 *
 * Takes the `stages` map rather than one stage's rows, so a caller cannot pass
 * a single stage and get a confident-looking answer about a radar that stage
 * never saw.
 */
export function radarVerdicts(
	stages: Record<string, RadarReading[]> | null | undefined
): Record<string, RadarVerdict> {
	const st = stages ?? {};
	const arrival = radarRows(st, ARRIVAL_STAGE);
	const processing = radarRows(st, PROCESSING_STAGE);
	const display = radarRows(st, DISPLAY_STAGE);
	const backendTier = arrival.length > 0 || processing.length > 0;

	const targets = new Set(
		[...arrival, ...processing, ...display].map((r) => r.target)
	);
	const out: Record<string, RadarVerdict> = {};
	for (const target of targets) {
		const order: [string, string | undefined][] = backendTier
			? [[ARRIVAL_STAGE, worstFor(arrival, target)],
			   [PROCESSING_STAGE, worstFor(processing, target)]]
			: [[DISPLAY_STAGE, worstFor(display, target)]];
		const chosen = order.find(([, status]) => status !== undefined);
		out[target] = chosen
			? { status: chosen[1] as string, from: chosen[0] }
			: { status: NO_READING, from: 'none' };
	}
	return out;
}

/**
 * The stages in the order a verdict considers them.
 *
 * Not `RADAR_STATUS_STAGES`, which is the set and is ordered by layer number.
 */
const VERDICT_ORDER = [ARRIVAL_STAGE, PROCESSING_STAGE, DISPLAY_STAGE] as const;

/** What one stage says about one radar, for a hover card or a detail row. */
export interface StageReading {
	stage: string;
	/** Short noun for what the stage observes, for a label. */
	subject: string;
	status: string;
}

/**
 * What each stage says about one radar, in the order a verdict considers them.
 *
 * The verdict collapses to one color and drops the rest, which is right for a
 * pin and wrong for the card that opens over it. A radar arriving on time and
 * missing from the composite reads `pass`, and an operator looking at that pin
 * needs the second row to know why the composite is short.
 *
 * Stages with no row for the radar are left out rather than listed as `skip`,
 * because a deployment's registered stages are a property of its profile and
 * a row that was never registered is not a reading.
 *
 * @param stages The rollup's stage map.
 * @param target A radar id.
 * @returns One entry per stage that reported the radar, worst row per stage.
 */
export function radarStageReadings(
	stages: Record<string, RadarReading[]> | null | undefined,
	target: string
): StageReading[] {
	const subjects: Record<string, string> = {
		[ARRIVAL_STAGE]: 'arrival',
		[PROCESSING_STAGE]: 'composite',
		[DISPLAY_STAGE]: 'radarca'
	};
	const out: StageReading[] = [];
	for (const stage of VERDICT_ORDER) {
		const status = worstFor(radarRows(stages ?? {}, stage), target);
		if (status === undefined) continue;
		out.push({ stage, subject: subjects[stage], status });
	}
	return out;
}

/** Collapsed status per radar, for callers that do not need the source. */
export function radarStatusByTarget(
	stages: Record<string, RadarReading[]> | null | undefined
): Record<string, string> {
	const out: Record<string, string> = {};
	for (const [target, v] of Object.entries(radarVerdicts(stages))) out[target] = v.status;
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
