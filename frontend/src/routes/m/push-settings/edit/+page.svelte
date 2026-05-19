<script lang="ts">
	/** /m/push-settings/edit?id=… — mobile-native per-device routing editor.
	 *
	 *  Standalone page (not a drilldown sheet) so the mob-layout's sticky
	 *  header + scrollable main handle the layout correctly. Pattern input
	 *  is now chip-based: the user picks radars / product categories from
	 *  predefined tiles, with "Other…" as the escape hatch. Schedule and
	 *  severity controls match the existing PushRoutingEditor.
	 */
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { productLabel, PRODUCT_CATEGORY_LABEL } from '$lib/format';

	let subscription = $state<any>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let info = $state<string | null>(null);
	let saving = $state(false);

	const id = $derived(Number(page.url.searchParams.get('id') ?? '0'));

	// Quick-pick pattern catalog — every radar + every common product
	// family + the four product categories. Tapping a chip toggles the
	// pattern into the routing config so the user never has to type a
	// match string. "Other…" expands a free-text input for edge cases.
	const RADAR_IDS = ['XSCW', 'XSCV', 'XSCR', 'XSWR', 'XEBY', 'CBAND'];
	const PRODUCT_IDS = [
		'qpe_15min', 'qpe_1hr', 'precip_rate_radar',
		'comp_ref', 'comp_now',
		'fcst_total_precip', 'fcst_total_precip_cum',
		'fcst_precip_rate', 'fcst_temp',
		'water_level', 'water_depth'
	];

	// Local form state — initialized from the loaded subscription.
	let label = $state('');
	let severityFloor = $state<'' | 'info' | 'warn' | 'critical'>('');
	let patterns = $state<string[]>([]);
	let customPattern = $state('');
	let delayMin = $state(0);
	let kind = $state<'always' | 'weekly' | 'biweekly'>('always');
	let weekdays = $state<number[]>([0, 1, 2, 3, 4]);
	let twStart = $state('');
	let twEnd = $state('');
	let anchorDate = $state(new Date().toISOString().slice(0, 10));
	// Recurring downtime (quiet hours) — same shape as groups schedule.
	let rdtEnabled = $state(false);
	let rdtStart = $state('22:00');
	let rdtEnd = $state('06:00');
	let rdtWeekdays = $state<number[]>([0, 1, 2, 3, 4, 5, 6]);

	const WEEKDAYS = [
		{ idx: 0, label: 'Mon' },{ idx: 1, label: 'Tue' },{ idx: 2, label: 'Wed' },
		{ idx: 3, label: 'Thu' },{ idx: 4, label: 'Fri' },{ idx: 5, label: 'Sat' },{ idx: 6, label: 'Sun' }
	];

	async function load() {
		if (!id) { error = 'missing device id'; loading = false; return; }
		try {
			const r = await fetch('/api/push/subscriptions');
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			const list = await r.json();
			subscription = list.find((s: any) => s.id === id);
			if (!subscription) throw new Error('device not found');
			const rc = subscription.routing_config ?? {};
			label = subscription.label ?? '';
			severityFloor = (rc.severity_floor ?? '') as any;
			patterns = [...(rc.product_patterns ?? [])];
			delayMin = Math.round((rc.delay_s ?? 0) / 60);
			kind = (rc.schedule?.kind ?? 'always') as any;
			weekdays = rc.schedule?.weekdays ?? [0, 1, 2, 3, 4];
			twStart = rc.schedule?.time_windows?.[0]?.[0] ?? '';
			twEnd = rc.schedule?.time_windows?.[0]?.[1] ?? '';
			anchorDate = rc.schedule?.anchor_date ?? new Date().toISOString().slice(0, 10);
			const rdt = rc.schedule?.recurring_downtime?.[0];
			if (rdt) {
				rdtEnabled = true;
				rdtStart = rdt.time_window?.[0] ?? '22:00';
				rdtEnd = rdt.time_window?.[1] ?? '06:00';
				rdtWeekdays = rdt.weekdays ?? [0,1,2,3,4,5,6];
			}
		} catch (e) {
			error = (e as Error).message;
		} finally {
			loading = false;
		}
	}
	onMount(load);

	function deviceLabel(s: any): string {
		if (s?.label) return s.label;
		const ua = s?.user_agent ?? '';
		if (/iPhone/i.test(ua)) return 'iPhone';
		if (/iPad/i.test(ua)) return 'iPad';
		if (/Android/i.test(ua)) return 'Android';
		if (/Macintosh/i.test(ua)) return 'Mac';
		if (/Windows/i.test(ua)) return 'Windows';
		return 'Device';
	}

	function togglePattern(p: string) {
		patterns = patterns.includes(p) ? patterns.filter((x) => x !== p) : [...patterns, p];
	}
	function addCustom() {
		const p = customPattern.trim();
		if (!p) return;
		if (!patterns.includes(p)) patterns = [...patterns, p];
		customPattern = '';
	}
	function removePattern(i: number) {
		patterns = patterns.filter((_, j) => j !== i);
	}
	function toggleWeekday(idx: number) {
		weekdays = weekdays.includes(idx) ? weekdays.filter((d) => d !== idx) : [...weekdays, idx].sort();
	}
	function toggleRdtWeekday(idx: number) {
		rdtWeekdays = rdtWeekdays.includes(idx) ? rdtWeekdays.filter((d) => d !== idx) : [...rdtWeekdays, idx].sort();
	}

	function buildRouting() {
		const routing: any = {};
		if (severityFloor) routing.severity_floor = severityFloor;
		const trimmed = patterns.filter((p) => p.trim());
		if (trimmed.length) routing.product_patterns = trimmed;
		if (delayMin > 0) routing.delay_s = Math.min(3600, Math.round(delayMin * 60));
		const sched: any = {};
		if (kind !== 'always') {
			sched.kind = kind;
			sched.weekdays = [...weekdays].sort();
			if (twStart && twEnd) sched.time_windows = [[twStart, twEnd]];
			if (kind === 'biweekly') sched.anchor_date = anchorDate;
		}
		if (rdtEnabled && rdtStart && rdtEnd && rdtWeekdays.length) {
			if (!sched.kind) sched.kind = 'always';
			sched.recurring_downtime = [{
				weekdays: [...rdtWeekdays].sort(),
				time_window: [rdtStart, rdtEnd]
			}];
		}
		if (Object.keys(sched).length) routing.schedule = sched;
		return routing;
	}

	async function save() {
		saving = true; error = null; info = null;
		try {
			const r = await fetch(`/api/push/subscriptions/${id}/routing`, {
				method: 'PUT',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ label: label.trim() || null, routing_config: buildRouting() })
			});
			if (!r.ok) {
				const j = await r.json().catch(() => ({}));
				throw new Error(j?.detail ?? `HTTP ${r.status}`);
			}
			info = 'Saved.';
		} catch (e) {
			error = (e as Error).message;
		} finally {
			saving = false;
		}
	}
