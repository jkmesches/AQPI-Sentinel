<script lang="ts">
	/** /admin/silences — manage suppression windows for the alarm engine.
	 *
	 *  Three big UX changes vs the previous version:
	 *   1. Matchers no longer require JSON: SilenceMatcherPicker exposes the
	 *      common presets (All / Radars / Products / Website) + a Custom panel.
	 *   2. The datetime inputs are always paired with a UTC↔Local toggle so
	 *      the user knows exactly which timezone they're entering. Internal
	 *      storage stays ISO-8601 UTC.
	 *   3. Each row has an Edit button that re-opens the form pre-filled.
	 */
	import { onMount } from 'svelte';
	import SilenceMatcherPicker from '$lib/components/SilenceMatcherPicker.svelte';
	import { stageLabel } from '$lib/format';

	interface Silence {
		id: string;
		matchers: Record<string, string>;
		starts: string;
		ends: string;
		reason: string | null;
		created_at: string;
		created_by: string | null;
	}

	let rows = $state<Silence[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let info = $state<string | null>(null);

	// Form state. `formId === null` = creating; otherwise editing that id.
	let formOpen = $state(false);
	let formId = $state<string | null>(null);     // null = new
	let idDraft = $state('');                     // editable id (only on new)
	let matchers = $state<Record<string, string>>({});
	let starts = $state(new Date().toISOString().slice(0, 16));
	let ends   = $state(new Date(Date.now() + 86400_000).toISOString().slice(0, 16));
	let tz = $state<'UTC' | 'Local'>('UTC');
	let reason = $state('');
	let busy = $state(false);

	async function load() {
		loading = true;
		error = null;
		try {
			const r = await fetch('/api/silences');
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			rows = await r.json();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			loading = false;
		}
	}

	onMount(load);

	function fmt(iso: string): string {
		return iso.slice(0, 19).replace('T', ' ') + ' UTC';
	}
	function active(s: Silence): boolean {
		const now = Date.now();
		return Date.parse(s.starts) <= now && now < Date.parse(s.ends);
	}

	// Convert the form's datetime-local string to a stored ISO-UTC string.
	// `dt` is "YYYY-MM-DDTHH:MM" with no zone. If tz === 'UTC' we treat it as
	// UTC literally (append "Z"); if tz === 'Local' we let `new Date()` apply
	// the browser zone and read the UTC equivalent.
	function localToIso(dt: string, tz: 'UTC' | 'Local'): string {
		if (!dt) return '';
		if (tz === 'UTC') return new Date(dt + 'Z').toISOString();
		return new Date(dt).toISOString();
	}
	// Convert a stored ISO-UTC string back to a datetime-local input value
	// under the chosen tz.
	function isoToInput(iso: string, tz: 'UTC' | 'Local'): string {
		if (!iso) return '';
		const d = new Date(iso);
		if (tz === 'UTC') {
			return d.toISOString().slice(0, 16);
		}
		// Local: produce a string in the browser's local time zone, no Z.
		const off = d.getTimezoneOffset() * 60_000;
		return new Date(d.getTime() - off).toISOString().slice(0, 16);
	}

	function browserZone(): string {
		try {
			return Intl.DateTimeFormat().resolvedOptions().timeZone ?? 'Local';
		} catch {
			return 'Local';
		}
	}

	// Auto-generate a friendly id from the matcher preset + timestamp when
	// the user hasn't supplied one. Empty matchers → "all-silence"; stage-
	// only matcher → "<descriptor>-silence"; multi-key matcher →
	// "custom-silence". Always suffixed with HHMM to disambiguate.
	function autoId(m: Record<string, string>): string {
		const keys = Object.keys(m);
		let base = 'custom';
		if (keys.length === 0) base = 'all';
		else if (keys.length === 1 && m.stage) {
			base = stageLabel(m.stage).toLowerCase().replace(/\s+/g, '-');
		}
		const d = new Date();
		const stamp = d.toISOString().slice(2, 16).replace(/[-T:]/g, '');
		return `${base}-silence-${stamp}`;
	}

	function startNew() {
		formOpen = true;
		formId = null;
		matchers = {};
		const now = new Date();
		starts = isoToInput(now.toISOString(), tz);
		ends   = isoToInput(new Date(now.getTime() + 86400_000).toISOString(), tz);
		idDraft = '';
		reason = '';
		info = null;
		error = null;
	}

	function startEdit(s: Silence) {
		formOpen = true;
		formId = s.id;
		matchers = { ...s.matchers };
		starts = isoToInput(s.starts, tz);
		ends   = isoToInput(s.ends,   tz);
		idDraft = s.id;
		reason = s.reason ?? '';
		info = null;
		error = null;
	}

	function cancel() {
		formOpen = false;
		info = null;
		error = null;
	}

	async function save(e: Event) {
		e.preventDefault();
		busy = true;
		error = null;
		try {
			const sid = formId ?? (idDraft.trim() || autoId(matchers));
			const url = formId ? `/api/silences/${encodeURIComponent(formId)}` : '/api/silences';
			const method = formId ? 'PUT' : 'POST';
			const body: any = {
				matchers,
				starts: localToIso(starts, tz),
				ends:   localToIso(ends, tz),
				reason: reason || null
			};
			if (!formId) body.id = sid;
			const r = await fetch(url, {
				method,
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify(body)
			});
			if (!r.ok) {
				const j = await r.json().catch(() => ({}));
				throw new Error(j?.detail ?? `HTTP ${r.status}`);
			}
			info = formId ? `Updated silence "${sid}".` : `Created silence "${sid}".`;
			formOpen = false;
			await load();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			busy = false;
		}
	}

	async function del(sid: string) {
		if (!confirm(`Delete silence "${sid}"?`)) return;
		try {
			const r = await fetch(`/api/silences/${encodeURIComponent(sid)}`, { method: 'DELETE' });
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			info = `Deleted silence "${sid}".`;
			await load();
		} catch (e) {
			error = (e as Error).message;
		}
	}

	function matcherSummary(m: Record<string, string>): string {
		const keys = Object.keys(m);
		if (keys.length === 0) return 'All alarms';
		if (keys.length === 1 && m.stage === 'L0') return 'All website';
		if (keys.length === 1 && m.stage === 'L1') return 'All products';
		if (keys.length === 1 && m.stage === 'L2') return 'All radars';
		return Object.entries(m).map(([k, v]) => `${k}=${v}`).join(' · ');
	}

	// React to tz toggle: re-format the visible inputs without changing
	// the underlying stored values.
	function onTzChange(newTz: 'UTC' | 'Local') {
		const oldTz = tz;
		if (oldTz === newTz) return;
		// Convert the current displayed values from the old tz interpretation
		// back to ISO, then re-format in the new tz.
		const startsIso = localToIso(starts, oldTz);
		const endsIso   = localToIso(ends,   oldTz);
		tz = newTz;
		if (startsIso) starts = isoToInput(startsIso, newTz);
		if (endsIso)   ends   = isoToInput(endsIso, newTz);
	}
</script>

<div class="p-6 max-w-5xl">
	<header class="flex items-baseline justify-between mb-6">
		<div>
			<div class="label tracking-[0.18em] text-[var(--color-bright)] mb-1">SILENCES</div>
			<div class="text-[11px] text-[var(--color-muted)] max-w-2xl">
				Suppress alarms matching the given criteria during a window. Useful during planned downtime or known-bad upstream issues.
			</div>
		</div>
		<button
			class="border border-[var(--color-ok)] bg-[var(--color-ok)]/15 px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-ok)]/25"
			onclick={startNew}
		>
			+ new silence
		</button>
	</header>

	{#if error}
		<div class="mb-4 border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-3 py-2 text-[12px] text-[var(--color-fail)]">{error}</div>
	{:else if info}
		<div class="mb-4 border border-[var(--color-ok)]/40 bg-[var(--color-ok)]/10 px-3 py-2 text-[12px] text-[var(--color-ok)]">{info}</div>
	{/if}

	<!-- LIST -->
	{#if loading}
		<div class="text-[12px] text-[var(--color-muted)]">loading…</div>
	{:else if !rows.length}
		<div class="text-[12px] text-[var(--color-faint)] italic mb-4">no silences.</div>
	{:else}
		<table class="w-full text-[11px] mb-6">
			<thead class="text-[var(--color-muted)]">
				<tr class="border-b border-[var(--color-border)]">
					<th class="px-2 py-1 text-left">id</th>
					<th class="px-2 py-1 text-left">matcher</th>
					<th class="px-2 py-1 text-left">window (UTC)</th>
					<th class="px-2 py-1 text-left">reason</th>
					<th class="px-2 py-1 text-left">by</th>
					<th class="px-2 py-1"></th>
				</tr>
			</thead>
			<tbody>
				{#each rows as s}
					<tr class="border-b border-[var(--color-border)] {active(s) ? '' : 'opacity-50'}">
						<td class="px-2 py-1 num text-[var(--color-bright)]">{s.id}</td>
						<td class="px-2 py-1 num text-[var(--color-default)]" title={JSON.stringify(s.matchers)}>{matcherSummary(s.matchers)}</td>
						<td class="px-2 py-1 num text-[var(--color-muted)]">
							{fmt(s.starts)} → {fmt(s.ends)}
							{#if active(s)}<span class="ml-1 text-[var(--color-ok)] uppercase text-[9.5px]">active</span>{/if}
						</td>
						<td class="px-2 py-1 text-[var(--color-muted)] truncate max-w-md">{s.reason ?? '—'}</td>
						<td class="px-2 py-1 num text-[var(--color-muted)]">{s.created_by ?? '?'}</td>
						<td class="px-2 py-1 text-right whitespace-nowrap">
							<button class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)] mr-2" onclick={() => startEdit(s)}>edit</button>
							<button class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-fail)]" onclick={() => del(s.id)}>delete</button>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}

	<!-- FORM (modal-ish; renders as a card inline at the bottom) -->
	{#if formOpen}
		<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/55 backdrop-blur-sm p-4 overflow-y-auto" role="dialog" aria-modal="true" onclick={cancel}>
			<form
				class="relative w-full max-w-[640px] rounded-md border border-[var(--color-border-strong)] bg-[var(--color-surface)] shadow-2xl"
				onsubmit={save}
				onclick={(e) => e.stopPropagation()}
			>
				<header class="flex items-center justify-between border-b border-[var(--color-border)] px-5 py-3">
					<span class="label tracking-[0.16em] text-[var(--color-bright)]">{formId ? `EDIT SILENCE · ${formId}` : 'NEW SILENCE'}</span>
					<button type="button" class="text-[var(--color-muted)] hover:text-[var(--color-bright)]" aria-label="close" onclick={cancel}>×</button>
				</header>

				<div class="px-5 py-4 space-y-3 text-[12px]">
					{#if !formId}
						<label class="flex items-center gap-2">
							<span class="w-24 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">ID</span>
							<input
								type="text"
								bind:value={idDraft}
								class="flex-1 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11.5px] num text-[var(--color-bright)]"
								placeholder="leave blank to auto-generate"
							/>
						</label>
					{/if}

					<label class="flex items-start gap-2">
						<span class="w-24 mt-1 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Matcher</span>
						<SilenceMatcherPicker bind:value={matchers} summaryWidth="w-full" />
					</label>

					<!-- Time-zone toggle for the window inputs -->
					<div class="flex items-center gap-2 pt-1">
						<span class="w-24 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Time zone</span>
						<div class="flex border border-[var(--color-border-strong)]">
							<button type="button" class="px-2 py-0.5 text-[11px] num {tz === 'UTC' ? 'bg-[var(--color-elevated)] text-[var(--color-bright)]' : 'text-[var(--color-muted)] hover:text-[var(--color-default)]'}" onclick={() => onTzChange('UTC')}>UTC</button>
							<button type="button" class="px-2 py-0.5 text-[11px] num {tz === 'Local' ? 'bg-[var(--color-elevated)] text-[var(--color-bright)]' : 'text-[var(--color-muted)] hover:text-[var(--color-default)]'}" onclick={() => onTzChange('Local')}>Local</button>
						</div>
						{#if tz === 'Local'}
							<span class="text-[10.5px] text-[var(--color-faint)] num">{browserZone()}</span>
						{/if}
						<span class="text-[10.5px] text-[var(--color-faint)] ml-auto">stored as UTC</span>
					</div>

					<label class="flex items-center gap-2">
						<span class="w-24 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Starts</span>
						<input type="datetime-local" bind:value={starts} class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11.5px] num text-[var(--color-bright)]" />
					</label>

					<label class="flex items-center gap-2">
						<span class="w-24 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Ends</span>
						<input type="datetime-local" bind:value={ends} class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11.5px] num text-[var(--color-bright)]" />
					</label>

					<label class="flex items-center gap-2">
						<span class="w-24 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Reason</span>
						<input type="text" bind:value={reason} class="flex-1 border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[11.5px] text-[var(--color-default)]" placeholder="planned downtime / known-bad upstream / …" />
					</label>
				</div>

				<footer class="flex items-center justify-end gap-2 border-t border-[var(--color-border)] bg-[var(--color-canvas)]/40 px-5 py-3">
					<button type="button" class="border border-[var(--color-border-strong)] px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]" onclick={cancel}>cancel</button>
					<button type="submit" disabled={busy} class="border border-[var(--color-ok)] bg-[var(--color-ok)]/15 px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-ok)]/25 disabled:opacity-50">
						{busy ? (formId ? 'saving…' : 'creating…') : (formId ? 'save changes' : 'create silence')}
					</button>
				</footer>
			</form>
		</div>
	{/if}
</div>
