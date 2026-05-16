<script lang="ts">
	import { onMount } from 'svelte';
	import YAML from 'yaml';

	let text = $state('');
	let source = $state<'db' | 'yaml' | ''>('');
	let updatedAt = $state<string | null>(null);
	let updatedBy = $state<string | null>(null);
	let loading = $state(true);
	let saving = $state(false);
	let banner = $state<{ kind: 'ok' | 'err'; text: string } | null>(null);
	let parseErr = $state<string | null>(null);

	async function load() {
		loading = true;
		banner = null;
		try {
			const r = await fetch('/api/admin/alerts');
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			const j = await r.json();
			text = YAML.stringify(j.value ?? {}, { indent: 2 });
			source = j.source;
			updatedAt = j.updated_at;
			updatedBy = j.updated_by;
		} catch (e) {
			banner = { kind: 'err', text: `Load failed: ${(e as Error).message}` };
		} finally {
			loading = false;
		}
	}

	function validate(): unknown | null {
		try {
			const parsed = YAML.parse(text);
			parseErr = null;
			return parsed;
		} catch (e) {
			parseErr = (e as Error).message;
			return null;
		}
	}

	async function save() {
		const value = validate();
		if (value === null) return;
		saving = true;
		banner = null;
		try {
			const r = await fetch('/api/admin/alerts', {
				method: 'PUT',
				headers: { 'content-type': 'application/json' },
				
				body: JSON.stringify({ value })
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

	$effect(() => {
		// reactive validation as user types
		void text;
		validate();
	});

	onMount(load);
</script>

<div class="p-6 max-w-4xl">
	<div class="label tracking-[0.18em] text-[var(--color-bright)] mb-1">ALERT ROUTING</div>
	<div class="text-[11px] text-[var(--color-muted)] mb-4 leading-relaxed">
		Receivers, escalation policies, and routes. Saved to DB; the running alarm engine
		hot-reloads on save (no restart needed). Bootstrapped from <code>alerts.yaml</code> on
		first load if no DB row exists yet.
	</div>

	{#if loading}
		<div class="text-[12px] text-[var(--color-muted)]">loading…</div>
	{:else}
		<div class="flex items-center gap-3 text-[10px] text-[var(--color-muted)] mb-2 uppercase tracking-wider">
			<span>source: <span class="num text-[var(--color-bright)]">{source}</span></span>
			{#if updatedAt}
				<span>· saved {updatedAt.slice(0, 19).replace('T', ' ')} by {updatedBy ?? '?'}</span>
			{/if}
		</div>

		<textarea
			bind:value={text}
			spellcheck="false"
			class="w-full h-[60vh] border {parseErr ? 'border-[var(--color-fail)]/60' : 'border-[var(--color-border-strong)]'} bg-[var(--color-canvas)] text-[var(--color-bright)] p-3 text-[12px] num font-mono leading-snug focus:outline-none"
		></textarea>

		{#if parseErr}
			<div class="mt-2 border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-3 py-2 text-[11px] text-[var(--color-fail)] num whitespace-pre-wrap">
				YAML parse: {parseErr}
			</div>
		{/if}

		{#if banner}
			<div
				class="mt-3 border px-3 py-2 text-[12px] {banner.kind === 'ok'
					? 'border-[var(--color-ok)]/40 bg-[var(--color-ok)]/10 text-[var(--color-ok)]'
					: 'border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 text-[var(--color-fail)]'}"
			>
				{banner.text}
			</div>
		{/if}

		<div class="mt-4 flex items-center gap-3">
			<button
				class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-elevated)] disabled:opacity-50"
				onclick={save}
				disabled={saving || !!parseErr}
			>
				{saving ? 'saving…' : 'save + reload'}
			</button>
			<button
				class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[11px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)]"
				onclick={load}
			>
				reload from server
			</button>
		</div>

		<details class="mt-6 text-[11px]">
			<summary class="cursor-pointer text-[var(--color-faint)] uppercase tracking-wider text-[10px]">schema cheat-sheet</summary>
			<pre class="mt-2 text-[11px] num text-[var(--color-muted)] whitespace-pre-wrap leading-relaxed">
receivers:
  - name: console
    console: true
  - name: oncall-primary
    email: ["you@example.com"]
    template: default
  - name: chat
    webhook: https://discord.com/...

escalation_policies:
  - name: standard
    steps:
      - delay: 0m
        receivers: [console]
      - delay: 15m
        receivers: [oncall-primary]

routes:
  - match: {{ stage: L0 }}
    severity_floor: critical
    policy: standard
    repeat_interval: 10m
  - match: {{}}
    policy: standard
    repeat_interval: 1h
			</pre>
		</details>
	{/if}
</div>
