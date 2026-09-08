<script lang="ts">
	import { onMount } from 'svelte';
	import { auth } from '$lib/stores/auth.svelte';

	interface DigestConfig {
		enabled: boolean;
		recipients: string[];
		hour: number;
		tz: string;
		products: boolean;
	}

	let v = $state<DigestConfig>({
		enabled: false, recipients: [], hour: 7,
		tz: 'America/Denver', products: true
	});
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
			const body = { ...v, recipients: parseRecipients(), hour: Number(v.hour) };
			const r = await fetch('/api/admin/digest', {
				method: 'PUT',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify(body)
			});
			if (!r.ok) {
				const j = await r.json().catch(() => ({}));
				throw new Error(j.detail ?? `HTTP ${r.status}`);
			}
			v = { ...v, ...(await r.json()) };
			recipientsText = (v.recipients ?? []).join('\n');
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
					placeholder="chandra@colostate.edu"
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

		<div class="flex items-center gap-2 pt-4">
			<button
				onclick={save}
				disabled={saving}
				class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[12px] hover:bg-[var(--color-elevated)] disabled:opacity-50"
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
