// Typed fetch wrapper for the Sentinel REST API.

import { url } from './origin';

export interface StatusRow {
	check_id: string;
	target: string;
	stage: string;
	status: 'pass' | 'warn' | 'fail' | 'error' | 'skip';
	started_at: string;
	finished_at: string;
	summary: string | null;
}
export interface StatusRollup {
	at: string;
	stages: Record<string, StatusRow[]>;
	counts: Record<string, { pass: number; warn: number; fail: number; error: number; total: number }>;
}
export interface CheckMeta {
	id: string;
	stage: string;
	target: string;
	cadence_s: number;
	depends_on: string[];
}
export interface CheckRun extends StatusRow {
	id: number;
	payload: Record<string, unknown> | null;
	artifacts: string[];
}
export interface AlarmAck { acked_by: string; acked_at: string | null; note: string | null }
export interface Alarm {
	id: number;
	check_id: string;
	target: string;
	stage: string;
	severity: 'info' | 'warn' | 'critical';
	opened_at: string;
	closed_at: string | null;
	suppressed_by: string | null;
	message: string;
	payload: Record<string, unknown> | null;
	ack?: AlarmAck | null;
}

async function get<T>(path: string, timeoutMs = 8000): Promise<T> {
	const ctl = new AbortController();
	const t = setTimeout(() => ctl.abort(), timeoutMs);
	try {
		const r = await fetch(url(path), {
			headers: { accept: 'application/json' },
			credentials: 'include',
			signal: ctl.signal
		});
		if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
		return (await r.json()) as T;
	} finally {
		clearTimeout(t);
	}
}

export interface TimelineCell { status: 'pass' | 'warn' | 'fail' | 'error' | 'skip'; n: number }
export interface TimelineBucket { ts: string; cells: Record<string, TimelineCell> }
export interface TimelinePage {
	bucket: string;
	bucket_s: number;
	until: string;
	since: string;
	buckets: TimelineBucket[];
	older_cursor: string;
}

export const api = {
	status:        ()                  => get<StatusRollup>('/api/status'),
	checks:        ()                  => get<CheckMeta[]>('/api/checks'),
	timeline:      (bucket: string, limit = 120, until?: string) => {
		const p = new URLSearchParams({ bucket, limit: String(limit) });
		if (until) p.set('until', until);
		return get<TimelinePage>(`/api/history/timeline?${p}`, 15000);
	},
	historyRuns:   (check_id: string, target: string, since: string, until: string, limit = 50) => {
		const p = new URLSearchParams({ check_id, target, since, until, limit: String(limit) });
		return get<CheckRun[]>(`/api/history/runs?${p}`, 15000);
	},
	latest:        (id: string)        => get<CheckRun>(`/api/checks/${id}/latest`),
	history:       (id: string, n = 50) => get<CheckRun[]>(`/api/checks/${id}/history?limit=${n}`),
	metric:        (id: string, m: string, n = 60) =>
		get<{ ts: string; value: number }[]>(`/api/checks/${id}/metrics?metric=${m}&limit=${n}`),
	alarms:        (status = 'open')  => get<Alarm[]>(`/api/alarms?status=${status}&limit=200`),
	alarm:         (id: number)       => get<Alarm & { ack: { acked_by: string; acked_at: string; note: string } | null; notifications: unknown[] }>(`/api/alarms/${id}`),
	ack:           (id: number, body: { note?: string } = {}) =>
		fetch(url(`/api/alarms/${id}/ack`), {
			method: 'POST',
			headers: { 'content-type': 'application/json' },
			credentials: 'include',
			body: JSON.stringify(body)
		}).then((r) => {
			if (!r.ok) throw new Error(`ack → HTTP ${r.status}`);
			return r.json();
		}),
	unack:         (id: number) =>
		fetch(url(`/api/alarms/${id}/unack`), { method: 'POST', credentials: 'include' }).then((r) => {
			if (!r.ok) throw new Error(`unack → HTTP ${r.status}`);
			return r.json();
		}),
	silences:      ()                  => get<unknown[]>('/api/silences')
};
