<script lang="ts">
	/** Per-device push routing editor.
	 *
	 *  Renders inside both /m/push-settings (mobile-native styling) and
	 *  /settings/devices (desktop). Mobile-friendly: large tap targets,
	 *  16px+ inputs so iOS Safari doesn't auto-zoom.
	 *
	 *  Routing fields:
	 *    label             — friendly device name shown in the device list
	 *    severity_floor    — info | warn | critical
	 *    product_patterns  — list of substring matchers against the
	 *                        notification's tag/title/body
	 *    delay_s           — defer this device's notifications by N seconds
	 *    schedule          — group-schedule shape (weekly/biweekly/downtime)
	 *                        evaluated by backend/groups.py at dispatch.
	 */
	import { stageLabel } from '$lib/format';

	let {
		subscription = $bindable<any>(null),
		onSaved
	}: {
		subscription: any;
		onSaved?: () => void;
	} = $props();

	let label = $state(subscription?.label ?? '');
	let severityFloor = $state<'' | 'info' | 'warn' | 'critical'>(
		(subscription?.routing_config?.severity_floor ?? '') as any
	);
	let patterns = $state<string[]>([
		...(subscription?.routing_config?.product_patterns ?? [])
	]);
	let pendingPattern = $state('');
	let delayMin = $state<number>(
		Math.round((subscription?.routing_config?.delay_s ?? 0) / 60)
	);

	// Schedule: support the same kinds groups.py understands but with a
	// simplified UI — most users only need "always" or "weekly". Biweekly
	// + downtime add tap-cost; surface them as a "Show advanced" toggle
	// later if the user actually asks.
	type SchedKind = 'always' | 'weekly' | 'biweekly';
	let kind = $state<SchedKind>((subscription?.routing_config?.schedule?.kind ?? 'always') as SchedKind);
	let weekdays = $state<number[]>(
		subscription?.routing_config?.schedule?.weekdays ?? [0, 1, 2, 3, 4]
	);
	let twStart = $state<string>(
		subscription?.routing_config?.schedule?.time_windows?.[0]?.[0] ?? ''
	);
	let twEnd = $state<string>(
		subscription?.routing_config?.schedule?.time_windows?.[0]?.[1] ?? ''
	);
	let anchorDate = $state<string>(
		subscription?.routing_config?.schedule?.anchor_date ?? new Date().toISOString().slice(0, 10)
	);

	let saving = $state(false);
	let error = $state<string | null>(null);
	let info = $state<string | null>(null);

	const WEEKDAYS = [
		{ idx: 0, label: 'Mon' },{ idx: 1, label: 'Tue' },{ idx: 2, label: 'Wed' },
		{ idx: 3, label: 'Thu' },{ idx: 4, label: 'Fri' },{ idx: 5, label: 'Sat' },
		{ idx: 6, label: 'Sun' }
	];

	function toggleWeekday(idx: number) {
		weekdays = weekdays.includes(idx)
			? weekdays.filter((d) => d !== idx)
			: [...weekdays, idx].sort();
	}
	function addPattern() {
		const p = pendingPattern.trim();
		if (!p) return;
		patterns = [...patterns, p];
		pendingPattern = '';
	}
	function removePattern(i: number) {
		patterns = patterns.filter((_, j) => j !== i);
	}

	function buildSchedule() {
		if (kind === 'always') return {};
		const out: any = { kind, weekdays: [...weekdays].sort() };
		if (twStart && twEnd) out.time_windows = [[twStart, twEnd]];
		if (kind === 'biweekly') out.anchor_date = anchorDate;
		return out;
	}

	async function save() {
		error = null;
		info = null;
		saving = true;
		try {
			const routing: any = {};
			if (severityFloor) routing.severity_floor = severityFloor;
			const trimmed = patterns.filter((p) => p.trim());
			if (trimmed.length) routing.product_patterns = trimmed;
			if (delayMin > 0) routing.delay_s = Math.min(3600, Math.round(delayMin * 60));
			const sched = buildSchedule();
			if (Object.keys(sched).length) routing.schedule = sched;

			const r = await fetch(`/api/push/subscriptions/${subscription.id}/routing`, {
				method: 'PUT',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({
					label: label.trim() || null,
					routing_config: routing
				})
			});
			if (!r.ok) {
				const j = await r.json().catch(() => ({}));
				throw new Error(j?.detail ?? `HTTP ${r.status}`);
			}
			info = 'Saved.';
			onSaved?.();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			saving = false;
		}
	}
</script>

