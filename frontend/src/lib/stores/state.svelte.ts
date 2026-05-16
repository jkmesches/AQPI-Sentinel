// Reactive store. Initial snapshot from REST; live updates over WebSocket.

import { api, type StatusRollup, type Alarm, type CheckMeta } from '$lib/api';
import { SentinelWs, type WsEvent } from '$lib/ws';
import { diag } from '$lib/diag';

class SentinelState {
	rollup     = $state<StatusRollup | null>(null);
	alarms     = $state<Alarm[]>([]);
	checks     = $state<CheckMeta[]>([]);
	lastUpdate = $state<Date | null>(null);
	error      = $state<string | null>(null);
	loading    = $state(true);

	// Sparkline data: key = `${check_id}|${metric}`, value = oldest→newest values.
	metrics    = $state<Record<string, number[]>>({});
	// Per-check previous status, used to drive one-shot pulse animations.
	prevStatus = $state<Record<string, string>>({});
	pulseTick  = $state<Record<string, number>>({});

	private timer?: ReturnType<typeof setInterval>;
	private ws?: SentinelWs;
	private liveSince = $state<Date | null>(null);

	// Cheap fingerprints of the last successful fetch, used by refresh() to
	// skip the reactive replacement when the new payload is byte-identical
	// to the old. On a healthy idle system this means most refresh ticks
	// do zero subscriber work.
	private _rollupSig = '';
	private _alarmsSig = '';
	private _checksSig = '';

	async refresh() {
		// Each fetch settles independently — loading drops on the first
		// success, but the function only RESOLVES once all three are done so
		// callers like refreshSparklines() can chain reliably.
		let anyOk = false;
		const dropLoading = () => {
			this.loading = false;
			this.lastUpdate = new Date();
		};
		const tasks = [
			api.status().then(
				(r) => {
					anyOk = true;
					dropLoading();
					const sig = JSON.stringify(r);
					if (sig === this._rollupSig) return;
					this._rollupSig = sig;
					this.detectTransitions(r);
					this.rollup = r;
				},
				(e) => { this.error = (e as Error).message; dropLoading(); }
			),
			api.alarms('open').then(
				(a) => {
					anyOk = true;
					dropLoading();
					const sig = JSON.stringify(a);
					if (sig === this._alarmsSig) return;
					this._alarmsSig = sig;
					this.alarms = a;
				},
				(e) => { this.error = (e as Error).message; dropLoading(); }
			),
			api.checks().then(
				(c) => {
					anyOk = true;
					dropLoading();
					const sig = JSON.stringify(c);
					if (sig === this._checksSig) return;
					this._checksSig = sig;
					this.checks = c;
				},
				(e) => { this.error = (e as Error).message; dropLoading(); }
			)
		];
		await Promise.allSettled(tasks);
		if (anyOk) this.error = null;
		this.lastUpdate = new Date();
	}

	private detectTransitions(next: StatusRollup) {
		const cur = this.prevStatus;
		const upd: Record<string, number> = { ...this.pulseTick };
		for (const rows of Object.values(next.stages)) {
			for (const r of rows) {
				const prev = cur[r.check_id];
				if (prev !== undefined && prev !== r.status) {
					upd[r.check_id] = (upd[r.check_id] ?? 0) + 1;
				}
				cur[r.check_id] = r.status;
			}
		}
		this.pulseTick = upd;
	}

	async refreshSparklines() {
		if (!this.rollup) return;
		// Fire-and-forget per check; each result lands incrementally so the UI
		// can render the sparklines that *did* arrive without waiting on stragglers.
		const wanted: { check_id: string; metric: string }[] = [];
		for (const r of this.rollup.stages?.L2 ?? []) {
			wanted.push({ check_id: r.check_id, metric: 'images_Reflectivity' });
		}
		for (const r of this.rollup.stages?.L1 ?? []) {
			if (r.check_id.startsWith('layer1.product.')) {
				wanted.push({ check_id: r.check_id, metric: 'age_s' });
			}
		}
		for (const { check_id, metric } of wanted) {
			void api.metric(check_id, metric, 30).then(
				(pts) => {
					this.metrics = {
						...this.metrics,
						[`${check_id}|${metric}`]: pts.map((p) => p.value).reverse()
					};
				},
				() => { /* drop quietly; live WS will fill it as runs arrive */ }
			);
		}
	}

