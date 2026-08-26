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
	// `skip` is sent by the backend and read by the header strips; it was
	// missing here, so every `counts.skip` access was a type error.
	counts: Record<string, { pass: number; warn: number; fail: number; error: number; skip: number; total: number }>;
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
		// No `credentials: 'include'`: auth rides on the Authorization
		// header (installFetchPrefix attaches the Bearer token). With
		// credentials:include and allow_origins=["*"], the browser blocks
		// the response because ACA-Origin can't be `*` for credentialed
		// requests — which broke all side-panel data in prod.
		const r = await fetch(url(path), {
			headers: { accept: 'application/json' },
			signal: ctl.signal
		});
		if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
		return (await r.json()) as T;
	} finally {
		clearTimeout(t);
	}
}

export interface TimelineCell {
	status: 'pass' | 'warn' | 'fail' | 'error' | 'skip';
	n: number;
	/** Optional.
	 *  'upstream_unhealthy' — the bucket's runs were cascade-demoted because
	 *    an upstream dependency was unhealthy (distinct from an intrinsic
	 *    skip, e.g. a forecast product skipping a sub-check).
	 *  'upstream_api'       — the check could not determine state at all
	 *    because the upstream API errored or timed out. This is a gap in our
	 *    visibility, NOT evidence the monitored thing is broken. */
	reason?: 'upstream_unhealthy' | 'upstream_api';
}
export interface TimelineBucket { ts: string; cells: Record<string, TimelineCell> }
export interface TimelinePage {
	bucket: string;
	bucket_s: number;
	until: string;
	since: string;
	buckets: TimelineBucket[];
	/** null once the grid has reached the oldest row we actually hold — the
	 *  cursor itself is pure arithmetic and would otherwise page forever. */
	older_cursor: string | null;
	/** Oldest finished_at in check_runs, or null if the table is empty. */
	oldest_available: string | null;
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
			body: JSON.stringify(body)
		}).then((r) => {
			if (!r.ok) throw new Error(`ack → HTTP ${r.status}`);
			return r.json();
		}),
	unack:         (id: number) =>
		fetch(url(`/api/alarms/${id}/unack`), { method: 'POST' }).then((r) => {
			if (!r.ok) throw new Error(`unack → HTTP ${r.status}`);
			return r.json();
		}),
	silences:      ()                  => get<unknown[]>('/api/silences')
};
