<script lang="ts">
	import { onMount } from 'svelte';

	// ---- shape mirrors backend/alarms/models.py:AlertsConfig ---------------
	interface Receiver {
		name: string;
		email: string[];
		webhook: string;
		console: boolean;
		template: string;
	}
	interface EscalationStep {
		delay: string;
		receivers: string[];
	}
	interface EscalationPolicy {
		name: string;
		steps: EscalationStep[];
	}
	interface Condition {
		time_of_day_in: string;
		time_of_day_not_in: string;
		timezone: string;
		weekday_only: boolean;
		duration_at_severity_min: string; // numeric in UI, blank = unset
		count_of_targets_failing: string;
		metric_above_name: string;
		metric_above_value: string;       // numeric in UI, blank = unset
	}
	interface Route {
		match: Record<string, string>;
		when: Condition;
		policy: string;
		severity_floor: string;       // '' = none
		repeat_interval: string;      // '' = none
		group_by: string[];
		showWhen: boolean;            // UI-only: condition section expanded?
		showAdvanced: boolean;        // UI-only: custom matchers expanded?
		testBusy: boolean;            // UI-only: test-send in flight
		testResult: { ok: boolean; results: any[]; step_receivers: string[] } | null;
	}

	const STAGES = ['L0', 'L1', 'L2', 'L3', 'L4-T1T2'] as const;
	const STATUSES = ['warn', 'fail', 'error'] as const;
	const SEVERITIES = ['warn', 'critical'] as const;
	const GROUP_BY_KEYS = ['stage', 'check_id', 'target', 'status_at_open'] as const;
	// Common alarm-dict keys the user already has dropdowns for; everything
	// else goes under "custom matchers".
	const COMMON_MATCH_KEYS = new Set(['stage', 'check_id', 'target', 'status_at_open']);

	function emptyCondition(): Condition {
		return {
			time_of_day_in: '', time_of_day_not_in: '', timezone: 'UTC',
			weekday_only: false, duration_at_severity_min: '',
			count_of_targets_failing: '', metric_above_name: '', metric_above_value: '',
		};
	}

	let receivers = $state<Receiver[]>([]);
	let policies = $state<EscalationPolicy[]>([]);
	let routes = $state<Route[]>([]);
	// Opaque round-trip — schema fields not edited here (smtp + silences are
	// managed by their own admin pages but live in the same config blob).
	let opaque = $state<Record<string, any>>({});

	let source = $state<'db' | 'yaml' | ''>('');
	let updatedAt = $state<string | null>(null);
	let updatedBy = $state<string | null>(null);
	let loading = $state(true);
	let saving = $state(false);
	let banner = $state<{ kind: 'ok' | 'err'; text: string } | null>(null);

	// pending email being typed (per-receiver index)
	let pendingEmail = $state<Record<number, string>>({});
	// pending custom matcher key/value being typed (per-route index)
	let pendingMatcherKey = $state<Record<number, string>>({});
	let pendingMatcherVal = $state<Record<number, string>>({});

	async function load() {
		loading = true;
		banner = null;
		try {
			const r = await fetch('/api/admin/alerts');
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			const j = await r.json();
			const v = j.value ?? {};
			receivers = (v.receivers ?? []).map((x: any): Receiver => ({
				name: x.name ?? '',
				email: Array.isArray(x.email) ? [...x.email] : [],
				webhook: x.webhook ?? '',
				console: !!x.console,
				template: x.template ?? 'default',
			}));
			policies = (v.escalation_policies ?? []).map((p: any): EscalationPolicy => ({
				name: p.name ?? '',
				steps: (p.steps ?? []).map((s: any) => ({
					delay: s.delay ?? '0m',
					receivers: Array.isArray(s.receivers) ? [...s.receivers] : [],
				})),
			}));
			routes = (v.routes ?? []).map((r: any): Route => {
				const w = r.when ?? null;
				const cond: Condition = emptyCondition();
				if (w) {
					cond.time_of_day_in = w.time_of_day_in ?? '';
					cond.time_of_day_not_in = w.time_of_day_not_in ?? '';
					cond.timezone = w.timezone ?? 'UTC';
					cond.weekday_only = !!w.weekday_only;
					cond.duration_at_severity_min = w.duration_at_severity_min != null
						? String(w.duration_at_severity_min) : '';
					cond.count_of_targets_failing = w.count_of_targets_failing ?? '';
					if (w.metric_above && typeof w.metric_above === 'object') {
						cond.metric_above_name = w.metric_above.metric ?? '';
						cond.metric_above_value = w.metric_above.value != null
							? String(w.metric_above.value) : '';
					}
				}
				const match = { ...(r.match ?? {}) } as Record<string, string>;
				const customMatchers = Object.keys(match).filter((k) => !COMMON_MATCH_KEYS.has(k));
				return {
					match,
					when: cond,
					policy: r.policy ?? '',
					severity_floor: r.severity_floor ?? '',
					repeat_interval: r.repeat_interval ?? '',
					group_by: Array.isArray(r.group_by) ? [...r.group_by] : [],
					showWhen: hasAnyCondition(cond),
					showAdvanced: customMatchers.length > 0,
					testBusy: false,
					testResult: null,
				};
			});
			// preserve any other top-level keys (smtp, silences)
			opaque = {};
			for (const k of Object.keys(v)) {
				if (!['receivers', 'escalation_policies', 'routes'].includes(k)) {
					opaque[k] = v[k];
				}
			}
			source = j.source;
			updatedAt = j.updated_at;
			updatedBy = j.updated_by;
		} catch (e) {
			banner = { kind: 'err', text: `Load failed: ${(e as Error).message}` };
		} finally {
			loading = false;
		}
	}

	function hasAnyCondition(c: Condition): boolean {
		return !!(c.time_of_day_in || c.time_of_day_not_in || c.weekday_only
			|| c.duration_at_severity_min || c.count_of_targets_failing
			|| c.metric_above_name || c.metric_above_value);
	}

	function buildConditionPayload(c: Condition): Record<string, any> | null {
		const payload: Record<string, any> = {};
		if (c.time_of_day_in) payload.time_of_day_in = c.time_of_day_in;
		if (c.time_of_day_not_in) payload.time_of_day_not_in = c.time_of_day_not_in;
		if (c.timezone && c.timezone !== 'UTC') payload.timezone = c.timezone;
		if (c.weekday_only) payload.weekday_only = true;
		if (c.duration_at_severity_min) {
			const n = Number(c.duration_at_severity_min);
			if (Number.isFinite(n)) payload.duration_at_severity_min = n;
		}
		if (c.count_of_targets_failing) payload.count_of_targets_failing = c.count_of_targets_failing;
		if (c.metric_above_name && c.metric_above_value) {
			const v = Number(c.metric_above_value);
			if (Number.isFinite(v)) {
				payload.metric_above = { metric: c.metric_above_name, value: v };
			}
		}
		return Object.keys(payload).length ? payload : null;
	}

	function buildConfig(): Record<string, any> {
		return {
			...opaque,
			receivers: receivers.map((r) => ({
				name: r.name,
				email: r.email.filter((e) => e.trim().length > 0),
				webhook: r.webhook.trim() || null,
				console: !!r.console,
				template: r.template || 'default',
			})),
			escalation_policies: policies.map((p) => ({
				name: p.name,
				steps: p.steps.map((s) => ({
					delay: s.delay || '0m',
					receivers: [...s.receivers],
				})),
			})),
			routes: routes.map((r) => {
				const out: Record<string, any> = {
					match: { ...r.match },
					policy: r.policy,
				};
				const w = buildConditionPayload(r.when);
				if (w) out.when = w;
				if (r.severity_floor) out.severity_floor = r.severity_floor;
				if (r.repeat_interval) out.repeat_interval = r.repeat_interval;
				if (r.group_by.length) out.group_by = [...r.group_by];
				return out;
			}),
		};
	}

	function validate(): string | null {
		// receiver names unique + non-empty
		const seenR = new Set<string>();
		for (const r of receivers) {
			if (!r.name.trim()) return 'A recipient is missing a name.';
			if (seenR.has(r.name)) return `Two recipients share the name "${r.name}".`;
			seenR.add(r.name);
			const hasChan = r.email.length > 0 || r.webhook.trim() || r.console;
			if (!hasChan) return `Recipient "${r.name}" has no channel — add an email, webhook, or check console.`;
		}
		// plan names unique + non-empty
		const seenP = new Set<string>();
		for (const p of policies) {
			if (!p.name.trim()) return 'An escalation plan is missing a name.';
			if (seenP.has(p.name)) return `Two plans share the name "${p.name}".`;
			seenP.add(p.name);
			if (p.steps.length === 0) return `Plan "${p.name}" has no steps.`;
			for (const [j, s] of p.steps.entries()) {
				if (!s.receivers.length) return `Plan "${p.name}" step ${j + 1} has no recipients selected.`;
				for (const rn of s.receivers) {
					if (!seenR.has(rn)) return `Plan "${p.name}" step ${j + 1} references unknown recipient "${rn}".`;
				}
			}
		}
		// routes
		for (const [i, r] of routes.entries()) {
			if (!r.policy) return `Routing rule ${i + 1} has no escalation plan selected.`;
			if (!seenP.has(r.policy)) return `Routing rule ${i + 1} references unknown plan "${r.policy}".`;
		}
		return null;
	}

	async function save() {
		const err = validate();
		if (err) {
			banner = { kind: 'err', text: err };
			return;
		}
		saving = true;
		banner = null;
		try {
			const r = await fetch('/api/admin/alerts', {
				method: 'PUT',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ value: buildConfig() }),
			});
			const j = await r.json();
			if (!r.ok) throw new Error(j?.detail ?? `HTTP ${r.status}`);
			banner = { kind: 'ok', text: 'Saved + reloaded the running alarm engine.' };
			await load();
		} catch (e) {
			banner = { kind: 'err', text: `Save failed: ${(e as Error).message}` };
		} finally {
			saving = false;
		}
	}

	// ---- recipients --------------------------------------------------------
	function addReceiver() {
		receivers.push({ name: '', email: [], webhook: '', console: false, template: 'default' });
	}
	function delReceiver(i: number) {
		const name = receivers[i].name;
		receivers.splice(i, 1);
		// strip from any plan step that referenced it
		for (const p of policies) {
			for (const s of p.steps) {
				s.receivers = s.receivers.filter((r) => r !== name);
			}
		}
	}
	function addEmail(i: number) {
		const e = (pendingEmail[i] ?? '').trim();
		if (!e) return;
		receivers[i].email.push(e);
		pendingEmail[i] = '';
	}
	function delEmail(i: number, j: number) {
		receivers[i].email.splice(j, 1);
	}

	// ---- plans -------------------------------------------------------------
	function addPolicy() {
		policies.push({ name: '', steps: [{ delay: '0m', receivers: [] }] });
	}
	function delPolicy(i: number) {
		const name = policies[i].name;
		policies.splice(i, 1);
		// orphan any route that referenced it (left to the user to fix; we just
		// surface the broken reference at save-validate time)
		for (const r of routes) {
			if (r.policy === name) r.policy = '';
		}
	}
	function addStep(i: number) {
		policies[i].steps.push({ delay: '5m', receivers: [] });
	}
	function delStep(i: number, j: number) {
		policies[i].steps.splice(j, 1);
	}
	function toggleStepReceiver(i: number, j: number, name: string) {
		const arr = policies[i].steps[j].receivers;
		const idx = arr.indexOf(name);
		if (idx >= 0) arr.splice(idx, 1);
		else arr.push(name);
	}

	// ---- routes ------------------------------------------------------------
	function addRoute() {
		routes.push({
			match: {}, when: emptyCondition(), policy: '',
			severity_floor: '', repeat_interval: '1h', group_by: [],
			showWhen: false, showAdvanced: false, testBusy: false, testResult: null,
		});
	}
	function delRoute(i: number) {
		routes.splice(i, 1);
	}
	function setMatch(i: number, key: string, val: string) {
		const m = { ...routes[i].match };
		if (!val) delete m[key];
		else m[key] = val;
		routes[i].match = m;
	}
	function addCustomMatcher(i: number) {
		const k = (pendingMatcherKey[i] ?? '').trim();
		const v = (pendingMatcherVal[i] ?? '').trim();
		if (!k || !v) return;
		setMatch(i, k, v);
		pendingMatcherKey[i] = '';
		pendingMatcherVal[i] = '';
	}
	function delCustomMatcher(i: number, key: string) {
		setMatch(i, key, '');
	}
	function customMatchers(r: Route): [string, string][] {
		return Object.entries(r.match).filter(([k]) => !COMMON_MATCH_KEYS.has(k));
	}
	function toggleGroupBy(i: number, key: string) {
		const arr = routes[i].group_by;
		const idx = arr.indexOf(key);
		if (idx >= 0) arr.splice(idx, 1);
		else arr.push(key);
	}

	async function sendTest(i: number) {
		const r = routes[i];
		r.testBusy = true;
		r.testResult = null;
		try {
			const resp = await fetch('/api/admin/alerts/test', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ route_index: i }),
			});
			const j = await resp.json();
			if (!resp.ok) throw new Error(j?.detail ?? `HTTP ${resp.status}`);
			r.testResult = j;
		} catch (e) {
			r.testResult = { ok: false, results: [{ receiver: '?', channel: null, ok: false, error: (e as Error).message }], step_receivers: [] };
		} finally {
			r.testBusy = false;
		}
	}

	onMount(load);