<div class="space-y-4 text-[13px]">
	<!-- Label -->
	<section>
		<div class="mb-1 text-[11px] uppercase tracking-wider text-[var(--color-muted)]">Device name</div>
		<input
			type="text"
			bind:value={label}
			placeholder={subscription?.user_agent ? subscription.user_agent.slice(0, 40) : 'iPhone, Office laptop, …'}
			class="w-full rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-3 py-2 text-[16px] num text-[var(--color-bright)]"
		/>
	</section>

	<!-- Severity floor -->
	<section>
		<div class="mb-1 text-[11px] uppercase tracking-wider text-[var(--color-muted)]">Minimum severity</div>
		<div class="flex gap-1">
			{#each [['','any'],['info','info+'],['warn','warn+'],['critical','critical only']] as [v, lbl]}
				<button
					type="button"
					onclick={() => (severityFloor = v as any)}
					class="flex-1 rounded-md border px-2 py-2 text-[12px] num uppercase tracking-wider {severityFloor === v
						? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
						: 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
					style="-webkit-tap-highlight-color: transparent;"
				>
					{lbl}
				</button>
			{/each}
		</div>
		<div class="mt-1 text-[10.5px] text-[var(--color-faint)] leading-relaxed">
			Notifications below this level are dropped for this device.
			Alarms open at <span class="text-[var(--color-bright)]">warn</span>; they auto-promote to <span class="text-[var(--color-bright)]">critical</span> after 30 min if status is <span class="num">fail</span>.
		</div>
	</section>

	<!-- Product / target patterns -->
	<section>
		<div class="mb-1 text-[11px] uppercase tracking-wider text-[var(--color-muted)]">Match patterns</div>
		{#if patterns.length}
			<div class="flex flex-wrap gap-1 mb-2">
				{#each patterns as p, i}
					<span class="inline-flex items-center gap-1 rounded-sm border border-[var(--color-border-strong)] bg-[var(--color-elevated)]/40 px-2 py-1 text-[11.5px] num text-[var(--color-bright)]">
						{p}
						<button class="text-[var(--color-muted)] active:text-[var(--color-fail)]" onclick={() => removePattern(i)} aria-label="remove">×</button>
					</span>
				{/each}
			</div>
		{/if}
		<div class="flex gap-2">
			<input
				type="text"
				bind:value={pendingPattern}
				onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addPattern(); } }}
				placeholder="XSCV, CBAND, qpe_15min, …"
				class="flex-1 rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-3 py-2 text-[16px] num text-[var(--color-bright)]"
			/>
			<button onclick={addPattern} type="button" class="rounded-md border border-[var(--color-border-strong)] px-3 text-[12px] uppercase tracking-wider text-[var(--color-default)] active:bg-[var(--color-elevated)]">add</button>
		</div>
		<div class="mt-1 text-[10.5px] text-[var(--color-faint)] leading-relaxed">
			Notifications whose check ID, target, or title contains at least one pattern are sent. Empty = match everything.<br />
			<span class="text-[var(--color-bright)]">Always included regardless of patterns:</span> L0 connectivity + canary alarms (origin / website / TLS / stream canary). Patterns only filter product-specific alarms.
		</div>
	</section>

	<!-- Delay -->
	<section>
		<div class="mb-1 text-[11px] uppercase tracking-wider text-[var(--color-muted)]">Delay before notify</div>
		<div class="flex items-center gap-3">
			<input type="number" min="0" max="60" step="1" bind:value={delayMin}
				class="w-20 rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-2 text-[16px] num text-[var(--color-bright)] text-right" />
			<span class="text-[12px] text-[var(--color-default)]">minutes</span>
		</div>
		<div class="mt-1 text-[10.5px] text-[var(--color-faint)] leading-relaxed">
			Notifications wait this long before delivering. If the alarm
			<span class="text-[var(--color-bright)]">self-resolves</span>
			or is <span class="text-[var(--color-bright)]">acknowledged</span>
			during the wait, the notification is dropped. Useful for
			"page me only if it hasn't fixed itself in N minutes."
		</div>
	</section>

	<!-- On-duty schedule -->
	<section>
		<div class="mb-1 text-[11px] uppercase tracking-wider text-[var(--color-muted)]">On-duty schedule (UTC)</div>
		<div class="flex gap-1">
			{#each ['always','weekly','biweekly'] as k}
				<button type="button" onclick={() => (kind = k as SchedKind)}
					class="flex-1 rounded-md border px-2 py-2 text-[12px] num uppercase tracking-wider {kind === k
						? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
						: 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
					style="-webkit-tap-highlight-color: transparent;"
				>
					{k}
				</button>
			{/each}
		</div>
		{#if kind !== 'always'}
			<div class="mt-2 flex flex-wrap gap-1">
				{#each WEEKDAYS as w}
					<button type="button" onclick={() => toggleWeekday(w.idx)}
						class="w-12 rounded-md border py-1.5 text-[11.5px] num {weekdays.includes(w.idx)
							? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
							: 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
					>
						{w.label}
					</button>
				{/each}
			</div>
			<div class="mt-2 flex items-center gap-2">
				<input type="time" bind:value={twStart} class="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1.5 text-[16px] num text-[var(--color-bright)]" />
				<span class="text-[var(--color-muted)]">→</span>
				<input type="time" bind:value={twEnd}   class="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1.5 text-[16px] num text-[var(--color-bright)]" />
				<button type="button" class="text-[10.5px] uppercase tracking-wider text-[var(--color-faint)] active:text-[var(--color-bright)]" onclick={() => { twStart = ''; twEnd = ''; }}>clear</button>
			</div>
			{#if kind === 'biweekly'}
				<div class="mt-2 flex items-center gap-2">
					<span class="text-[11px] uppercase tracking-wider text-[var(--color-muted)]">Anchor</span>
					<input type="date" bind:value={anchorDate} class="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1.5 text-[16px] num text-[var(--color-bright)]" />
				</div>
			{/if}
		{:else}
			<div class="mt-1 text-[10.5px] text-[var(--color-faint)]">Always on duty. Notifications are not gated by time-of-day.</div>
		{/if}
	</section>

	<!-- Status + save -->
	<section class="border-t border-[var(--color-border)] pt-3">
		<div class="mb-2 text-[11px]">
			{#if error}<span class="text-[var(--color-fail)]">{error}</span>
			{:else if info}<span class="text-[var(--color-ok)]">{info}</span>{/if}
		</div>
		<button
			type="button"
			onclick={save}
			disabled={saving}
			class="w-full rounded-md border border-[var(--color-ok)] bg-[var(--color-ok)]/15 px-4 py-3 text-[14px] uppercase tracking-wider text-[var(--color-bright)] active:bg-[var(--color-ok)]/25 disabled:opacity-50"
			style="-webkit-tap-highlight-color: transparent;"
		>
			{saving ? 'saving…' : 'save routing'}
		</button>
	</section>
</div>
