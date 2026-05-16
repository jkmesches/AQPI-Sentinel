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

	connect() {
		const proto = location.protocol === 'https:' ? 'wss' : 'ws';
		this.ws = new WebSocket(`${proto}://${location.host}/api/ws`);
		this.ws.onopen = () => {
			this.backoff = 1000;
		};
		this.ws.onmessage = (m) => {
			let e: WsEvent;
			try {
				e = JSON.parse(m.data);
			} catch {
				return;
			}
			if (e.type === 'ping') {
				try {
					this.ws?.send('pong');
				} catch { /* */ }
				return;
			}
			for (const fn of this.listeners) fn(e);
		};
		this.ws.onclose = () => {
			if (this.stopped) return;
			setTimeout(() => this.connect(), this.backoff);
			this.backoff = Math.min(this.backoff * 2, 15000);
		};
		this.ws.onerror = () => {
			try {
				this.ws?.close();
			} catch { /* */ }
		};
	}

	subscribe(fn: (e: WsEvent) => void) {
		this.listeners.push(fn);
	}

	stop() {
		this.stopped = true;
		try {
			this.ws?.close();
		} catch { /* */ }
	}
}
