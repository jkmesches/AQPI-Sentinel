import { redirect, type Handle } from '@sveltejs/kit';

// Catches phones (Mobi / iPhone / iPod / Android Mobile) but NOT tablets,
// because tablets typically have screen real estate to use the desktop UI.
const PHONE_UA = /Mobi|iPhone|iPod|Android.*Mobile/i;

// Set by /m/more's "View desktop site" link. Once present, the server hook
// stops auto-redirecting that browser regardless of UA. Clearing the cookie
// (or visiting /?mobile=1) re-enables the redirect.
const DESKTOP_PREF_COOKIE = 'sentinel-desktop';

export const handle: Handle = async ({ event, resolve }) => {
	const path = event.url.pathname;
	const search = event.url.searchParams;

	// Allow an explicit ?mobile=1 to clear the desktop opt-out and bounce
	// to /m. Useful when sharing a phone with someone who set "desktop"
	// once, or when toggling back via a deep link.
	if (search.get('mobile') === '1') {
		event.cookies.delete(DESKTOP_PREF_COOKIE, { path: '/' });
		throw redirect(302, '/m');
	}

	// UA-sniff redirect: only on entry-point hits (root). Don't trap users
	// who deep-link into desktop pages like /timeline or /admin/* — they
	// presumably know what they want.
	if (path === '/') {
		const ua = event.request.headers.get('user-agent') ?? '';
		const wantsDesktop = event.cookies.get(DESKTOP_PREF_COOKIE) === '1';
		if (PHONE_UA.test(ua) && !wantsDesktop) {
			throw redirect(302, '/m');
		}
	}

	return resolve(event);
};
