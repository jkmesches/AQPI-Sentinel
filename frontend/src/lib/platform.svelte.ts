// Platform detection + PWA install state.
//
// Sentinel's mobile shell targets BOTH iOS Safari and Android Chrome.
// They differ in how the user installs the PWA:
//   - iOS Safari: no programmatic install API. The user must tap
//     Share → "Add to Home Screen". We just show instructions.
//   - Android Chrome / Edge: fires `beforeinstallprompt` once the PWA
//     meets install criteria. We capture it and expose a button that
//     calls .prompt() directly — one-tap install.
//
// To make sure the event is captured even when the user navigates via
// SvelteKit client-side routing (the event only fires once per page
// load), the /m/+layout.svelte calls initInstallCapture() on mount.
// /m/more then reads from the reactive state below.

const _state = $state<{
	deferredPrompt: any;
	installed: boolean;
	pwaPromptReady: boolean;
}>({
	deferredPrompt: null,
	installed: false,
	pwaPromptReady: false
});

export const install = _state;

export function isIos(): boolean {
	if (typeof navigator === 'undefined') return false;
	return /iPhone|iPod/i.test(navigator.userAgent);
}

export function isAndroid(): boolean {
	if (typeof navigator === 'undefined') return false;
	return /Android/i.test(navigator.userAgent);
}

export function isStandalone(): boolean {
	if (typeof window === 'undefined') return false;
	return (
		window.matchMedia('(display-mode: standalone)').matches ||
		// Safari-specific non-standard flag.
		(navigator as any).standalone === true
	);
}

let _captureWired = false;

/** Wire up window-level listeners. Safe to call multiple times. */
export function initInstallCapture(): void {
	if (typeof window === 'undefined' || _captureWired) return;
	_captureWired = true;
	_state.installed = isStandalone();

	// beforeinstallprompt: Chrome / Edge / Samsung Internet. iOS Safari
	// does NOT fire this event. The handler preventDefault()s so the
	// browser holds the prompt; we trigger it explicitly via .prompt().
	window.addEventListener('beforeinstallprompt', (e: Event) => {
		e.preventDefault();
		_state.deferredPrompt = e;
		_state.pwaPromptReady = true;
	});

	// appinstalled: fires after a successful install from either path
	// (programmatic or "Install" entry in the browser menu).
	window.addEventListener('appinstalled', () => {
		_state.installed = true;
		_state.deferredPrompt = null;
		_state.pwaPromptReady = false;
	});

	// When the user launches the installed PWA, display-mode flips to
	// standalone. Listening keeps the UI in sync without a reload.
	const mq = window.matchMedia('(display-mode: standalone)');
	const onChange = () => (_state.installed = isStandalone());
	if (mq.addEventListener) mq.addEventListener('change', onChange);
	else mq.addListener(onChange);
}

/** Trigger the deferred install prompt. Returns 'accepted', 'dismissed',
 *  or 'unavailable' when the browser hasn't fired beforeinstallprompt
 *  (iOS, already-installed PWA, or PWA criteria not yet met). */
export async function promptInstall(): Promise<'accepted' | 'dismissed' | 'unavailable'> {
	const dp = _state.deferredPrompt;
	if (!dp) return 'unavailable';
	try {
		await dp.prompt();
		const choice = await dp.userChoice;
		_state.deferredPrompt = null;
		_state.pwaPromptReady = false;
		return choice.outcome === 'accepted' ? 'accepted' : 'dismissed';
	} catch {
		_state.deferredPrompt = null;
		_state.pwaPromptReady = false;
		return 'dismissed';
	}
}
