// Typed fetch wrapper for the Sentinel REST API.

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
}

async function get<T>(path: string, timeoutMs = 8000): Promise<T> {
	const ctl = new AbortController();
	const t = setTimeout(() => ctl.abort(), timeoutMs);
	try {
		const r = await fetch(path, { headers: { accept: 'application/json' }, signal: ctl.signal });
		if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
		return (await r.json()) as T;
	} finally {
		clearTimeout(t);
	}
}

export const api = {
	status:        ()                  => get<StatusRollup>('/api/status'),
	checks:        ()                  => get<CheckMeta[]>('/api/checks'),
	latest:        (id: string)        => get<CheckRun>(`/api/checks/${id}/latest`),
	history:       (id: string, n = 50) => get<CheckRun[]>(`/api/checks/${id}/history?limit=${n}`),
	metric:        (id: string, m: string, n = 60) =>
		get<{ ts: string; value: number }[]>(`/api/checks/${id}/metrics?metric=${m}&limit=${n}`),
	alarms:        (status = 'open')  => get<Alarm[]>(`/api/alarms?status=${status}&limit=200`),
	alarm:         (id: number)       => get<Alarm & { ack: { acked_by: string; acked_at: string; note: string } | null; notifications: unknown[] }>(`/api/alarms/${id}`),
	ack:           (id: number, body: { user?: string; note?: string }) =>
		fetch(`/api/alarms/${id}/ack`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }).then(r => r.json()),
	unack:         (id: number) =>
		fetch(`/api/alarms/${id}/unack`, { method: 'POST' }).then(r => r.json()),
	silences:      ()                  => get<unknown[]>('/api/silences')
};
