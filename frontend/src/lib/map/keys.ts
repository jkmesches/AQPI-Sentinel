/**
 * Keyboard control for the console, as a pure mapping from a key to an act.
 *
 * Separate from the handler so the awkward parts are testable. Four of them
 * matter more than the mapping itself.
 *
 * A shortcut must not fire on a key the focused control handles itself. The
 * console has a product picker, a time slider, opacity sliders and a row of
 * buttons, and space, the arrows and the digits all mean something inside
 * those. A bare keydown listener turns typing a 2 into a product switch.
 *
 * It must still fire on the keys that control does not handle. Taking the
 * whole keyboard away from any focused control is the obvious rule and it is
 * wrong: clicking play and then pressing an arrow is ordinary, and under that
 * rule nothing happens, because a button never handled an arrow. Found by
 * driving the deployed page, where every later check failed after the play
 * button took focus.
 *
 * A shortcut must not fire with ctrl, meta or alt held. Ctrl+R reloads the
 * page and Cmd+Left goes back, and swallowing either because the map wanted
 * the key is worse than having no shortcut at all.
 *
 * The arrows cannot be shared with MapLibre. Its keyboard handler pans with
 * them whenever the canvas has focus, which it has as soon as anyone clicks
 * the map to read a value, so a bound arrow would pan and step at once. The
 * console takes the arrows for time, the caller disables that handler, and
 * the shifted arrows and the zoom keys below replace what it did, so
 * disabling it costs a keyboard user nothing.
 *
 * Caps Lock must not disable the letters. A one-character key is matched in
 * lower case, so K and k both play.
 */

/** What a key press asks the console to do. */
export type Action =
	| 'play'
	| 'step-back'
	| 'step-forward'
	| 'first'
	| 'live'
	| 'product-next'
	| 'product-prev'
	| 'compare'
	| 'panel'
	| 'help'
	| 'close'
	| 'pan-left'
	| 'pan-right'
	| 'pan-up'
	| 'pan-down'
	| 'zoom-in'
	| 'zoom-out';

/** The parts of a key event this needs. */
export interface KeyLike {
	key: string;
	ctrlKey?: boolean;
	metaKey?: boolean;
	altKey?: boolean;
	shiftKey?: boolean;
	/** Where the press landed, for the typing check. */
	target?: EventTargetLike | null;
}

/** The parts of an event target this needs. */
export interface EventTargetLike {
	tagName?: string;
	isContentEditable?: boolean;
	/** Set on a custom control that takes keys of its own. */
	getAttribute?: (name: string) => string | null;
}

/**
 * What kind of control has focus, as far as the keyboard is concerned.
 *
 * A focused control owns the keys it handles itself and no others. Treating
 * every focused control as owning everything is the obvious rule and it is
 * wrong: clicking the play button and then pressing an arrow is an ordinary
 * thing to do, and under that rule nothing happens, because a button is
 * taken to own a key it has never handled.
 */
export type ControlKind = 'text' | 'list' | 'slider' | 'button' | null;

/** Keys that move within a control. */
const NAV_KEYS = new Set([
	'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown',
	'Home', 'End', 'PageUp', 'PageDown'
]);

/** Keys that press a control. */
const ACTIVATE_KEYS = new Set([' ', 'Enter']);

/** Input types that are a slider rather than a field. */
const SLIDER_TYPES = new Set(['range']);

/** Input types that are pressed rather than typed into. */
const BUTTON_TYPES = new Set(['checkbox', 'radio', 'button', 'submit', 'reset']);

/** Roles a control built out of divs uses to say what it is. */
const ROLE_KINDS: Readonly<Record<string, ControlKind>> = {
	textbox: 'text',
	searchbox: 'text',
	combobox: 'list',
	listbox: 'list',
	spinbutton: 'slider',
	slider: 'slider',
	button: 'button',
	checkbox: 'button',
	switch: 'button',
	radio: 'button',
	tab: 'button'
};