</script>

{#if loading}
	<div class="text-[12px] text-[var(--color-muted)]">loading…</div>
{:else if error && !subscription}
	<div class="rounded-md border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-3 py-2 text-[12px] text-[var(--color-fail)]">
		{error}
		<div class="mt-2"><a href="/m/push-settings" class="underline">Back to devices</a></div>
	</div>
{:else}
	<!-- Back affordance -->
	<a href="/m/push-settings" class="-ml-2 mb-3 inline-flex items-center gap-1 px-2 py-1 text-[12px] text-[var(--color-muted)] active:text-[var(--color-bright)]" style="-webkit-tap-highlight-color: transparent;">
		<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 6 9 12 15 18" /></svg>
		<span>All devices</span>
	</a>

	<header class="mb-4">
		<div class="text-[11px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Routing</div>
		<h1 class="text-[18px] font-semibold tracking-wide text-[var(--color-bright)]">{deviceLabel(subscription)}</h1>
		{#if subscription?.user_agent}
			<div class="mt-0.5 text-[10.5px] text-[var(--color-faint)] num truncate">{subscription.user_agent}</div>
		{/if}
	</header>

	<div class="space-y-5 text-[13px]">
		<!-- Device name -->
		<section>
			<label for="device-name" class="mb-1 block text-[11px] uppercase tracking-wider text-[var(--color-muted)]">Device name</label>
			<input id="device-name"
				type="text"
				bind:value={label}
				placeholder="iPhone, Office laptop, …"
				class="w-full rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-3 py-2.5 text-[16px] num text-[var(--color-bright)]"
			/>
		</section>

		<!-- Severity floor -->
		<section>
			<div class="mb-1 text-[11px] uppercase tracking-wider text-[var(--color-muted)]">Minimum severity</div>
			<div class="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
				{#each [['','any severity'],['info','info+'],['warn','warn+'],['critical','critical only']] as [v, lbl]}
					<button
						type="button"
						onclick={() => (severityFloor = v as any)}
						class="rounded-md border px-2 py-2.5 text-[12.5px] num uppercase tracking-wider {severityFloor === v
							? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
							: 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
						style="-webkit-tap-highlight-color: transparent; min-height: 44px;"
					>
						{lbl}
					</button>
				{/each}
			</div>
		</section>

		<!-- Quick-pick patterns -->
		<section>
			<div class="mb-1 text-[11px] uppercase tracking-wider text-[var(--color-muted)]">Notify me about</div>
			<div class="text-[10.5px] text-[var(--color-faint)] mb-2 leading-snug">
				Pick any combination. Empty = receive every notification. Patterns match the alarm's check ID, target, or title.
			</div>

			<div class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] mb-1">Radars</div>
			<div class="flex flex-wrap gap-1.5 mb-3">
				{#each RADAR_IDS as r}
					{@const sel = patterns.includes(r)}
					<button type="button" onclick={() => togglePattern(r)}
						class="rounded-md border px-3 py-1.5 text-[12.5px] num {sel ? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]' : 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
						style="-webkit-tap-highlight-color: transparent; min-height: 36px;">
						{r}
					</button>
				{/each}
			</div>

			<div class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] mb-1">Products</div>
			<div class="flex flex-wrap gap-1.5 mb-3">
				{#each PRODUCT_IDS as p}
					{@const sel = patterns.includes(p)}
					<button type="button" onclick={() => togglePattern(p)}
						class="rounded-md border px-2.5 py-1.5 text-[11.5px] num {sel ? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]' : 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
						style="-webkit-tap-highlight-color: transparent; min-height: 36px;">
						{productLabel(p)}
					</button>
				{/each}
			</div>

			{#if patterns.some((p) => !RADAR_IDS.includes(p) && !PRODUCT_IDS.includes(p))}
				<div class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] mb-1">Custom patterns</div>
				<div class="flex flex-wrap gap-1.5 mb-2">
					{#each patterns as p, i}
						{#if !RADAR_IDS.includes(p) && !PRODUCT_IDS.includes(p)}
							<span class="inline-flex items-center gap-1.5 rounded-md border border-[var(--color-border-strong)] bg-[var(--color-elevated)]/40 px-2 py-1.5 text-[11.5px] num text-[var(--color-bright)]">
								{p}
								<button type="button" class="text-[var(--color-muted)] active:text-[var(--color-fail)] text-[14px] leading-none" onclick={() => removePattern(i)} aria-label="remove">×</button>
							</span>
						{/if}
					{/each}
				</div>
			{/if}

			<details class="text-[12px]">
				<summary class="cursor-pointer text-[11px] uppercase tracking-wider text-[var(--color-muted)] active:text-[var(--color-bright)]">+ add custom pattern</summary>
				<div class="mt-2 flex items-center gap-2">
					<input
						type="text"
						bind:value={customPattern}
						placeholder="e.g. layer2.radar"
						onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addCustom(); } }}
						class="flex-1 rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-3 py-2 text-[16px] num text-[var(--color-bright)]"
					/>
					<button type="button" onclick={addCustom} class="rounded-md border border-[var(--color-border-strong)] px-3 text-[12px] uppercase tracking-wider text-[var(--color-default)] active:bg-[var(--color-elevated)]" style="min-height: 44px;">add</button>
				</div>
			</details>
		</section>

		<!-- Delay -->
		<section>
			<label for="delay-min" class="mb-1 block text-[11px] uppercase tracking-wider text-[var(--color-muted)]">Delay before notifying</label>
			<div class="flex items-center gap-3">
				<input id="delay-min" type="number" min="0" max="60" step="1" bind:value={delayMin}
					class="w-24 rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-3 py-2.5 text-[16px] num text-[var(--color-bright)] text-right" />
				<span class="text-[13px] text-[var(--color-default)]">minutes</span>
			</div>
			<div class="mt-1 text-[10.5px] text-[var(--color-faint)] leading-relaxed">
				Notifications wait this long before delivering. If the
				alarm self-resolves or is acknowledged during the wait,
				the notification is dropped.
			</div>
		</section>

		<!-- On-duty schedule -->
		<section>
			<div class="mb-1 text-[11px] uppercase tracking-wider text-[var(--color-muted)]">On-duty schedule (UTC)</div>
			<div class="grid grid-cols-3 gap-1.5">
				{#each ['always','weekly','biweekly'] as k}
					<button type="button" onclick={() => (kind = k as any)}
						class="rounded-md border px-2 py-2.5 text-[12.5px] num uppercase tracking-wider {kind === k ? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]' : 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
						style="-webkit-tap-highlight-color: transparent; min-height: 44px;">
						{k}
					</button>
				{/each}
			</div>
			{#if kind !== 'always'}
				<div class="mt-3">
					<div class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] mb-1">Active weekdays</div>
					<div class="flex flex-wrap gap-1.5">
						{#each WEEKDAYS as w}
							<button type="button" onclick={() => toggleWeekday(w.idx)}
								class="w-[3.25rem] rounded-md border py-1.5 text-[12px] num {weekdays.includes(w.idx) ? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]' : 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
								style="min-height: 36px;">
								{w.label}
							</button>
						{/each}
					</div>
				</div>
				<div class="mt-3 flex items-center gap-2 flex-wrap">
					<span class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Time window</span>
					<input type="time" bind:value={twStart} class="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1.5 text-[16px] num text-[var(--color-bright)]" />
					<span class="text-[var(--color-muted)]">→</span>
					<input type="time" bind:value={twEnd} class="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1.5 text-[16px] num text-[var(--color-bright)]" />
					<button type="button" class="text-[10px] uppercase tracking-wider text-[var(--color-faint)] active:text-[var(--color-bright)]" onclick={() => { twStart = ''; twEnd = ''; }}>clear</button>
				</div>
				{#if kind === 'biweekly'}
					<div class="mt-3 flex items-center gap-2 flex-wrap">
						<span class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Anchor date</span>
						<input type="date" bind:value={anchorDate} class="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1.5 text-[16px] num text-[var(--color-bright)]" />
					</div>
				{/if}
			{:else}
				<div class="mt-1 text-[10.5px] text-[var(--color-faint)]">Always on duty.</div>
			{/if}
		</section>

		<!-- Recurring quiet hours -->
		<section>
			<label class="flex items-center gap-2 mb-1">
				<input type="checkbox" bind:checked={rdtEnabled} class="accent-[var(--color-ok)] h-4 w-4" />
				<span class="text-[11px] uppercase tracking-wider text-[var(--color-muted)]">Quiet hours (recurring, UTC)</span>
			</label>
			{#if rdtEnabled}
				<div class="mt-2 flex items-center gap-2 flex-wrap">
					<input type="time" bind:value={rdtStart} class="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1.5 text-[16px] num text-[var(--color-bright)]" />
					<span class="text-[var(--color-muted)]">→</span>
					<input type="time" bind:value={rdtEnd} class="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1.5 text-[16px] num text-[var(--color-bright)]" />
					<span class="text-[10.5px] text-[var(--color-faint)] num">
						{#if rdtStart && rdtEnd}
							{rdtStart > rdtEnd ? 'overnight' : 'same day'}
						{/if}
					</span>
				</div>
				<div class="mt-2 flex flex-wrap gap-1.5">
					{#each WEEKDAYS as w}
						<button type="button" onclick={() => toggleRdtWeekday(w.idx)}
							class="w-[3.25rem] rounded-md border py-1.5 text-[12px] num {rdtWeekdays.includes(w.idx) ? 'border-[var(--color-warn)] bg-[var(--color-warn)]/15 text-[var(--color-bright)]' : 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
							style="min-height: 36px;">
							{w.label}
						</button>
					{/each}
				</div>
				<div class="mt-1 text-[10.5px] text-[var(--color-faint)]">
					Notifications are suppressed during this window on the selected days.
				</div>
			{:else}
				<div class="text-[10.5px] text-[var(--color-faint)]">No recurring quiet hours.</div>
			{/if}
		</section>

		<!-- Save -->
		<section class="border-t border-[var(--color-border)] pt-4">
			<div class="mb-2 text-[12px]">
				{#if error}<span class="text-[var(--color-fail)]">{error}</span>
				{:else if info}<span class="text-[var(--color-ok)]">{info}</span>{/if}
			</div>
			<button
				type="button"
				onclick={save}
				disabled={saving}
				class="w-full rounded-md border border-[var(--color-ok)] bg-[var(--color-ok)]/15 px-4 py-3 text-[14px] uppercase tracking-wider text-[var(--color-bright)] active:bg-[var(--color-ok)]/25 disabled:opacity-50"
				style="-webkit-tap-highlight-color: transparent; min-height: 48px;"
			>
				{saving ? 'saving…' : 'save routing'}
			</button>
		</section>
	</div>
{/if}
