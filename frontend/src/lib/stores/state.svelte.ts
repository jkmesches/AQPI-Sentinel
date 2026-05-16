// Reactive store. Initial snapshot from REST; live updates over WebSocket.

import { api, type StatusRollup, type Alarm, type CheckMeta } from '$lib/api';
import { SentinelWs, type WsEvent } from '$lib/ws';

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
				(r) => { this.detectTransitions(r); this.rollup = r; anyOk = true; dropLoading(); },
				(e) => { this.error = (e as Error).message; dropLoading(); }
			),
			api.alarms('open').then(
				(a) => { this.alarms = a; anyOk = true; dropLoading(); },
				(e) => { this.error = (e as Error).message; dropLoading(); }
			),
			api.checks().then(
				(c) => { this.checks = c; anyOk = true; dropLoading(); },
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
		this.lastUpdate = new Date();
		if (e.type === 'hello') {
			this.liveSince = new Date();
			return;
		}
		if (e.type === 'run') {
			this.mergeRun(e.run);
			// append metric values incrementally so sparklines move live
			if (e.run.metrics) {
				const upd: Record<string, number[]> = { ...this.metrics };
				for (const [m, v] of Object.entries(e.run.metrics)) {
					const k = `${e.run.check_id}|${m}`;
					const arr = (upd[k] ?? []).slice();
					arr.push(v as number);
					if (arr.length > 30) arr.shift();
					upd[k] = arr;
				}
				this.metrics = upd;
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
		const stages = { ...this.rollup.stages };
		const list = (stages[run.stage] ?? []).slice();
		const i = list.findIndex((r) => r.check_id === run.check_id);
		const row = {
			check_id: run.check_id,
			target: run.target,
			stage: run.stage,
			status: run.status as 'pass' | 'warn' | 'fail' | 'error' | 'skip',
			started_at: run.started_at,
			finished_at: run.finished_at,
			summary: run.summary
		};
		if (i >= 0) list[i] = row;
		else list.push(row);
		stages[run.stage] = list;
		// recompute counts for this stage
		const c = list.reduce(
			(acc, r) => {
				acc.total++;
				(acc as Record<string, number>)[r.status]++;
				return acc;
			},
			{ pass: 0, warn: 0, fail: 0, error: 0, skip: 0, total: 0 } as Record<string, number>
		);
		this.rollup = { ...this.rollup, stages, counts: { ...this.rollup.counts, [run.stage]: c as any } };
	}

	private started = false;

	start(intervalMs = 30000) {
		// idempotent — guards against Svelte HMR / repeated mounts during dev
		// from accumulating intervals + websockets until the tab freezes.
		if (this.started) return;
		this.started = true;
		this.refresh().then(() => this.refreshSparklines());
		this.timer = setInterval(() => this.refresh(), intervalMs);
		this.ws = new SentinelWs();
		this.ws.subscribe((e) => this.handleWs(e));
		this.ws.connect();
	}

	stop() {
		this.started = false;
		if (this.timer) {
			clearInterval(this.timer);
			this.timer = undefined;
		}
		if (this.ws) {
			this.ws.stop();
			this.ws = undefined;
		}
	}
}

export const sentinel = new SentinelState();