/** Key to action with no modifier held. */
export const BINDINGS: Readonly<Record<string, Action>> = {
	' ': 'play',
	'k': 'play',
	'ArrowLeft': 'step-back',
	'ArrowRight': 'step-forward',
	'j': 'step-back',
	'l': 'step-forward',
	'Home': 'first',
	'End': 'live',
	'ArrowUp': 'product-prev',
	'ArrowDown': 'product-next',
	'c': 'compare',
	'[': 'panel',
	'?': 'help',
	'Escape': 'close',
	'+': 'zoom-in',
	'=': 'zoom-in',
	'-': 'zoom-out',
	'_': 'zoom-out'
};

/**
 * Key to action with shift held, searched before `BINDINGS`.
 *
 * These are what MapLibre's own handler would have done with the arrows, so
 * taking the bare arrows for time does not remove keyboard panning. A key
 * missing here falls through to `BINDINGS`, so `?` works both on a layout
 * that needs shift for it and on one that does not.
 */
export const SHIFT_BINDINGS: Readonly<Record<string, Action>> = {
	'ArrowLeft': 'pan-left',
	'ArrowRight': 'pan-right',
	'ArrowUp': 'pan-up',
	'ArrowDown': 'pan-down'
};

/**
 * What kind of control the press landed on.
 *
 * The explicit role wins over the tag, since a div with `role="slider"` is a
 * slider and a `<div>` with no role is not a control at all.
 *
 * @param target Where the press landed.
 * @returns The kind, or null when nothing focusable has it.
 */
export function controlKind(
	target: EventTargetLike | null | undefined
): ControlKind {
	if (!target) return null;
	if (target.isContentEditable) return 'text';
	const role = target.getAttribute?.('role') ?? null;
	if (role !== null && role in ROLE_KINDS) return ROLE_KINDS[role];
	const tag = (target.tagName ?? '').toUpperCase();
	if (tag === 'TEXTAREA') return 'text';
	if (tag === 'SELECT' || tag === 'OPTION') return 'list';
	if (tag === 'BUTTON') return 'button';
	if (tag === 'INPUT') {
		const type = (target.getAttribute?.('type') ?? 'text').toLowerCase();
		if (SLIDER_TYPES.has(type)) return 'slider';
		if (BUTTON_TYPES.has(type)) return 'button';
		return 'text';
	}
	return null;
}

/**
 * Whether a focused control handles this key itself.
 *
 * Text entry owns every key, because any of them may be part of what someone
 * is typing. The others own only what they act on, so the console still
 * answers a step key with the play button focused, and still answers the
 * zoom keys with a product selected.
 *
 * @param target Where the press landed.
 * @param key The key, already lower-cased when it is one character.
 * @returns True when the console must keep its hands off.
 */
export function ownsKey(
	target: EventTargetLike | null | undefined,
	key: string
): boolean {
	const kind = controlKind(target);
	if (kind === null) return false;
	if (kind === 'text') return true;
	if (kind === 'button') return ACTIVATE_KEYS.has(key);
	if (kind === 'slider') return NAV_KEYS.has(key);
	// A list jumps to an option as soon as a character is typed, so every
	// one-character key is its own as well as the navigation keys.
	return NAV_KEYS.has(key) || ACTIVATE_KEYS.has(key) || key.length === 1;
}

/**
 * Whether a press landed in text entry, where no shortcut may fire.
 *
 * @param target Where the press landed.
 * @returns True when someone is typing.
 */
export function isTyping(target: EventTargetLike | null | undefined): boolean {
	return controlKind(target) === 'text';
}

/**
 * What a key press asks for, or null when the console should ignore it.
 *
 * @param e The press.
 * @returns The action, or null.
 */
export function actionForKey(e: KeyLike): Action | null {
	if (e.ctrlKey || e.metaKey || e.altKey) return null;
	const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
	if (ownsKey(e.target, key)) return null;
	if (e.shiftKey && key in SHIFT_BINDINGS) return SHIFT_BINDINGS[key];
	return BINDINGS[key] ?? null;
}

