// Web Push helpers. Exposed as a tiny module the /m/more page imports.
// All functions assume window/navigator are available (callers should
// guard with onMount / typeof window !== 'undefined').
import { url } from './origin';

// Returns Uint8Array<ArrayBuffer>, not the default Uint8Array<ArrayBufferLike>:
// PushManager.subscribe wants a BufferSource, and ArrayBufferLike also admits
// SharedArrayBuffer, which is not a valid BufferSource.
function urlBase64ToUint8Array(base64String: string): Uint8Array<ArrayBuffer> {
	const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
	const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
	const raw = atob(base64);
	const out = new Uint8Array(raw.length);
	for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
	return out;
}

export function pushCapable(): boolean {
	return (
		typeof window !== 'undefined' &&
		'serviceWorker' in navigator &&
		'PushManager' in window &&
		'Notification' in window
	);
}

export async function getRegistration(): Promise<ServiceWorkerRegistration | null> {
	if (!('serviceWorker' in navigator)) return null;
	// /m/* layout already registered with scope=/m/; getRegistration with no
	// arg returns the controlling SW for the current page (works on /m/more).
	return (await navigator.serviceWorker.getRegistration()) ?? null;
}

export async function currentSubscription(): Promise<PushSubscription | null> {
	const reg = await getRegistration();
	if (!reg) return null;
	return (await reg.pushManager.getSubscription()) ?? null;
}

export async function enablePush(): Promise<{ ok: boolean; reason?: string }> {
	if (!pushCapable()) return { ok: false, reason: 'browser does not support Web Push' };

	const perm = await Notification.requestPermission();
	if (perm !== 'granted') return { ok: false, reason: `notification permission ${perm}` };

	const reg = await getRegistration();
	if (!reg) return { ok: false, reason: 'service worker not ready' };

	// Get the server's VAPID public key.
	const keyResp = await fetch(url('/api/push/vapid_public'));
	if (!keyResp.ok) return { ok: false, reason: `vapid key fetch HTTP ${keyResp.status}` };
	const { public_key_b64 } = (await keyResp.json()) as { public_key_b64: string };

	let sub = await reg.pushManager.getSubscription();
	if (!sub) {
		sub = await reg.pushManager.subscribe({
			userVisibleOnly: true,
			applicationServerKey: urlBase64ToUint8Array(public_key_b64)
		});
	}

	// Send the subscription to the backend.
	const json = sub.toJSON();
	const postResp = await fetch(url('/api/push/subscribe'), {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({
			endpoint: json.endpoint,
			keys: json.keys
		})
	});
	if (!postResp.ok) return { ok: false, reason: `subscribe HTTP ${postResp.status}` };
	return { ok: true };
}

export async function disablePush(): Promise<{ ok: boolean; reason?: string }> {
	const sub = await currentSubscription();
	if (sub) {
		const json = sub.toJSON();
		// Best-effort backend cleanup first, then unsubscribe locally so
		// re-enabling on the same device picks up a fresh subscription.
		await fetch(url('/api/push/unsubscribe'), {
			method: 'DELETE',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify({ endpoint: json.endpoint })
		}).catch(() => {});
		await sub.unsubscribe().catch(() => {});
	}
	return { ok: true };
}