	private handleWs(e: WsEvent) {
		// When the tab is hidden, drop run + alarm mutations entirely. The
		// next visibilitychange triggers a full refresh that re-reads the
		// authoritative state from /api/status + /api/alarms. Without this,
		// a backgrounded tab keeps mutating reactive state for ~30 events/
		// minute, all of which apply at once when the tab comes back —
		// a known wedge trigger.
		if (typeof document !== 'undefined' && document.hidden) {
			if (e.type === 'hello') this.liveSince = new Date();
			return;
		}
		// Intentionally don't touch `lastUpdate` here — the 1s tickTimer in
		// the layout drives `sinceUpdate` on its own, and per-event writes
		// just trigger redundant reactive recomputation for every header
		// element that reads `lastUpdate`.
		if (e.type === 'hello') {
			this.liveSince = new Date();
			this.lastUpdate = new Date();
			return;
		}
		if (e.type === 'run') {
			this.mergeRun(e.run);
			if (e.run.metrics) {
				// Skip metrics whose value equals the last sample for the
				// same key — sparklines don't render any differently from
				// duplicate trailing samples, and avoiding the push prevents
				// flushMetrics from rebuilding the whole metrics map.
				let anyChanged = false;
				for (const [m, v] of Object.entries(e.run.metrics)) {
					const k = `${e.run.check_id}|${m}`;
					const arr = this.metrics[k];
					const last = arr && arr.length ? arr[arr.length - 1] : undefined;
					if (last !== v) { anyChanged = true; break; }
				}
				if (anyChanged) {
					if (!this.pendingMetrics) this.pendingMetrics = [];
					this.pendingMetrics.push({ check_id: e.run.check_id, metrics: e.run.metrics });
					if (!this.metricsScheduled) {
						this.metricsScheduled = true;
						queueMicrotask(() => this.flushMetrics());
					}
				}
			}
			return;
		}
		if (e.type === 'alarm_open') {
			const a = e.alarm as Alarm;
			if (!this.alarms.find((x) => x.id === a.id)) this.alarms = [a, ...this.alarms];
			return;
		}
		if (e.type === 'alarm_close') {
			this.alarms = this.alarms.filter((x) => x.id !== e.alarm.id);
			return;
		}
		if (e.type === 'alarm_promote') {
			const id = (e.alarm as { id: number }).id;
			this.alarms = this.alarms.map((x) =>
				x.id === id ? { ...x, severity: (e.alarm as { severity: 'warn' | 'critical' }).severity } : x
			);
			return;
		}
	}

	// Bursty WS streams can fire ~1 run/sec across 36 checks. We coalesce
	// many runs into one atomic rollup replacement on the next microtask so
	// Svelte's reactivity machinery runs once per burst, not per row.
	private pendingMerge?: Map<string, any>;
	private mergeScheduled = false;
	private pendingMetrics?: { check_id: string; metrics: Record<string, number> }[];
	private metricsScheduled = false;

	private flushMetrics() {
		this.metricsScheduled = false;
		const pending = this.pendingMetrics;
		this.pendingMetrics = undefined;
		if (!pending?.length) return;
		const next = { ...this.metrics };
		for (const { check_id, metrics } of pending) {
			for (const [m, v] of Object.entries(metrics)) {
				const k = `${check_id}|${m}`;
				const arr = (next[k] ?? []).slice();
				arr.push(v as number);
				if (arr.length > 30) arr.shift();
				next[k] = arr;
			}
		}
		this.metrics = next;
	}

