<script lang="ts">
	import { onMount } from 'svelte';
	import { auth } from '$lib/stores/auth.svelte';

	type Source = 'frontend' | 'backend' | 'combined';

	/** One row of the picker, as /api/admin/digest/subjects describes it.
	 *  `frontend` / `backend` say which TIERS are registered on this
	 *  deployment — not which is selected. CBAND has no LB2 check without
	 *  SENTINEL_SSCB_ROOT, and a source whose tier is absent yields a row with
	 *  no data, which reads as an outage rather than as a misconfiguration. */
	interface SubjectEntry {
		key: string;
		kind: 'radar' | 'product';
		name: string;
		frontend: boolean;
		backend: boolean;
	}

	interface DigestConfig {
		enabled: boolean;
		recipients: string[];
		hour: number;
		tz: string;
		products: boolean;
		default_source: Record<string, Source>;
		/** Keyed "<kind>:<id>", and holding only DEVIATIONS from the default.
		 *  An absent entry means "shown, from the default source" — the server
		 *  drops entries that merely restate that, so a row reads "default"
		 *  again after a save and keeps following later changes to it. */
		subjects: Record<string, { show?: boolean; source?: Source }>;
	}

	let v = $state<DigestConfig>({
		enabled: false, recipients: [], hour: 7,
		tz: 'America/Denver', products: true,
		default_source: { radar: 'frontend', product: 'frontend' },
		subjects: {}
	});
	let radars = $state<SubjectEntry[]>([]);
	let products = $state<SubjectEntry[]>([]);
	/** The notification gate, which is NOT part of the digest config: it lives
	 *  in settings['alarm_tiers'] so that turning the morning email off cannot
	 *  change who gets paged at 03:00. Presented here because this is the page
	 *  about the two tiers. */
	let tiers = $state<{ frontend: boolean; backend: boolean }>({
		frontend: true, backend: true
	});
	let savingTiers = $state(false);
	/** Which per-subject panel is expanded. CONTROLLED, not derived from
	 *  "does this kind have overrides" — that expression changes as the user
	 *  edits, so clearing the last override would snap the panel shut under
	 *  their cursor. Seeded once when the lists load, then owned by the
	 *  <details> element. Same reasoning as GroupRow on the home page. */
	let openPanel = $state<Record<string, boolean>>({ radar: false, product: false });
	let recipientsText = $state('');
	let loading = $state(true);
	let saving = $state(false);
	let testing = $state(false);
	let previewing = $state(false);
	let testTo = $state('');
	let preview = $state<{ subject: string; text: string } | null>(null);
	let banner = $state<{ kind: 'ok' | 'err'; text: string } | null>(null);

	// A short list beats a free-text zone field nobody can spell from memory,
	// while still allowing anything IANA knows via the text input.
	const COMMON_TZ = [
		'America/Denver', 'America/Los_Angeles', 'America/Chicago',
		'America/New_York', 'UTC', 'Europe/London', 'Europe/Rome'
	];

	async function load() {
		loading = true;
		banner = null;
		try {
			const r = await fetch('/api/admin/digest');
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			v = { ...v, ...(await r.json()) };
			recipientsText = (v.recipients ?? []).join('\n');
			testTo = auth.user?.email ?? '';

			const rs = await fetch('/api/admin/digest/subjects');
			if (rs.ok) {
				const j = await rs.json();
				radars = j.radars ?? [];
				products = j.products ?? [];
				// Open the panel that already has something to show.
				openPanel = {
					radar: overrideCount('radar') > 0 || hiddenCount('radar') > 0,
					product: overrideCount('product') > 0 || hiddenCount('product') > 0
				};
			}
			// A 404 or a null value here is the normal state before anyone has
			// ever touched the gate, and it means both tiers are audible. Read
			// defensively for the same reason the server does: a gate that
			// goes quiet because it could not read itself is the one failure
			// nobody notices.
			pristine = snapshot(v, recipientsText);

			const rt = await fetch('/api/admin/settings/alarm_tiers');
			if (rt.ok) {
				const got = (await rt.json())?.value;
				if (got && typeof got === 'object') {
					tiers = {
						frontend: got.frontend !== false,
						backend: got.backend !== false
					};
				}
			}
		} catch (e) {
			banner = { kind: 'err', text: `Load failed: ${(e as Error).message}` };
		} finally {
			loading = false;
		}
	}

	function parseRecipients(): string[] {
		return recipientsText
			.split(/[\n,;]+/)
			.map((s) => s.trim())
			.filter(Boolean);
	}

	async function save() {
		saving = true;
		banner = null;
		try {
			const body = {
				...v,
				recipients: parseRecipients(),
				hour: Number(v.hour),
				default_source: v.default_source,
				subjects: v.subjects
			};
			const r = await fetch('/api/admin/digest', {
				method: 'PUT',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify(body)
			});
			if (!r.ok) {
				const j = await r.json().catch(() => ({}));
				throw new Error(j.detail ?? `HTTP ${r.status}`);
			}
			// Rebuilt from the RESPONSE, not from what was sent. The server
			// normalises the blob — dropping any entry that restates the
			// default — so this is what makes a row snap back to "default"
			// instead of showing a stored value the server did not keep.
			v = { ...v, ...(await r.json()) };
			recipientsText = (v.recipients ?? []).join('\n');
			pristine = snapshot(v, recipientsText);
			banner = { kind: 'ok', text: 'Saved.' };
		} catch (e) {
			banner = { kind: 'err', text: `Save failed: ${(e as Error).message}` };
		} finally {
			saving = false;
		}
	}

	async function sendTest() {
		testing = true;
		banner = null;
		try {
			const r = await fetch('/api/admin/digest/test', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ to: testTo })
			});
			const j = await r.json().catch(() => ({}));
			if (!r.ok) throw new Error(j.detail ?? `HTTP ${r.status}`);
			banner = { kind: 'ok', text: `Test report sent to ${j.sent_to}.` };
		} catch (e) {
			banner = { kind: 'err', text: `Test failed: ${(e as Error).message}` };
		} finally {
			testing = false;
		}
	}

	async function loadPreview() {
		previewing = true;
		banner = null;
		try {
			const r = await fetch('/api/admin/digest/preview', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({})
			});
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			const j = await r.json();
			preview = { subject: j.subject, text: j.text };
		} catch (e) {
			banner = { kind: 'err', text: `Preview failed: ${(e as Error).message}` };
		} finally {
			previewing = false;
		}
	}

	const SOURCE_LABEL: Record<Source, string> = {
		frontend: 'radarca',
		backend: 'backend files',
		combined: 'worse of both'
	};

	function entryOf(kind: 'radar' | 'product', key: string) {
		return v.subjects[`${kind}:${key}`] ?? {};
	}

	/** The source a row will actually be built from. */
	function resolvedSource(kind: 'radar' | 'product', key: string): Source {
		return entryOf(kind, key).source ?? v.default_source[kind] ?? 'frontend';
	}

	function shown(kind: 'radar' | 'product', key: string): boolean {
		return entryOf(kind, key).show !== false;
	}

	function setSubject(
		kind: 'radar' | 'product',
		key: string,
		patch: { show?: boolean; source?: Source | '' }
	) {
		const id = `${kind}:${key}`;
		const next = { ...entryOf(kind, key), ...patch };
		// Mirror the server's normalisation locally so the control settles on
		// the same value the next load would show, rather than flickering
		// between "frontend" and "default (frontend)" around a save.
		if (!next.source) delete next.source;
		if (next.show !== false) delete next.show;
		if (Object.keys(next).length === 0) {
			const { [id]: _drop, ...rest } = v.subjects;
			v.subjects = rest;
		} else {
			v.subjects = { ...v.subjects, [id]: next as { show?: boolean; source?: Source } };
		}
	}

	/** Which tier this subject is missing, if any. Said on every such row,
	 *  dimmed, because it is the reason an option below is greyed out — an
	 *  unexplained disabled control reads as a bug in the page. */
	function tierGap(e: SubjectEntry): string | null {
		if (e.frontend && e.backend) return null;
		return e.frontend ? 'radarca only' : 'backend only';
	}

	/** Does this row's chosen signal need a tier this subject does not have?
	 *  Then the row is built from a family with no rows, and the report can
	 *  only show that as an absence — so it has to be said here instead. */
	function wouldBeEmpty(e: SubjectEntry): string | null {
		const src = resolvedSource(e.kind, e.key);
		const needsFe = src === 'frontend' || src === 'combined';
		const needsBe = src === 'backend' || src === 'combined';
		if (needsFe && !e.frontend) return 'no radarca check here';
		if (needsBe && !e.backend) return 'no backend check here';
		return null;
	}

	function rowsOf(kind: 'radar' | 'product'): SubjectEntry[] {
		return kind === 'radar' ? radars : products;
	}

	function hiddenCount(kind: 'radar' | 'product'): number {
		return rowsOf(kind).filter((e) => !shown(kind, e.key)).length;
	}

	function overrideCount(kind: 'radar' | 'product'): number {
		return rowsOf(kind).filter((e) => entryOf(kind, e.key).source).length;
	}

	function setAllShown(kind: 'radar' | 'product', show: boolean) {
		for (const e of rowsOf(kind)) setSubject(kind, e.key, { show });
	}

	/** A one-line statement of what the section currently does, so the
	 *  collapsed form still answers "what will the report contain". */
	function sectionSummary(kind: 'radar' | 'product'): string {
		const n = rowsOf(kind).length;
		const hid = hiddenCount(kind);
		const ovr = overrideCount(kind);
		const noun = kind === 'radar' ? 'radar' : 'product';
		const bits = [`${n - hid} of ${n} ${noun}${n - hid === 1 ? '' : 's'}`];
		bits.push(`from ${SOURCE_LABEL[v.default_source[kind] ?? 'frontend']}`);
		if (ovr) bits.push(`${ovr} overridden`);
		return bits.join(' · ');
	}

	/** Unsaved-changes marker. The per-subject list can be collapsed, so a
	 *  change made inside it must still be visible from the Save button. */
	let pristine = $state('');

	function snapshot(cfg: DigestConfig, recips: string): string {
		return JSON.stringify({
			...cfg, recipients: recips.split(/[\n,;]+/).map((x) => x.trim()).filter(Boolean)
		});
	}

	let dirty = $derived(pristine !== '' && snapshot(v, recipientsText) !== pristine);

	async function saveTiers() {
		savingTiers = true;
		banner = null;
		try {
			const r = await fetch('/api/admin/settings/alarm_tiers', {
				method: 'PUT',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ value: { ...tiers } })
			});
			if (!r.ok) {
				const j = await r.json().catch(() => ({}));
				throw new Error(j.detail ?? `HTTP ${r.status}`);
			}
			banner = { kind: 'ok', text: 'Notification gate saved.' };
		} catch (e) {
			banner = { kind: 'err', text: `Save failed: ${(e as Error).message}` };
		} finally {
			savingTiers = false;
		}
	}

	onMount(load);
