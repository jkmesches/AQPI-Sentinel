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
	/** Which published tree a backend-reading check read, as a two-character
	 *  tag such as "FS" or "CB". Null for everything that does not read one.
	 *  CBAND is the reason this exists. It comes off its own tree while every
	 *  other backend row comes off the profile's main one, and the UI cannot
	 *  know that. The deployment sets the text, see config.BACKEND_SOURCE. */
	source_tag?: string | null;
	/** The full name of the same thing, for row labels and tooltips. */
	source_label?: string | null;
	/** Whether the check's readings come from radarca, decided by the
	 *  backend's own rule (layer0_episode.reads_radarca). Absent from a
	 *  backend older than the timeline's RadarCA filter. */
	reads_radarca?: boolean;
	/** Whether the check watches a NEXRAD radar (config.NEXRAD_RADARS).
	 *  Absent from a backend older than the dashboard's NEXRAD filter. */
	nexrad?: boolean;
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

/** One (check, metric) series and the samples it returned. */
export type MetricSeries = Record<string, { ts: string; value: number }[]>;

/**
 * Most series `metrics_recent` accepts in one request.
 *
 * Mirrors `_MAX_SERIES` in backend/api/routes/checks.py, which raises 400
 * above it. Measured against the AQPI deployment: 64 series returns 200 and 65 returns
 * 400. Keep the two in step, and lower this one first if the cap ever drops.
 */
export const METRICS_SERIES_MAX = 64;

/**
 * Split a series list into requests the endpoint will accept.
 *
 * The sparkline refresh sent every series in one request, which is fine until
 * a deployment registers more checks than the cap. The AQPI deployment has 84 against a
 * cap of 64, so every poll returned 400 and no sample reached the browser
 * after the initial seed. The two smaller deployments sit under the cap at 45
 * and 10, so only production was ever large enough to cross it.
 *
 * @param keys `check_id|metric` strings, in any order.
 * @param size Largest batch to emit. Must be at least 1.
 * @returns Batches covering every key once, in the original order.
 * @raises RangeError If `size` is below 1, which would loop forever.
 */
export function chunkSeries(keys: string[], size = METRICS_SERIES_MAX): string[][] {
	if (!Number.isInteger(size) || size < 1) {
		throw new RangeError(`chunkSeries size must be a positive integer, got ${size}`);
	}
	const out: string[][] = [];
	for (let i = 0; i < keys.length; i += size) out.push(keys.slice(i, i + size));
	return out;
}

/**
 * Fold per-batch responses into one map.
 *
 * A key appearing in more than one batch keeps the samples from both, because
 * dropping either would punch a hole in a trace. Callers de-dupe by timestamp
 * already, so repeats here are harmless.
 *
 * @param parts Responses in batch order.
 * @returns One map over every key any batch returned.
 */
export function mergeSeries(parts: MetricSeries[]): MetricSeries {
	const out: MetricSeries = {};
	for (const part of parts) {
		for (const [k, pts] of Object.entries(part ?? {})) {
			if (!pts?.length) continue;
			out[k] = out[k] ? [...out[k], ...pts] : [...pts];
		}
	}
	return out;
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
	/** Per-status run counts within the bucket. The cell's COLOR comes from
	 *  the worst status present; these say how MUCH of the bucket was at that
	 *  status, which is what stops coarse grains reading as uniformly broken. */
	n_fail?: number;
	n_error?: number;
	n_warn?: number;
	/** Skipped runs in the bucket. Distinct from pass: the check ran and
	 *  declined to judge. Absent from servers older than 2026-09-05, which
	 *  skipFraction treats as "unknown", not as zero-skip. */
	n_skip?: number;
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
	// Many series in one request, optionally only samples newer than `since`.
	// Used by the sparkline refresh on every poll tick — see
	// SentinelState.refreshSparklinesIncremental for why polling is required
	// rather than relying on the WebSocket.
	// Batched, because the endpoint caps a single request at METRICS_SERIES_MAX
	// and a deployment can register more checks than that. Batches run
	// concurrently: they are independent reads and a sequential loop would put
	// the whole poll behind the slowest one.
	metricsRecent: async (series: string[], since?: string, limit = 40) => {
		const parts = await Promise.all(
			chunkSeries(series).map((batch) =>
				get<MetricSeries>(
					`/api/checks/-/metrics_recent?series=${encodeURIComponent(batch.join(','))}` +
					(since ? `&since=${encodeURIComponent(since)}` : '') +
					`&limit=${limit}`
				)
			)
		);
		return mergeSeries(parts);
	},
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
