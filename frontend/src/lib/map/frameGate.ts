/**
 * Holding both panes' frame swaps until both are ready.
 *
 * Each pane already has a barrier of its own, so its composite, its NEXRAD
 * tile and its radar overlays swap together rather than arriving in
 * stages. Across two panes there was nothing: the pane that loaded first
 * painted the new minute while the other was still fetching, so a
 * comparison showed one product at 04:52 beside the other at 04:50 for as
 * long as the slower load took. A reader comparing two products cannot see
 * that the panes are a frame apart, which makes it the same fault as
 * letting them drift in time, and that is the fault compare mode exists to
 * avoid.
 *
 * The timeout is the part that needs care. A pane whose product has no
 * frame inside the shared window never arrives at all, and a gate that
 * waited for it would stop the other pane from ever painting. So the gate
 * releases whoever has arrived once the wait has passed, and the result is
 * the old behavior rather than a stall.
 *
 * No timers here. The caller owns the clock, because a module that sets
 * its own cannot be tested without faking one, and the timing is the part
 * worth testing.
 */

import type { FrameApply } from './layers/registry';

/**
 * How long one pane waits for the other, in milliseconds.
 *
 * Measured on the deployed console on 2026-11-21, stepping six frames in
 * compare mode with the stagger between the two panes' arrivals recorded
 * for each. The arrivals fall into two groups and the gap between them is
 * what sets this number.
 *
 * A step drives both panes, and those pairs arrived 3, 22, 32, 47, 68 and
 * 156 ms apart. Everything else came from the panes' own refresh cycles,
 * the 5 s poll and the 2 min overlay sweep, and those arrived 2.2 to 11.4
 * seconds apart. They are not a pair at all, so holding one for the other
 * would delay a pane for no reason.
 *
 * 400 ms sits well above the slowest real pair and far below the fastest
 * unrelated one, so every step releases together and every background
 * reload releases on its own.
 */
export const GATE_WAIT_MS = 400;

/** One pane's frame, waiting for the other. */
export interface Pending {
	pane: number;
	applies: FrameApply[];
	/** When it arrived, on the caller's clock. */
	at: number;
}

/** Who is waiting, and for how many. */
export interface GateState {
	/** How many panes are open, and so how many to wait for. */
	expected: number;
	pending: Pending[];
}

/**
 * A gate expecting one pane.
 *
 * @returns A fresh state.
 */
export function newGate(): GateState {
	return { expected: 1, pending: [] };
}

/**
 * Say how many panes are open.
 *
 * Releases anything already waiting when the count drops, because a pane
 * that has closed will never arrive and the frames held for it would
 * otherwise sit there until the next step.
 *
 * @param gate The state, mutated.
 * @param count How many panes are open now.
 * @returns Thunks to fire at once, empty when nothing was released.
 */
export function setExpected(gate: GateState, count: number): FrameApply[] {
	const want = Number.isFinite(count) ? Math.max(1, Math.trunc(count)) : 1;
	gate.expected = want;
	if (gate.pending.length >= want) return drain(gate);
	return [];
}

/**
 * A pane's frame is ready.
 *
 * A pane that arrives twice before the other arrives once replaces its own
 * entry. The newer frame is the one to paint, and firing both would swap
 * the same layer twice in one tick.
 *
 * @param gate The state, mutated.
 * @param pane Which pane.
 * @param applies Its swaps.
 * @param at The caller's clock.
 * @returns Every pane's thunks when all have arrived, or null to wait.
 */
export function arrive(
	gate: GateState,
	pane: number,
	applies: readonly FrameApply[],
	at: number
): FrameApply[] | null {
	const existing = gate.pending.findIndex((p) => p.pane === pane);
	const entry: Pending = { pane, applies: [...applies], at };
	if (existing >= 0) {
		gate.pending[existing] = entry;
	} else {
		gate.pending.push(entry);
	}
	if (gate.pending.length >= gate.expected) return drain(gate);
	return null;
}

/**
 * Whether the wait has run out for whoever arrived first.
 *
 * Measured from the earliest arrival, not the latest, so a pane that keeps
 * re-arriving cannot hold the other past the wait.
 *
 * @param gate The state.
 * @param now The caller's clock.
 * @param waitMs How long a pane waits.
 * @returns True when the gate should release what it has.
 */
export function overdue(
	gate: GateState,
	now: number,
	waitMs: number = GATE_WAIT_MS
): boolean {
	if (gate.pending.length === 0) return false;
	const first = Math.min(...gate.pending.map((p) => p.at));
	return now - first >= waitMs;
}

/**
 * Take everything waiting, in pane order.
 *
 * Pane order rather than arrival order, so the same two frames swap in the
 * same sequence every time. It makes no difference to what is on screen
 * after the tick, and it makes a trace of the swaps readable.
 *
 * @param gate The state, mutated.
 * @returns Every pending thunk.
 */
export function drain(gate: GateState): FrameApply[] {
	const ordered = [...gate.pending].sort((a, b) => a.pane - b.pane);
	gate.pending = [];
	return ordered.flatMap((p) => p.applies);
}

/**
 * Whether anything is waiting.
 *
 * @param gate The state.
 * @returns True when at least one pane has arrived and not been released.
 */
export function isWaiting(gate: GateState): boolean {
	return gate.pending.length > 0;
}