</script>

<div class="p-4 max-w-3xl">
	<h1 class="text-[13px] uppercase tracking-[0.18em] text-[var(--color-bright)] mb-1">
		Daily report
	</h1>
	<div class="text-[12px] text-[var(--color-muted)] mb-4">
		One email each morning covering the previous 24 hours, with a row for every
		radar and product and a link to the checks behind each one.
	</div>

	{#if banner}
		<div
			class="mb-4 border px-3 py-2 text-[12px] {banner.kind === 'ok'
				? 'border-[var(--color-ok)]/40 bg-[var(--color-ok)]/10 text-[var(--color-ok)]'
				: 'border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 text-[var(--color-fail)]'}"
		>
			{banner.text}
		</div>
	{/if}

	{#if loading}
		<div class="text-[12px] text-[var(--color-muted)]">loading…</div>
	{:else}
		<div class="grid grid-cols-[10rem_1fr] gap-x-4 gap-y-3 text-[12px] items-start">
			<label for="dg-enabled" class="text-[var(--color-muted)] uppercase tracking-wider text-[10px] pt-1">send daily</label>
			<label class="flex items-center gap-2 text-[var(--color-default)]">
				<input id="dg-enabled" type="checkbox" bind:checked={v.enabled} />
				enabled
			</label>

			<label for="dg-to" class="text-[var(--color-muted)] uppercase tracking-wider text-[10px] pt-1">recipients</label>
			<div>
				<textarea
					id="dg-to"
					bind:value={recipientsText}
					rows="3"
					placeholder="ops@example.edu"
					class="w-full border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1.5 text-[12px] num"
				></textarea>
				<div class="text-[11px] text-[var(--color-muted)] pt-1">
					One per line. These receive the scheduled report — the test button below
					never sends to them.
				</div>
			</div>

			<label for="dg-hour" class="text-[var(--color-muted)] uppercase tracking-wider text-[10px] pt-1">send at</label>
			<div class="flex items-center gap-2">
				<input
					id="dg-hour"
					type="number"
					min="0"
					max="23"
					bind:value={v.hour}
					class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1.5 text-[12px] num w-20"
				/>
				<span class="text-[var(--color-muted)]">:00 local time in</span>
				<input
					list="dg-zones"
					bind:value={v.tz}
					class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1.5 text-[12px] num w-56"
				/>
				<datalist id="dg-zones">
					{#each COMMON_TZ as z}<option value={z}></option>{/each}
				</datalist>
			</div>

			<label for="dg-products" class="text-[var(--color-muted)] uppercase tracking-wider text-[10px] pt-1">include</label>
			<label class="flex items-center gap-2 text-[var(--color-default)]">
				<input id="dg-products" type="checkbox" bind:checked={v.products} />
				products as well as radars
			</label>
		</div>

		<div class="mt-6 border-t border-[var(--color-border)] pt-4">
			<div class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] mb-1">
				Which signal each row is built from
			</div>
			<div class="text-[11px] text-[var(--color-muted)] mb-3 max-w-2xl leading-[1.55]">
				Every radar and product is watched twice. <span class="text-[var(--color-default)]">radarca</span>
				is what the display tier believes; <span class="text-[var(--color-default)]">backend files</span>
				is what has actually landed on K2/Trinity. <span class="text-[var(--color-default)]">worse of both</span>
				takes the less healthy of the two at each timestep — where the tiers disagree, the
				disagreement is itself the signal, and its availability counts timesteps rather than
				runs so it stays comparable to the rows around it.
			</div>

			<div class="grid grid-cols-[8rem_1fr] gap-x-4 gap-y-2 text-[12px] items-center mb-4">
				<label for="dg-def-radar" class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">radars</label>
				<select
					id="dg-def-radar"
					bind:value={v.default_source.radar}
					class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] w-56"
				>
					<option value="frontend">radarca</option>
					<option value="backend">backend files</option>
					<option value="combined">worse of both</option>
				</select>

				<label for="dg-def-product" class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">products</label>
				<select
					id="dg-def-product"
					bind:value={v.default_source.product}
					class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1 text-[12px] w-56"
				>
					<option value="frontend">radarca</option>
					<option value="backend">backend files</option>
					<option value="combined">worse of both</option>
				</select>
			</div>
			<div class="text-[11px] text-[var(--color-muted)] mb-4 max-w-2xl">
				The default applies to anything below still set to <span class="num">default</span>,
				including a radar added after today. Only the rows you change are stored.
			</div>

			{#each [{ kind: 'radar' as const, title: 'Radars' }, { kind: 'product' as const, title: 'Products' }] as grp (grp.kind)}
				<details
					class="mb-3 border border-[var(--color-border)] bg-[var(--color-canvas)]"
					open={openPanel[grp.kind]}
					ontoggle={(ev) => (openPanel[grp.kind] = ev.currentTarget.open)}
				>
					<summary
						class="flex items-baseline gap-3 px-3 py-2 cursor-pointer select-none hover:bg-[var(--color-elevated)]"
					>
						<!-- A flex summary loses its native marker in Chrome, and a
						     disclosure nobody can see is just a section that mysteriously
						     hides half its content. -->
						<span class="chev text-[var(--color-muted)] text-[14px] leading-none w-2">›</span>
						<span class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] w-20 shrink-0">
							{grp.title}
						</span>
						<span class="text-[12px] text-[var(--color-default)]">
							{sectionSummary(grp.kind)}
						</span>
						{#if grp.kind === 'product' && !v.products}
							<span class="text-[11px] text-[var(--color-warn)]">
								excluded entirely — see “include” above
							</span>
						{/if}
						<span class="grow"></span>
						<span class="text-[11px] text-[var(--color-faint)] shrink-0">
							set per {grp.kind}
						</span>
					</summary>

					<div class="px-3 pb-3 pt-1 border-t border-[var(--color-border)]">
						{#if rowsOf(grp.kind).length === 0}
							<div class="text-[11px] text-[var(--color-muted)] py-2">
								none registered on this deployment
							</div>
						{:else}
							<div
								class="flex items-center gap-3 pb-1 text-[10px] uppercase tracking-wider text-[var(--color-muted)]"
							>
								<span class="w-[17rem] shrink-0">in report</span>
								<span class="w-56 shrink-0">signal</span>
								<span class="grow"></span>
								<button
									type="button"
									onclick={() => setAllShown(grp.kind, true)}
									class="underline decoration-dotted hover:text-[var(--color-bright)]"
								>
									all
								</button>
								<span class="text-[var(--color-faint)]">/</span>
								<button
									type="button"
									onclick={() => setAllShown(grp.kind, false)}
									class="underline decoration-dotted hover:text-[var(--color-bright)]"
								>
									none
								</button>
							</div>

							{#each rowsOf(grp.kind) as e (e.kind + e.key)}
								{@const gap = tierGap(e)}
								{@const empty = wouldBeEmpty(e)}
								<div
									class="flex items-center gap-3 py-1 border-t border-[var(--color-border)]/40 text-[12px]"
								>
									<label
										class="flex items-center gap-2 w-[17rem] min-w-0 shrink-0 cursor-pointer"
										class:opacity-45={!shown(e.kind, e.key)}
									>
										<input
											type="checkbox"
											checked={shown(e.kind, e.key)}
											onchange={(ev) =>
												setSubject(e.kind, e.key, { show: ev.currentTarget.checked })}
										/>
										<span class="num text-[var(--color-default)]">{e.key}</span>
										{#if e.name}
											<span class="text-[var(--color-muted)] truncate" title={e.name}>
												{e.name}
											</span>
										{/if}
									</label>
									<select
										value={entryOf(e.kind, e.key).source ?? ''}
										disabled={!shown(e.kind, e.key)}
										aria-label={`signal for ${e.key}`}
										onchange={(ev) =>
											setSubject(e.kind, e.key, {
												source: ev.currentTarget.value as Source | ''
											})}
										class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-[2px] text-[12px] w-56 shrink-0 disabled:opacity-40"
									>
										<option value="">
											default ({SOURCE_LABEL[v.default_source[e.kind] ?? 'frontend']})
										</option>
										<option value="frontend" disabled={!e.frontend}>radarca</option>
										<option value="backend" disabled={!e.backend}>backend files</option>
										<option value="combined" disabled={!(e.frontend && e.backend)}>
											worse of both
										</option>
									</select>
									{#if !shown(e.kind, e.key)}
										<span class="text-[11px] text-[var(--color-muted)]">not in the report</span>
									{:else if empty}
										<span
											class="text-[11px] text-[var(--color-warn)] truncate"
											title="This row would be built from a tier that is not checked on this deployment, so it would have no data at all."
										>
											{empty}
										</span>
									{:else if gap}
										<span class="text-[11px] text-[var(--color-faint)]">{gap}</span>
									{/if}
								</div>
							{/each}
						{/if}
					</div>
				</details>
			{/each}
		</div>

		<div class="flex items-center gap-2 pt-4">
			<button
				onclick={save}
				disabled={saving}
				class="border px-3 py-1.5 text-[12px] hover:bg-[var(--color-elevated)] disabled:opacity-50 {dirty
					? 'border-[var(--color-ok)]/60 text-[var(--color-bright)]'
					: 'border-[var(--color-border-strong)]'}"
			>
				{saving ? 'saving…' : 'Save'}
			</button>
			<button
				onclick={loadPreview}
				disabled={previewing}
				class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[12px] hover:bg-[var(--color-elevated)] disabled:opacity-50"
			>
				{previewing ? 'rendering…' : 'Preview'}
			</button>
			<!-- The per-subject lists collapse, so a change made inside one has
			     to stay visible from out here or it gets saved by luck. -->
			{#if dirty}
				<span class="text-[11px] text-[var(--color-warn)]">unsaved changes</span>
			{/if}
			<span class="grow"></span>
			<span class="text-[11px] text-[var(--color-muted)]">
				Save applies to the daily report only — the gate below saves separately.
			</span>
		</div>

		<div class="mt-6 border-t border-[var(--color-border)] pt-4">
			<div class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] mb-1">
				Alarm notifications by tier
			</div>
			<div class="text-[11px] text-[var(--color-muted)] mb-3 max-w-2xl leading-[1.55]">
				Turning a tier off stops it <span class="text-[var(--color-default)]">notifying</span>
				and nothing else. Its alarms still open, still carry their severity, still appear on
				the timeline and still count in this report, and every withheld message is recorded
				against its alarm — so afterwards you can see what Sentinel decided not to tell you.
				Re-enabling does not replay what it missed; the tier is audible again from the next
				repeat interval onward.
			</div>
			<div class="flex items-center gap-5 text-[12px] mb-3">
				<label class="flex items-center gap-2 text-[var(--color-default)]">
					<input type="checkbox" bind:checked={tiers.frontend} />
					radarca <span class="text-[var(--color-muted)]">(L0–L4)</span>
				</label>
				<label class="flex items-center gap-2 text-[var(--color-default)]">
					<input type="checkbox" bind:checked={tiers.backend} />
					backend files <span class="text-[var(--color-muted)]">(LB1–LB3)</span>
				</label>
				<button
					onclick={saveTiers}
					disabled={savingTiers}
					class="border border-[var(--color-border-strong)] px-3 py-1 text-[12px] hover:bg-[var(--color-elevated)] disabled:opacity-50"
				>
					{savingTiers ? 'saving…' : 'Save gate'}
				</button>
			</div>
			{#if !tiers.frontend || !tiers.backend}
				<div class="text-[11px] text-[var(--color-warn)] mb-1">
					{!tiers.frontend && !tiers.backend
						? 'Both tiers are silent — no alarm will notify anyone.'
						: `The ${!tiers.frontend ? 'radarca' : 'backend'} tier is silent.`}
				</div>
			{/if}
		</div>

		<div class="mt-6 border-t border-[var(--color-border)] pt-4">
			<div class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] mb-2">
				Send a test copy
			</div>
			<div class="flex items-center gap-2">
				<input
					bind:value={testTo}
					placeholder="you@example.com"
					class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1.5 text-[12px] num w-72"
				/>
				<button
					onclick={sendTest}
					disabled={testing || !testTo}
					class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[12px] hover:bg-[var(--color-elevated)] disabled:opacity-50"
				>
					{testing ? 'sending…' : 'Send test'}
				</button>
			</div>
			<div class="text-[11px] text-[var(--color-muted)] pt-2">
				Goes to this one address only, subject prefixed <span class="num">[TEST]</span>.
				Works whether or not the daily send is enabled.
			</div>
		</div>

		{#if preview}
			<div class="mt-6 border-t border-[var(--color-border)] pt-4">
				<div class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] mb-2">
					Preview — subject
				</div>
				<div class="text-[12px] text-[var(--color-bright)] num mb-3">{preview.subject}</div>
				<div class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] mb-2">
					Plain-text part
				</div>
				<pre class="overflow-x-auto border border-[var(--color-border)] bg-[var(--color-canvas)] p-3 text-[11px] leading-[1.5] text-[var(--color-default)] num">{preview.text}</pre>
			</div>
		{/if}
	{/if}
</div>

<style>
	/* Chrome drops ::marker on a flex <summary>, so the caret is drawn by hand
	   and turned here rather than by a class toggle in script — the element's
	   own open state is the only source of truth that cannot drift. */
	summary {
		list-style: none;
	}
	summary::-webkit-details-marker {
		display: none;
	}
	.chev {
		display: inline-block;
		transition: transform 120ms ease;
	}
	details[open] > summary .chev {
		transform: rotate(90deg);
	}
</style>