	private mergeRun(run: {
		check_id: string;
		target: string;
		stage: string;
		status: string;
		finished_at: string;
		started_at: string;
		summary: string;
	}) {
		if (!this.rollup) return;
		// Dedupe on STATUS ONLY. The summary string from L1 product checks
		// includes time-varying age (`age=+3m45s` → `age=+3m48s`) so every
		// run reports a slightly different summary even though the status
		// is the same. If we deduped on summary too, ~30 events/min would
		// each force a full mergeRun rebuild for purely cosmetic age-string
		// drift. The 5s polling refresh picks up updated summaries on its
		// own cadence; for live updates we only care about status flips.
		const stageList = this.rollup.stages[run.stage];
		if (stageList) {
			const existing = stageList.find((r) => r.check_id === run.check_id);
			if (existing && existing.status === run.status) {
				return;
			}
		}
		if (!this.pendingMerge) this.pendingMerge = new Map();
		this.pendingMerge.set(run.check_id, {
			check_id: run.check_id,
			target: run.target,
			stage: run.stage,
			status: run.status as 'pass' | 'warn' | 'fail' | 'error' | 'skip',
			started_at: run.started_at,
			finished_at: run.finished_at,
			summary: run.summary
		});
		if (this.mergeScheduled) return;
		this.mergeScheduled = true;
		queueMicrotask(() => this.flushMerge());
	}

	private flushMerge() {
		this.mergeScheduled = false;
		const pending = this.pendingMerge;
		this.pendingMerge = undefined;
		if (!pending || !this.rollup) return;
		// Build a single new stages+counts snapshot atomically so the rollup
		// signal fires exactly once per microtask, regardless of how many
		// runs arrived this tick.
		const stages: typeof this.rollup.stages = {};
		for (const [k, v] of Object.entries(this.rollup.stages)) {
			stages[k] = v.slice();
		}
		for (const row of pending.values()) {
			const list = (stages[row.stage] ??= []);
			const i = list.findIndex((r) => r.check_id === row.check_id);
			if (i >= 0) list[i] = row;
			else list.push(row);
		}
		const counts: typeof this.rollup.counts = { ...this.rollup.counts };
		for (const [stage, list] of Object.entries(stages)) {
			const c = { pass: 0, warn: 0, fail: 0, error: 0, skip: 0, total: list.length };
			for (const r of list) (c as Record<string, number>)[r.status]++;
			counts[stage] = c as any;
		}
		this.rollup = { ...this.rollup, stages, counts };
	}

	private started = false;
	private intervalMs = 30000;
	private visibilityHandler?: () => void;

	start(intervalMs = 30000) {
		// idempotent — guards against Svelte HMR / repeated mounts during dev
		// from accumulating intervals + websockets until the tab freezes.
		if (this.started) return;
		this.started = true;
		this.intervalMs = intervalMs;
		this.refresh().then(() => this.refreshSparklines());
		if (diag.poll) this.startTimer();
		if (diag.ws) {
			this.ws = new SentinelWs();
			this.ws.subscribe((e) => this.handleWs(e));
			this.ws.connect();
		}

		// Pause polling when the tab is in the background; otherwise an
		// open-but-hidden Sentinel tab keeps hammering /api/status every 5s
		// and accumulating WS state updates until the page comes back and
		// has to apply hundreds of queued mutations at once.
		if (typeof document !== 'undefined') {
			this.visibilityHandler = () => {
				if (document.hidden) {
					this.stopTimer();
				} else {
					if (diag.poll) this.startTimer();
					this.refresh();
				}
			};
			document.addEventListener('visibilitychange', this.visibilityHandler);
		}
	}

	stop() {
		this.started = false;
		this.stopTimer();
		if (this.ws) {
			this.ws.stop();
			this.ws = undefined;
		}
		if (this.visibilityHandler && typeof document !== 'undefined') {
			document.removeEventListener('visibilitychange', this.visibilityHandler);
			this.visibilityHandler = undefined;
		}
	}

	private startTimer() {
		if (this.timer) return;
		this.timer = setInterval(() => this.refresh(), this.intervalMs);
	}
	private stopTimer() {
		if (this.timer) {
			clearInterval(this.timer);
			this.timer = undefined;
		}
	}
}

export const sentinel = new SentinelState();

// HMR cleanup. Without this, every save in dev rebuilds this module and
// instantiates a fresh SentinelState, but the previous instance's polling
// timer + WebSocket keep running because nothing tells them to stop. After
// a dozen edits the tab is running a dozen zombie pollers in parallel,
// each driving its own reactive updates — the freeze symptom.
if (import.meta.hot) {
	import.meta.hot.dispose(() => {
		try { sentinel.stop(); } catch { /* */ }
	});
}
