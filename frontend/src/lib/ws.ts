// Resilient WebSocket subscriber. Auto-reconnects with backoff.

import type { Alarm } from '$lib/api';

export interface RunEvent {
	check_id: string;
	target: string;
	stage: string;
	status: string;
	started_at: string;
	finished_at: string;
	summary: string;
	payload: Record<string, unknown> | null;
	metrics: Record<string, number> | null;
}

export type WsEvent =
	| { type: 'hello'; sentinel: { version: string; checks: number; stages: string[]; at: string } }
	| { type: 'run'; run: RunEvent }
	| { type: 'alarm_open'; alarm: Alarm }
	| { type: 'alarm_close'; alarm: Alarm & { closed_at: string } }
	| { type: 'alarm_promote'; alarm: { id: number; severity: string } }
	| { type: 'ping' };

export class SentinelWs {
	private ws?: WebSocket;
	private backoff = 1000;
	private listeners: ((e: WsEvent) => void)[] = [];
	private stopped = false;
	// Monotonic connect id — invalidates stale onclose handlers from
	// previously-discarded WebSocket instances so they can't trigger
	// another reconnect after we've already started a new one.
	private connectId = 0;
	private reconnectTimer?: ReturnType<typeof setTimeout>;

	connect() {
		if (this.stopped) return;
		if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
			return;  // already have a live socket
		}
		const myId = ++this.connectId;
		const proto = location.protocol === 'https:' ? 'wss' : 'ws';
		const ws = new WebSocket(`${proto}://${location.host}/api/ws`);
		this.ws = ws;
		ws.onopen = () => {
			if (myId !== this.connectId) return;
			this.backoff = 1000;
		};
		ws.onmessage = (m) => {
			if (myId !== this.connectId) return;
			let e: WsEvent;
			try { e = JSON.parse(m.data); } catch { return; }
			if (e.type === 'ping') {
				try { ws.send('pong'); } catch { /* */ }
				return;
			}
			for (const fn of this.listeners) fn(e);
		};
		ws.onclose = () => {
			// Only the current connect-id schedules a reconnect; a stale
			// close from a previously-discarded WS is ignored.
			if (myId !== this.connectId || this.stopped) return;
			if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
			this.reconnectTimer = setTimeout(() => this.connect(), this.backoff);
			this.backoff = Math.min(this.backoff * 2, 15000);
		};
		ws.onerror = () => {
			// Don't call close() here — onclose will fire on its own and
			// drive the reconnect path. Calling close() from onerror used
			// to cause duplicate close events in some browsers.
		};
	}

	subscribe(fn: (e: WsEvent) => void) {
		this.listeners.push(fn);
	}

	stop() {
		this.stopped = true;
		this.connectId++;          // invalidate any in-flight handlers
		if (this.reconnectTimer) {
			clearTimeout(this.reconnectTimer);
			this.reconnectTimer = undefined;
		}
		try { this.ws?.close(); } catch { /* */ }
		this.ws = undefined;
		this.listeners.length = 0; // drop refs so GC can reclaim subscribers
	}
}
