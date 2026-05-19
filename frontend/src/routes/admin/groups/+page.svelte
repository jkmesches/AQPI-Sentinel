<script lang="ts">
	/** /admin/groups — full CRUD for notification groups.
	 *
	 *  Each group bundles users + a notification schedule (always / weekly /
	 *  biweekly with anchor + downtime). Inheritance from a parent group is
	 *  expressed as parent_group_id; the alert routing pipeline ANDs both
	 *  schedules before dispatching.
	 *
	 *  Layout is master/detail: list on the left, editor on the right.
	 */
	import { onMount } from 'svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { url as apiUrl } from '$lib/origin';

	type User = { id: number; email: string; display_name: string | null };
	type Group = {
		id: number;
		name: string;
		description: string;
		parent_group_id: number | null;
		parent_name: string | null;
		schedule: any;
		members: { id: number; email: string; display_name: string | null }[];
		created_at: string | null;
		updated_at: string | null;
	};

	const WEEKDAYS = [
		{ idx: 0, label: 'Mon' },
		{ idx: 1, label: 'Tue' },
		{ idx: 2, label: 'Wed' },
		{ idx: 3, label: 'Thu' },
		{ idx: 4, label: 'Fri' },
		{ idx: 5, label: 'Sat' },
		{ idx: 6, label: 'Sun' }
	];

	let groups = $state<Group[]>([]);
	let users = $state<User[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let info = $state<string | null>(null);

	let selectedId = $state<number | null>(null);

	type RecurringDowntime = { weekdays: number[]; start: string; end: string };
	type Draft = {
		id: number | null;
		name: string;
		description: string;
		parent_group_id: number | null;
		kind: 'always' | 'weekly' | 'biweekly';
		weekdays: number[];
		time_windows: { start: string; end: string }[];
		anchor_date: string;
		downtime: { start: string; end: string }[];
		recurring_downtime: RecurringDowntime[];
		member_ids: number[];
	};

	function freshDraft(): Draft {
		return {
			id: null,
			name: '',
			description: '',
			parent_group_id: null,
			kind: 'always',
			weekdays: [0, 1, 2, 3, 4],
			time_windows: [],
			anchor_date: new Date().toISOString().slice(0, 10),
			downtime: [],
			recurring_downtime: [],
			member_ids: []
		};
	}

	let draft = $state<Draft>(freshDraft());
	let previewWindows = $state<{ start: string; end: string }[]>([]);
	let previewActiveNow = $state(false);

	function authHeaders() {
		return auth.token ? { Authorization: `Bearer ${auth.token}` } : {};
	}

	async function loadAll() {
		loading = true;
		error = null;
		try {
			const [gr, ur] = await Promise.all([
				fetch(apiUrl('/api/admin/groups'),  { headers: authHeaders() }),
				fetch(apiUrl('/api/admin/users'),   { headers: authHeaders() })
			]);
			if (!gr.ok) throw new Error(`groups HTTP ${gr.status}`);
			if (!ur.ok) throw new Error(`users HTTP ${ur.status}`);
			groups = await gr.json();
			users  = await ur.json();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			loading = false;
		}
	}

	onMount(loadAll);

	function selectGroup(g: Group) {
		selectedId = g.id;
		const s = g.schedule || {};
		draft = {
			id: g.id,
			name: g.name,
			description: g.description ?? '',
			parent_group_id: g.parent_group_id,
			kind: s.kind ?? 'always',
			weekdays: Array.isArray(s.weekdays) ? [...s.weekdays] : [0, 1, 2, 3, 4],
			time_windows: (s.time_windows ?? []).map((p: string[]) => ({ start: p[0], end: p[1] })),
			anchor_date: s.anchor_date ?? new Date().toISOString().slice(0, 10),
			downtime: (s.downtime ?? []).map((d: any) => ({ start: d.start ?? '', end: d.end ?? '' })),
			recurring_downtime: (s.recurring_downtime ?? []).map((r: any) => ({
				weekdays: Array.isArray(r.weekdays) ? [...r.weekdays] : [0,1,2,3,4,5,6],
				start: (r.time_window?.[0] ?? '22:00'),
				end:   (r.time_window?.[1] ?? '06:00')
			})),
			member_ids: g.members.map((m) => m.id)
		};
		info = null;
		previewWindows = [];
	}

	function newGroup() {
		selectedId = null;
		draft = freshDraft();
		info = null;
		previewWindows = [];
	}

	function toggleWeekday(idx: number) {
		draft.weekdays = draft.weekdays.includes(idx)
			? draft.weekdays.filter((d) => d !== idx)
			: [...draft.weekdays, idx].sort();
	}

	function addTimeWindow() { draft.time_windows = [...draft.time_windows, { start: '09:00', end: '17:00' }]; }
	function removeTimeWindow(i: number) { draft.time_windows = draft.time_windows.filter((_, j) => j !== i); }
	function addDowntime()   { draft.downtime = [...draft.downtime, { start: '', end: '' }]; }
	function removeDowntime(i: number) { draft.downtime = draft.downtime.filter((_, j) => j !== i); }
	function addRecurringDowntime() {
		// Sensible default: nightly 22:00 → 06:00 every day.
		draft.recurring_downtime = [
			...draft.recurring_downtime,
			{ weekdays: [0, 1, 2, 3, 4, 5, 6], start: '22:00', end: '06:00' }
		];
	}
	function removeRecurringDowntime(i: number) {
		draft.recurring_downtime = draft.recurring_downtime.filter((_, j) => j !== i);
	}
	function toggleRecurringWeekday(i: number, wd: number) {
		const wds = draft.recurring_downtime[i].weekdays;
		draft.recurring_downtime[i].weekdays = wds.includes(wd)
			? wds.filter((d) => d !== wd)
			: [...wds, wd].sort();
	}
	function toggleMember(id: number) {
		draft.member_ids = draft.member_ids.includes(id)
			? draft.member_ids.filter((u) => u !== id)
			: [...draft.member_ids, id];
	}

	function buildSchedule() {
		if (draft.kind === 'always') {
			return draft.downtime.length
				? { kind: 'always', downtime: draft.downtime.filter((d) => d.start && d.end) }
				: {};
		}
		const sched: any = {
			kind: draft.kind,
			weekdays: [...draft.weekdays].sort(),
			time_windows: draft.time_windows.map((w) => [w.start, w.end])
		};
		if (draft.kind === 'biweekly') sched.anchor_date = draft.anchor_date;
		const dt = draft.downtime.filter((d) => d.start && d.end);
		if (dt.length) sched.downtime = dt;
		const rdt = draft.recurring_downtime
			.filter((r) => r.start && r.end && r.weekdays.length > 0)
			.map((r) => ({ weekdays: [...r.weekdays].sort(), time_window: [r.start, r.end] }));
		if (rdt.length) sched.recurring_downtime = rdt;
		// Recurring downtime is meaningful even when kind=always, so don't
		// short-circuit to `{}` if downtime exists.
		if (Object.keys(sched).length === 0) return {};
		if (!sched.kind) sched.kind = 'always';
		return sched;
	}

	async function preview() {
		try {
			const r = await fetch(apiUrl(`/api/admin/groups/${draft.id ?? 0}/preview`), {
				method: 'POST',
				headers: { 'content-type': 'application/json', ...authHeaders() },
				body: JSON.stringify({ schedule: buildSchedule(), count: 5 })
			});
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			const j = await r.json();
			previewWindows = j.windows;
			previewActiveNow = j.active_now;
		} catch (e) {
			error = (e as Error).message;
		}
	}

	async function save() {
		error = null;
		info = null;
		if (!draft.name.trim()) { error = 'Name required.'; return; }
		const body = {
			name: draft.name,
			description: draft.description,
			parent_group_id: draft.parent_group_id,
			schedule: buildSchedule(),
			member_ids: draft.member_ids
		};
		const url = draft.id ? `/api/admin/groups/${draft.id}` : '/api/admin/groups';
		const method = draft.id ? 'PUT' : 'POST';
		try {
			const r = await fetch(apiUrl(url), {
				method,
				headers: { 'content-type': 'application/json', ...authHeaders() },
				body: JSON.stringify(body)
			});
			if (!r.ok) {
				const j = await r.json().catch(() => ({}));
				throw new Error(j.detail ?? `HTTP ${r.status}`);
			}
			const j = await r.json();
			info = draft.id ? 'Saved.' : 'Created.';
			await loadAll();
			selectGroup(groups.find((g) => g.id === j.id) ?? j);
		} catch (e) {
			error = (e as Error).message;
		}
	}

	async function del() {
		if (!draft.id) return;
		if (!confirm(`Delete group "${draft.name}"? Its members are not deleted.`)) return;
		try {
			const r = await fetch(apiUrl(`/api/admin/groups/${draft.id}`), {
				method: 'DELETE', headers: authHeaders()
			});
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			await loadAll();
			newGroup();
			info = 'Deleted.';
		} catch (e) {
			error = (e as Error).message;
		}
	}

	// Filter parent options to avoid creating a cycle: a group can't pick
	// itself OR any of its descendants as parent. Cheap approximation: just
	// exclude self for now — server also rejects parent==self.
	const parentOptions = $derived(groups.filter((g) => g.id !== draft.id));
</script>

<div class="flex h-full flex-col">
	<header class="flex items-center justify-between border-b border-[var(--color-border)] px-5 py-3">
		<div class="flex items-baseline gap-3">
			<span class="label tracking-[0.18em] text-[var(--color-bright)]">GROUPS</span>
			<span class="text-[11px] text-[var(--color-faint)]">
				Bundle users + a notification schedule. Used by alert routing to gate dispatch by time-of-day, weekday, or biweekly cadence.
			</span>
		</div>
		<button
			class="border border-[var(--color-ok)] bg-[var(--color-ok)]/15 px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-ok)]/25"
			onclick={newGroup}
		>
			+ new group
		</button>
	</header>

	{#if loading}
		<div class="px-5 py-6 text-[12px] text-[var(--color-muted)]">loading…</div>
	{:else}
		<div class="flex flex-1 overflow-hidden">
			<!-- Sidebar list -->
			<aside class="w-56 shrink-0 overflow-y-auto border-r border-[var(--color-border)] bg-[var(--color-canvas)]/40">
				<ul>
					{#each groups as g}
						<li>
							<button
								class="flex w-full flex-col items-start gap-0.5 border-b border-[var(--color-border)]/40 px-3 py-2 text-left text-[12px] {selectedId === g.id ? 'bg-[var(--color-elevated)]' : 'hover:bg-[var(--color-elevated)]/40'}"
								onclick={() => selectGroup(g)}
							>
								<span class="num font-medium text-[var(--color-bright)]">{g.name}</span>
								<span class="text-[10px] text-[var(--color-faint)]">
									{g.members.length} members
									{#if g.parent_name}· inherits {g.parent_name}{/if}
								</span>
							</button>
						</li>
					{/each}
					{#if groups.length === 0}
						<li class="px-3 py-3 text-[11px] text-[var(--color-faint)] italic">no groups yet</li>
					{/if}
				</ul>
			</aside>

			<!-- Editor -->
			<main class="flex-1 overflow-y-auto px-6 py-5 text-[12px]">
				<div class="max-w-2xl space-y-5">
					<!-- Identity -->
					<section class="space-y-2">
						<div class="flex items-center gap-3">
							<label class="flex flex-1 items-center gap-2">
								<span class="w-24 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Name</span>
								<input
									type="text"
									bind:value={draft.name}
									class="flex-1 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[12px] num text-[var(--color-bright)]"
								/>
							</label>
						</div>
						<label class="flex items-center gap-2">
							<span class="w-24 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Description</span>
							<input
								type="text"
								bind:value={draft.description}
								class="flex-1 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[12px] text-[var(--color-default)]"
							/>
						</label>
						<label class="flex items-center gap-2">
							<span class="w-24 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Inherits from</span>
							<select
								value={draft.parent_group_id ?? ''}
								onchange={(e) => (draft.parent_group_id = (e.target as HTMLSelectElement).value ? Number((e.target as HTMLSelectElement).value) : null)}
								class="flex-1 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[12px] num text-[var(--color-bright)]"
							>
								<option value="">— none —</option>
								{#each parentOptions as p}
									<option value={p.id}>{p.name}</option>
								{/each}
							</select>
						</label>
					</section>

					<!-- Schedule kind -->
					<section>
						<div class="mb-2 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Notification schedule</div>
						<div class="flex gap-1">
							{#each ['always', 'weekly', 'biweekly'] as k}
								<button
									type="button"
									onclick={() => (draft.kind = k as any)}
									class="rounded-sm border px-3 py-1 text-[11px] num uppercase tracking-wider transition-colors {draft.kind === k
										? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
										: 'border-[var(--color-border-strong)] text-[var(--color-muted)] hover:bg-[var(--color-elevated)]/40 hover:text-[var(--color-default)]'}"
								>
									{k}
								</button>
							{/each}
						</div>
					</section>

					{#if draft.kind !== 'always'}
						<!-- Weekday mask -->
						<section>
							<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Active weekdays (UTC)</div>
							<div class="flex gap-1">
								{#each WEEKDAYS as w}
									<button
										type="button"
										onclick={() => toggleWeekday(w.idx)}
										class="w-12 rounded-sm border px-2 py-1 text-[11px] num {draft.weekdays.includes(w.idx)
											? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
											: 'border-[var(--color-border-strong)] text-[var(--color-muted)] hover:bg-[var(--color-elevated)]/40'}"
									>
										{w.label}
									</button>
								{/each}
							</div>
						</section>

						<!-- Time windows -->
						<section>
							<div class="mb-1 flex items-center justify-between">
								<span class="text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Time windows (UTC)</span>
								<button class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]" onclick={addTimeWindow}>+ add</button>
							</div>
							{#if draft.time_windows.length === 0}
								<div class="text-[11px] text-[var(--color-faint)] italic">No time windows = active the whole day on matching weekdays.</div>
							{:else}
								<ul class="space-y-1">
									{#each draft.time_windows as w, i}
										<li class="flex items-center gap-2">
											<input type="time" bind:value={w.start} class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)]" />
											<span class="text-[var(--color-muted)]">→</span>
											<input type="time" bind:value={w.end} class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)]" />
											<button class="text-[10.5px] text-[var(--color-faint)] hover:text-[var(--color-fail)]" onclick={() => removeTimeWindow(i)}>remove</button>
										</li>
									{/each}
								</ul>
							{/if}
						</section>

						{#if draft.kind === 'biweekly'}
							<section>
								<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Anchor date (defines on-weeks)</div>
								<input
									type="date"
									bind:value={draft.anchor_date}
									class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11px] num text-[var(--color-bright)]"
								/>
								<div class="mt-1 text-[10.5px] text-[var(--color-faint)]">
									On-weeks: the week containing the anchor date, and every other week after. Adjust to align with an existing rotation.
								</div>
							</section>
						{/if}
					{/if}

					<!-- One-off downtime (specific dates) -->
					<section>
						<div class="mb-1 flex items-center justify-between">
							<span class="text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">One-off downtime (UTC)</span>
							<button class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]" onclick={addDowntime}>+ add</button>
						</div>
						{#if draft.downtime.length === 0}
							<div class="text-[11px] text-[var(--color-faint)] italic">Suppress notifications during specific date ranges (vacations, maintenance).</div>
						{:else}
							<ul class="space-y-1">
								{#each draft.downtime as d, i}
									<li class="flex items-center gap-2">
										<input type="datetime-local" bind:value={d.start} class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)]" />
										<span class="text-[var(--color-muted)]">→</span>
										<input type="datetime-local" bind:value={d.end}   class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)]" />
										<button class="text-[10.5px] text-[var(--color-faint)] hover:text-[var(--color-fail)]" onclick={() => removeDowntime(i)}>remove</button>
									</li>
								{/each}
							</ul>
						{/if}
					</section>

					<!-- Recurring downtime (nightly / weekly / daily blackouts) -->
					<section>
						<div class="mb-1 flex items-center justify-between">
							<span class="text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Recurring downtime (UTC)</span>
							<button class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]" onclick={addRecurringDowntime}>+ add</button>
						</div>
						{#if draft.recurring_downtime.length === 0}
							<div class="text-[11px] text-[var(--color-faint)] italic">
								Quiet hours that repeat each week. E.g. nightly 22:00 → 06:00, weekends only, or weekday lunch.
							</div>
						{:else}
							<ul class="space-y-2">
								{#each draft.recurring_downtime as r, i}
									<li class="rounded-sm border border-[var(--color-border)] bg-[var(--color-canvas)]/50 px-2 py-2">
										<div class="mb-1 flex flex-wrap items-center gap-1">
											{#each WEEKDAYS as w}
												<button type="button" onclick={() => toggleRecurringWeekday(i, w.idx)}
													class="w-10 rounded-sm border px-1 py-0.5 text-[10.5px] num {r.weekdays.includes(w.idx)
														? 'border-[var(--color-warn)] bg-[var(--color-warn)]/15 text-[var(--color-bright)]'
														: 'border-[var(--color-border-strong)] text-[var(--color-muted)] hover:bg-[var(--color-elevated)]/40'}">
													{w.label}
												</button>
											{/each}
										</div>
										<div class="flex items-center gap-2">
											<input type="time" bind:value={r.start} class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)]" />
											<span class="text-[var(--color-muted)]">→</span>
											<input type="time" bind:value={r.end}   class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-0.5 text-[11px] num text-[var(--color-bright)]" />
											<span class="text-[10px] text-[var(--color-faint)] num">
												{#if r.start && r.end}
													{#if r.start > r.end}
														overnight ({r.start} → 24:00 + 00:00 → {r.end})
													{:else}
														{r.start} → {r.end} UTC
													{/if}
												{/if}
											</span>
											<button class="ml-auto text-[10.5px] text-[var(--color-faint)] hover:text-[var(--color-fail)]" onclick={() => removeRecurringDowntime(i)}>remove</button>
										</div>
									</li>
								{/each}
							</ul>
						{/if}
					</section>

					<!-- Members -->
					<section>
						<div class="mb-1 text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">
							Members ({draft.member_ids.length})
						</div>
						<ul class="max-h-[180px] overflow-y-auto rounded-sm border border-[var(--color-border)]">
							{#each users as u}
								{@const checked = draft.member_ids.includes(u.id)}
								<li>
									<button
										type="button"
										onclick={() => toggleMember(u.id)}
										class="flex w-full items-center gap-2 border-b border-[var(--color-border)]/40 px-3 py-1.5 text-left text-[11.5px] hover:bg-[var(--color-elevated)]/40"
									>
										<span class="inline-flex h-3 w-3 items-center justify-center rounded-[2px] border {checked ? 'border-[var(--color-ok)] bg-[var(--color-ok)]/20 text-[var(--color-ok)]' : 'border-[var(--color-border-strong)] text-transparent'}">
											<svg viewBox="0 0 10 10" width="8" height="8" fill="none" stroke="currentColor" stroke-width="2">
												<polyline points="2,5 4,7 8,3" />
											</svg>
										</span>
										<span class="num text-[var(--color-bright)]">{u.email}</span>
										{#if u.display_name}<span class="text-[10.5px] text-[var(--color-faint)]">{u.display_name}</span>{/if}
									</button>
								</li>
							{/each}
						</ul>
					</section>

					<!-- Preview -->
					<section>
						<div class="flex items-center justify-between mb-1">
							<span class="text-[10px] uppercase tracking-[0.14em] text-[var(--color-muted)]">Schedule preview</span>
							<button class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]" onclick={preview}>refresh preview →</button>
						</div>
						{#if previewWindows.length === 0}
							<div class="text-[11px] text-[var(--color-faint)] italic">Click "refresh preview" to see the next 5 on-windows.</div>
						{:else}
							<div class="text-[10.5px] num text-[var(--color-muted)]">
								Active now? <span class={previewActiveNow ? 'text-[var(--color-ok)]' : 'text-[var(--color-fail)]'}>{previewActiveNow ? 'yes' : 'no'}</span>
							</div>
							<ul class="mt-1 space-y-0.5 text-[11px] num text-[var(--color-default)]">
								{#each previewWindows as w}
									<li>{w.start.slice(0, 16).replace('T', ' ')}Z → {w.end.slice(0, 16).replace('T', ' ')}Z</li>
								{/each}
							</ul>
						{/if}
					</section>

					<!-- Status + actions -->
					<section class="flex items-center justify-between border-t border-[var(--color-border)] pt-3">
						<div class="text-[10.5px] text-[var(--color-faint)]">
							{#if error}<span class="text-[var(--color-fail)]">{error}</span>
							{:else if info}<span class="text-[var(--color-ok)]">{info}</span>
							{/if}
						</div>
						<div class="flex gap-2">
							{#if draft.id}
								<button class="border border-[var(--color-fail)]/50 px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-fail)] hover:bg-[var(--color-fail)]/10" onclick={del}>
									delete
								</button>
							{/if}
							<button class="border border-[var(--color-ok)] bg-[var(--color-ok)]/15 px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-ok)]/25" onclick={save}>
								{draft.id ? 'save' : 'create'}
							</button>
						</div>
					</section>
				</div>
			</main>
		</div>
	{/if}
</div>