/**
 * Every shortcut, for the help panel, in the order it reads best.
 *
 * Each row carries the actions it covers so the list can be checked against
 * the tables above rather than kept in step by hand. A row groups keys where
 * grouping reads better, so the check is that the rows and the tables name
 * the same set of actions, not that there is a row per action.
 *
 * The arrows are spelled out. As glyphs they came from a fallback font,
 * since the monospace face this panel uses has none, and rendered at about
 * half the size of the letters beside them.
 */
export const SHORTCUTS: readonly {
	keys: string;
	does: string;
	actions: readonly Action[];
}[] = [
	{ keys: 'Space or K', does: 'play and pause', actions: ['play'] },
	{ keys: 'Left or J', does: 'one step back', actions: ['step-back'] },
	{ keys: 'Right or L', does: 'one step forward', actions: ['step-forward'] },
	{ keys: 'Home', does: 'oldest frame', actions: ['first'] },
	{ keys: 'End', does: 'jump to live', actions: ['live'] },
	{
		keys: 'Up and Down',
		does: 'previous and next product',
		actions: ['product-prev', 'product-next']
	},
	{
		keys: 'Shift and an arrow',
		does: 'pan the map',
		actions: ['pan-left', 'pan-right', 'pan-up', 'pan-down']
	},
	{
		keys: '+ and -',
		does: 'zoom in and out',
		actions: ['zoom-in', 'zoom-out']
	},
	{
		keys: 'C',
		does: 'show or hide the second pane',
		actions: ['compare']
	},
	{
		keys: '[',
		does: 'show or hide the layers panel',
		actions: ['panel']
	},
	{ keys: '?', does: 'this list', actions: ['help'] },
	{ keys: 'Esc', does: 'close this list', actions: ['close'] }
];

/** How far a shifted arrow pans, as a fraction of the shorter map edge. */
export const PAN_FRACTION = 0.3;

/** How much a zoom key changes the zoom. */
export const ZOOM_STEP = 0.5;

/** Which action pans which way, in screen pixels per unit of pan distance. */
export const PAN_VECTORS: Readonly<Record<string, readonly [number, number]>> = {
	'pan-left': [-1, 0],
	'pan-right': [1, 0],
	'pan-up': [0, -1],
	'pan-down': [0, 1]
};

/**
 * The product one step from the current one.
 *
 * Wraps, so holding the key walks the list rather than stopping at an end.
 * Returns the first product when the list does not contain the current one,
 * which happens on a deployment that has just dropped one.
 *
 * @param products The products this deployment publishes, in picker order.
 * @param current The selected product.
 * @param step 1 for the next, -1 for the previous.
 * @returns The product to select.
 */
export function stepProduct(
	products: readonly string[],
	current: string,
	step: number
): string {
	if (products.length === 0) return current;
	const at = products.indexOf(current);
	if (at < 0) return products[0];
	const n = products.length;
	return products[(((at + step) % n) + n) % n];
}

/**
 * How far a shifted arrow moves the camera, in screen pixels.
 *
 * Taken from the shorter edge so one press covers the same share of the view
 * in a tall pane and a wide one. Two panes side by side are half as wide as
 * one, and a fixed pixel step would move the compared views by different
 * fractions of what each shows.
 *
 * @param widthPx The map's width in CSS pixels.
 * @param heightPx The map's height in CSS pixels.
 * @returns The distance one press pans, in pixels.
 */
export function panDistance(widthPx: number, heightPx: number): number {
	const shorter = Math.min(widthPx, heightPx);
	if (!Number.isFinite(shorter) || shorter <= 0) return 0;
	return shorter * PAN_FRACTION;
}

/**
 * The pixel offset a pan action asks for.
 *
 * @param action The action, which may be any action.
 * @param widthPx The map's width in CSS pixels.
 * @param heightPx The map's height in CSS pixels.
 * @returns The offset in pixels, or null when the action is not a pan.
 */
export function panOffset(
	action: Action,
	widthPx: number,
	heightPx: number
): [number, number] | null {
	const vector = PAN_VECTORS[action];
	if (!vector) return null;
	const d = panDistance(widthPx, heightPx);
	return [vector[0] * d, vector[1] * d];
}
