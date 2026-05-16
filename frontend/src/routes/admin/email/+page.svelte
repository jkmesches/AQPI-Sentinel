<script lang="ts">
	import { onMount } from 'svelte';
	import { auth } from '$lib/stores/auth.svelte';

	interface SmtpValue {
		host: string;
		port: number | string;
		username: string;
		password: string;       // mask sentinel `•••••` round-trips unchanged
		from_addr: string;
		use_tls: boolean;
		use_starttls: boolean;
	}

	let v = $state<SmtpValue>({
		host: '', port: 587, username: '', password: '',
		from_addr: '', use_tls: false, use_starttls: true
	});
	let updatedAt = $state<string | null>(null);
	let updatedBy = $state<string | null>(null);
	let loading = $state(true);
	let saving = $state(false);
	let testing = $state(false);
	let testTo = $state('');
	let banner = $state<{ kind: 'ok' | 'err'; text: string } | null>(null);

	async function load() {
		loading = true;
		banner = null;
		try {
			const r = await fetch('/api/admin/settings/smtp', { credentials: 'include' });
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			const j = await r.json();
			if (j.value) {
				v = { ...v, ...j.value };
			}
			updatedAt = j.updated_at;
			updatedBy = j.updated_by;
			testTo = auth.user?.email ?? '';
		} catch (e) {
			banner = { kind: 'err', text: `Load failed: ${(e as Error).message}` };
		} finally {
			loading = false;
		}
	}

	async function save() {
		saving = true;
		banner = null;
		try {
			const r = await fetch('/api/admin/settings/smtp', {
				method: 'PUT',
				headers: { 'content-type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ value: { ...v, port: Number(v.port) } })
			});
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			banner = { kind: 'ok', text: 'Saved.' };
			await load();
		} catch (e) {
			banner = { kind: 'err', text: `Save failed: ${(e as Error).message}` };
		} finally {
			saving = false;
		}
	}

	async function testSend() {
		testing = true;
		banner = null;
		try {
			const r = await fetch('/api/admin/email/test', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ to: testTo })
			});
			const j = await r.json();
			if (!r.ok) throw new Error(j?.detail ?? `HTTP ${r.status}`);
			banner = { kind: 'ok', text: `Sent to ${j.to} in ${j.ms} ms` };
		} catch (e) {
			banner = { kind: 'err', text: `Test failed: ${(e as Error).message}` };
		} finally {
			testing = false;
		}
	}

	onMount(load);
</script>

<div class="p-6 max-w-2xl">
	<div class="label tracking-[0.18em] text-[var(--color-bright)] mb-1">EMAIL / SMTP</div>
	<div class="text-[11px] text-[var(--color-muted)] mb-6">
		Outbound mail config. Used by the alarm engine to send notifications when alerts fire.
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
		<div class="grid grid-cols-[10rem_1fr] gap-x-4 gap-y-3 text-[12px] items-center">
			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">SMTP host</label>
			<input bind:value={v.host} class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1.5 text-[12px] num" placeholder="smtp.gmail.com" />

			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">port</label>
			<input bind:value={v.port} type="number" class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1.5 text-[12px] num w-24" />

			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">username</label>
			<input bind:value={v.username} class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1.5 text-[12px] num" autocomplete="off" />

			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">password</label>
			<input bind:value={v.password} type="password" class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1.5 text-[12px] num" autocomplete="off" placeholder={updatedAt ? '(unchanged)' : ''} />

			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">from address</label>
			<input bind:value={v.from_addr} class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1.5 text-[12px] num" placeholder="sentinel@example.com" />

			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">encryption</label>
			<div class="flex items-center gap-4 text-[12px] text-[var(--color-default)]">
				<label class="flex items-center gap-2">
					<input type="radio" name="enc" checked={v.use_starttls && !v.use_tls} onchange={() => { v.use_starttls = true; v.use_tls = false; }} />
					STARTTLS (587)
				</label>
				<label class="flex items-center gap-2">
					<input type="radio" name="enc" checked={v.use_tls && !v.use_starttls} onchange={() => { v.use_starttls = false; v.use_tls = true; }} />
					Implicit TLS (465)
				</label>
				<label class="flex items-center gap-2">
					<input type="radio" name="enc" checked={!v.use_tls && !v.use_starttls} onchange={() => { v.use_starttls = false; v.use_tls = false; }} />
					None
				</label>
			</div>
		</div>

		<div class="mt-6 flex items-center gap-3">
			<button
				class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-elevated)] disabled:opacity-50"
				onclick={save}
				disabled={saving}
			>
				{saving ? 'saving…' : 'save'}
			</button>
			<span class="text-[10px] text-[var(--color-faint)] num">
				{#if updatedAt}last saved {updatedAt.slice(0, 19).replace('T', ' ')} by {updatedBy ?? '?'}{/if}
			</span>
		</div>

		<div class="mt-8 border-t border-[var(--color-border)] pt-6">
			<div class="label text-[var(--color-bright)] mb-1">SEND TEST EMAIL</div>
			<div class="text-[11px] text-[var(--color-muted)] mb-3">
				Uses the saved config (not the form values). Save first if you've changed something.
			</div>
			<div class="flex items-center gap-3">
				<input
					bind:value={testTo}
					placeholder="recipient@example.com"
					class="border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1.5 text-[12px] num w-72"
				/>
				<button
					class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[11px] uppercase tracking-wider text-[var(--color-default)] hover:bg-[var(--color-elevated)] hover:text-[var(--color-bright)] disabled:opacity-50"
					onclick={testSend}
					disabled={testing || !testTo}
				>
					{testing ? 'sending…' : 'send test'}
				</button>
			</div>
		</div>
	{/if}
</div>
