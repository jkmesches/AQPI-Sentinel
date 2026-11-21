/**
 * Draw an element where it belongs on screen rather than where it is
 * declared.
 *
 * The console's dock holds one pane's controls, and those controls are
 * dozens of pieces of that pane's own state: its product, its moment, its
 * radars, its overlays and the handlers that write them. Threading all of
 * that up to the page as props would put a pane's internals in the page's
 * signature, and the page would then own a copy of every rule about which
 * combinations are legal.
 *
 * So the pane keeps the controls and this moves the element they are in.
 * Svelte keeps updating a node after it is reparented, because its
 * bindings reference the node and not its position, so the controls stay
 * live and stay the pane's.
 */

/** What an action gives Svelte back. */
interface Action {
	update: (selector: string) => void;
	destroy: () => void;
}

/**
 * Move a node into the element a selector names.
 *
 * @param node The element to move, from `use:portal`.
 * @param selector The destination, which must already be in the document.
 * @returns The action's update and destroy hooks.
 */
export function portal(node: HTMLElement, selector: string): Action {
	const place = (want: string): void => {
		const host = document.querySelector(want);
		// Left where it was declared when the destination is missing.
		// A control the reader cannot see is worse than one in the wrong
		// place, and a dock that has not mounted yet is the same case as
		// one that never will.
		if (host) host.appendChild(node);
	};
	place(selector);
	return {
		update: (want: string) => place(want),
		// Svelte unmounts the node from where it was declared, which is no
		// longer where it is, so this has to take it out itself.
		destroy: () => { node.remove(); }
	};
}