</script>

<div class="p-6 max-w-5xl">
	<div class="label tracking-[0.18em] text-[var(--color-bright)] mb-1">ALERT ROUTING</div>
	<div class="text-[11px] text-[var(--color-muted)] mb-4 leading-relaxed">
		Decide who gets notified when an alarm fires, with what urgency, and through which channel.
		Saved to DB; the running alarm engine hot-reloads on save (no restart needed).
	</div>

	{#if !loading}
		<div class="sticky top-0 z-10 -mx-6 px-6 py-2 mb-6 bg-[var(--color-surface)] border-b border-[var(--color-border)] flex items-center gap-3">
			<button
				class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-elevated)] disabled:opacity-50"
				onclick={save} disabled={saving}
			>{saving ? 'saving…' : 'save changes'}</button>
			<button
				class="text-[11px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]"
				onclick={load}
			>discard / reload</button>
			<span class="text-[10px] text-[var(--color-faint)] num ml-auto">
				source: <span class="text-[var(--color-bright)]">{source}</span>
				{#if updatedAt} · saved {updatedAt.slice(0, 19).replace('T', ' ')} by {updatedBy ?? '?'}{/if}
			</span>
		</div>
	{/if}

	{#if banner}
		<div
			class="mb-4 border px-3 py-2 text-[12px] {banner.kind === 'ok'
				? 'border-[var(--color-ok)]/40 bg-[var(--color-ok)]/10 text-[var(--color-ok)]'
				: 'border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 text-[var(--color-fail)]'}"
		>{banner.text}</div>
	{/if}

	{#if loading}
		<div class="text-[12px] text-[var(--color-muted)]">loading…</div>
	{:else}

	<!-- ============== RECIPIENTS ============== -->
	<section class="mb-10">
		<div class="label text-[var(--color-bright)] mb-1">RECIPIENTS</div>
		<div class="text-[11px] text-[var(--color-muted)] mb-3 leading-relaxed">
			Who gets notified. Each recipient is a name plus one or more delivery channels
			(email addresses, a webhook URL, or the backend console log).
		</div>

		{#if receivers.length === 0}
			<div class="text-[12px] text-[var(--color-faint)] italic mb-3">No recipients yet.</div>
		{/if}

		{#each receivers as r, i (i)}
			<div class="border border-[var(--color-border)] bg-[var(--color-canvas)] p-4 mb-3">
				<div class="grid grid-cols-[7rem_1fr] gap-x-4 gap-y-3 text-[12px] items-center">
					<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">name</label>
					<input bind:value={r.name} placeholder="oncall-primary"
						class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-72" />

					<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px] self-start pt-1">email</label>
					<div>
						{#if r.email.length}
							<div class="flex flex-wrap gap-1 mb-1">
								{#each r.email as e, j}
									<span class="flex items-center gap-1 border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)]">
										{e}
										<button onclick={() => delEmail(i, j)} class="text-[var(--color-muted)] hover:text-[var(--color-fail)] text-[12px] leading-none">×</button>
									</span>
								{/each}
							</div>
						{/if}
						<div class="flex items-center gap-2">
							<input
								bind:value={pendingEmail[i]}
								placeholder="you@example.com"
								type="email"
								onkeydown={(ev) => { if (ev.key === 'Enter') { ev.preventDefault(); addEmail(i); } }}
								class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-72"
							/>
							<button onclick={() => addEmail(i)} class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]">+ add</button>
						</div>
					</div>

					<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">webhook</label>
					<input bind:value={r.webhook} placeholder="https://discord.com/api/webhooks/…"
						class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-full" />

					<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">console log</label>
					<label class="flex items-center gap-2 text-[12px] text-[var(--color-default)]">
						<input type="checkbox" bind:checked={r.console} />
						Also write to backend log (useful for debugging)
					</label>

					<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">template</label>
					<input bind:value={r.template} placeholder="default"
						class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-72" />
				</div>

				<div class="mt-3 flex justify-end">
					<button onclick={() => delReceiver(i)} class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-fail)]">delete recipient</button>
				</div>
			</div>
		{/each}

		<button onclick={addReceiver} class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[11px] uppercase tracking-wider text-[var(--color-default)] hover:bg-[var(--color-elevated)] hover:text-[var(--color-bright)]">+ add recipient</button>
	</section>

	<!-- ============== ESCALATION PLANS ============== -->
	<section class="mb-10">
		<div class="label text-[var(--color-bright)] mb-1">ESCALATION PLANS</div>
		<div class="text-[11px] text-[var(--color-muted)] mb-3 leading-relaxed">
			A sequence of steps. Each step waits N (e.g. <span class="num">0m</span>, <span class="num">15m</span>, <span class="num">1h</span>),
			then notifies the selected recipients. Used by routing rules below.
		</div>

		{#if policies.length === 0}
			<div class="text-[12px] text-[var(--color-faint)] italic mb-3">No plans yet.</div>
		{/if}

		{#each policies as p, i (i)}
			<div class="border border-[var(--color-border)] bg-[var(--color-canvas)] p-4 mb-3">
				<div class="grid grid-cols-[7rem_1fr] gap-x-4 gap-y-3 text-[12px] items-center mb-3">
					<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">name</label>
					<input bind:value={p.name} placeholder="standard"
						class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-72" />
				</div>

				<div class="space-y-2">
					{#each p.steps as s, j (j)}
						<div class="border border-[var(--color-border)] bg-[var(--color-surface)] p-3 text-[12px]">
							<div class="flex items-center gap-3 flex-wrap">
								<span class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">step {j + 1}:</span>
								<span class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">wait</span>
								<input bind:value={s.delay} placeholder="0m"
									class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-20" />
								<span class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">then notify</span>
								{#if receivers.length === 0}
									<span class="text-[10px] italic text-[var(--color-faint)]">add a recipient above first</span>
								{:else}
									<div class="flex flex-wrap gap-1">
										{#each receivers as rec}
											<label class="flex items-center gap-1 text-[11px] text-[var(--color-default)] border border-[var(--color-border)] px-2 py-0.5 cursor-pointer hover:bg-[var(--color-elevated)]">
												<input type="checkbox" checked={s.receivers.includes(rec.name)} onchange={() => toggleStepReceiver(i, j, rec.name)} />
												<span class="num">{rec.name || '(unnamed)'}</span>
											</label>
										{/each}
									</div>
								{/if}
								<button onclick={() => delStep(i, j)} class="ml-auto text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-fail)]">delete step</button>
							</div>
						</div>
					{/each}
				</div>

				<div class="mt-3 flex items-center gap-3">
					<button onclick={() => addStep(i)} class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]">+ add step</button>
					<button onclick={() => delPolicy(i)} class="ml-auto text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-fail)]">delete plan</button>
				</div>
			</div>
		{/each}

		<button onclick={addPolicy} class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[11px] uppercase tracking-wider text-[var(--color-default)] hover:bg-[var(--color-elevated)] hover:text-[var(--color-bright)]">+ add plan</button>
	</section>

	<!-- ============== ROUTING RULES ============== -->
	<section class="mb-10">
		<div class="label text-[var(--color-bright)] mb-1">ROUTING RULES</div>
		<div class="text-[11px] text-[var(--color-muted)] mb-3 leading-relaxed">
			First match wins. Each rule maps alarms (filtered by stage / check / target / status,
			plus optional extra conditions) to an escalation plan. Leave a field blank to match
			anything.
		</div>

		{#if routes.length === 0}
			<div class="text-[12px] text-[var(--color-faint)] italic mb-3">No rules yet.</div>
		{/if}

		{#each routes as r, i (i)}
			<div class="border border-[var(--color-border)] bg-[var(--color-canvas)] p-4 mb-3">
				<!-- MATCH -->
				<div class="mb-3">
					<div class="text-[10px] uppercase tracking-wider text-[var(--color-faint)] mb-2">Match alarms where</div>
					<div class="grid grid-cols-2 gap-3 text-[12px]">
						<label class="flex items-center gap-2">
							<span class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] w-14">stage</span>
							<select value={r.match.stage ?? ''} onchange={(e) => setMatch(i, 'stage', (e.target as HTMLSelectElement).value)}
								class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num flex-1">
								<option value="">(any)</option>
								{#each STAGES as s}<option value={s}>{s}</option>{/each}
							</select>
						</label>
						<label class="flex items-center gap-2">
							<span class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] w-14">status</span>
							<select value={r.match.status_at_open ?? ''} onchange={(e) => setMatch(i, 'status_at_open', (e.target as HTMLSelectElement).value)}
								class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num flex-1">
								<option value="">(any)</option>
								{#each STATUSES as s}<option value={s}>{s}</option>{/each}
							</select>
						</label>
						<label class="flex items-center gap-2">
							<span class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] w-14">check</span>
							<input value={r.match.check_id ?? ''} oninput={(e) => setMatch(i, 'check_id', (e.target as HTMLInputElement).value)}
								placeholder="(any)" class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num flex-1" />
						</label>
						<label class="flex items-center gap-2">
							<span class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] w-14">target</span>
							<input value={r.match.target ?? ''} oninput={(e) => setMatch(i, 'target', (e.target as HTMLInputElement).value)}
								placeholder="(any)" class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num flex-1" />
						</label>
					</div>

					<button onclick={() => (r.showAdvanced = !r.showAdvanced)}
						class="mt-2 text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]">
						{r.showAdvanced ? '▾' : '▸'} custom matchers ({customMatchers(r).length})
					</button>
					{#if r.showAdvanced}
						<div class="mt-2 pl-4 border-l border-[var(--color-border)] space-y-1 text-[12px]">
							{#each customMatchers(r) as [k, v]}
								<div class="flex items-center gap-2 text-[11px] num text-[var(--color-default)]">
									<span class="text-[var(--color-muted)]">{k}</span>
									<span class="text-[var(--color-faint)]">=</span>
									<span class="text-[var(--color-bright)]">{v}</span>
									<button onclick={() => delCustomMatcher(i, k)} class="text-[var(--color-muted)] hover:text-[var(--color-fail)] text-[12px] leading-none">×</button>
								</div>
							{/each}
							<div class="flex items-center gap-2">
								<input bind:value={pendingMatcherKey[i]} placeholder="key"
									class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-32" />
								<span class="text-[var(--color-faint)]">=</span>
								<input bind:value={pendingMatcherVal[i]} placeholder="value"
									onkeydown={(ev) => { if (ev.key === 'Enter') { ev.preventDefault(); addCustomMatcher(i); } }}
									class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-40" />
								<button onclick={() => addCustomMatcher(i)} class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]">+ add matcher</button>
							</div>
						</div>
					{/if}
				</div>

				<!-- WHEN (extra conditions) -->
				<div class="mb-3">
					<button onclick={() => (r.showWhen = !r.showWhen)}
						class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]">
						{r.showWhen ? '▾' : '▸'} extra conditions {hasAnyCondition(r.when) ? '(set)' : '(optional)'}
					</button>
					{#if r.showWhen}
						<div class="mt-2 pl-4 border-l border-[var(--color-border)] grid grid-cols-[10rem_1fr] gap-x-4 gap-y-2 text-[12px] items-center">
							<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">time of day in</label>
							<input bind:value={r.when.time_of_day_in} placeholder="09:00-17:00"
								class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-40" />

							<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">time of day not in</label>
							<input bind:value={r.when.time_of_day_not_in} placeholder="22:00-06:00"
								class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-40" />

							<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">timezone</label>
							<input bind:value={r.when.timezone} placeholder="UTC"
								class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-40" />

							<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">weekdays only</label>
							<label class="flex items-center gap-2 text-[12px] text-[var(--color-default)]">
								<input type="checkbox" bind:checked={r.when.weekday_only} />
								Mon–Fri only (skip weekends)
							</label>

							<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">min duration (min)</label>
							<input bind:value={r.when.duration_at_severity_min} placeholder="(none)" type="number" min="0"
								class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-24" />

							<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">peers also failing</label>
							<input bind:value={r.when.count_of_targets_failing} placeholder=">=2"
								class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-24" />

							<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">metric above</label>
							<div class="flex items-center gap-2">
								<input bind:value={r.when.metric_above_name} placeholder="metric name"
									class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-40" />
								<span class="text-[var(--color-faint)]">&gt;</span>
								<input bind:value={r.when.metric_above_value} placeholder="value" type="number" step="any"
									class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-24" />
							</div>
						</div>
					{/if}
				</div>

				<!-- THEN -->
				<div class="border-t border-[var(--color-border)] pt-3">
					<div class="text-[10px] uppercase tracking-wider text-[var(--color-faint)] mb-2">Then</div>
					<div class="grid grid-cols-[10rem_1fr] gap-x-4 gap-y-3 text-[12px] items-center">
						<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">send via plan</label>
						<select bind:value={r.policy}
							class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-72">
							<option value="">— pick a plan —</option>
							{#each policies as p}<option value={p.name}>{p.name || '(unnamed)'}</option>{/each}
						</select>

						<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">severity floor</label>
						<select bind:value={r.severity_floor}
							class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-40">
							<option value="">(none)</option>
							{#each SEVERITIES as s}<option value={s}>{s}</option>{/each}
						</select>

						<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">repeat every</label>
						<input bind:value={r.repeat_interval} placeholder="1h (blank = no repeat)"
							class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] num w-40" />

						<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px] self-start pt-1">group by</label>
						<div class="flex flex-wrap gap-2">
							{#each GROUP_BY_KEYS as k}
								<label class="flex items-center gap-1 text-[11px] text-[var(--color-default)] border border-[var(--color-border)] px-2 py-0.5 cursor-pointer hover:bg-[var(--color-elevated)]">
									<input type="checkbox" checked={r.group_by.includes(k)} onchange={() => toggleGroupBy(i, k)} />
									<span class="num">{k}</span>
								</label>
							{/each}
						</div>
					</div>
				</div>

				<!-- ACTIONS -->
				<div class="mt-4 flex items-center gap-3">
					<button onclick={() => sendTest(i)} disabled={r.testBusy || !r.policy}
						class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[11px] uppercase tracking-wider text-[var(--color-default)] hover:bg-[var(--color-elevated)] hover:text-[var(--color-bright)] disabled:opacity-50"
					>{r.testBusy ? 'sending…' : 'send test alert'}</button>
					<span class="text-[10px] text-[var(--color-faint)]">(uses currently-saved config)</span>
					<button onclick={() => delRoute(i)} class="ml-auto text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-fail)]">delete rule</button>
				</div>

				{#if r.testResult}
					<div class="mt-3 border px-3 py-2 text-[11px] num {r.testResult.ok
						? 'border-[var(--color-ok)]/40 bg-[var(--color-ok)]/10 text-[var(--color-ok)]'
						: 'border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 text-[var(--color-fail)]'}">
						{#if !r.testResult.results.length}
							No recipients in step 1 of this rule's plan.
						{:else}
							{#each r.testResult.results as res}
								<div>
									<span class="text-[var(--color-bright)]">{res.receiver}</span>
									{#if res.channel}<span class="text-[var(--color-muted)]"> · {res.channel}</span>{/if}
									{#if res.ok}<span class="text-[var(--color-ok)]"> · sent</span>
									{:else}<span class="text-[var(--color-fail)]"> · {res.error ?? 'failed'}</span>{/if}
								</div>
							{/each}
						{/if}
					</div>
				{/if}
			</div>
		{/each}

		<button onclick={addRoute} class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[11px] uppercase tracking-wider text-[var(--color-default)] hover:bg-[var(--color-elevated)] hover:text-[var(--color-bright)]">+ add rule</button>
	</section>

	{/if}
</div>
