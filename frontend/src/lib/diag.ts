/**
 * Debug feature-flags read from `?diag=...` URL params. Used to bisect
 * the idle-tab freeze: toggle subsystems off one at a time and leave the
 * tab idle until the combination that doesn't freeze identifies the
 * culprit.
 *
 * Recognised flags (comma-separated under `?diag=`):
 *   no-ws        — disable the WebSocket live-push subscription
 *   no-poll      — disable the 5s polling refresh
 *   no-map       — don't render MapView at all
 *   no-spark     — don't render Sparkline components
 *   no-tick      — disable the 1s clock tick in the layout
 *   no-pulse     — disable status-dot transition pulse animations
 *
 * The defaults are all-enabled. Read once at module load — refresh the
 * page to change settings.
 *
 * Example:
 *   /?diag=no-ws,no-map    leave Live for 30 min — if no freeze, we know
 *                          one of WebSocket or MapView is the culprit.
 */
const params = typeof window !== 'undefined'
	? new URLSearchParams(window.location.search)
	: new URLSearchParams();
const raw = (params.get('diag') ?? '').split(',').map((s) => s.trim()).filter(Boolean);
const off = new Set(raw);

export const diag = {
	ws:     !off.has('no-ws'),
	poll:   !off.has('no-poll'),
	map:    !off.has('no-map'),
	spark:  !off.has('no-spark'),
	tick:   !off.has('no-tick'),
	pulse:  !off.has('no-pulse'),
	raw,
	anyDisabled: raw.length > 0
};
