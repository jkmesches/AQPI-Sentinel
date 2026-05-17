<script lang="ts">
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';

	let token = $derived(page.params.token);
	let loading = $state(true);
	let email = $state<string | null>(null);
	let error = $state<string | null>(null);
	let password = $state('');
	let confirm = $state('');
	let busy = $state(false);
	let done = $state(false);

	async function check() {
		loading = true;
		try {
			const r = await fetch(`/api/auth/reset/${encodeURIComponent(token)}`);
			const j = await r.json();
			if (!r.ok) throw new Error(j?.detail ?? `HTTP ${r.status}`);
			email = j.email;
		} catch (e) {
			error = (e as Error).message;
		} finally {
			loading = false;
		}
	}

	async function submit(e: Event) {
		e.preventDefault();
		if (password !== confirm) { error = 'passwords do not match'; return; }
		if (password.length < 8) { error = 'password must be at least 8 characters'; return; }
		busy = true; error = null;
		try {
			const r = await fetch(`/api/auth/reset/${encodeURIComponent(token)}`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ password })
			});
			const j = await r.json();
			if (!r.ok) throw new Error(j?.detail ?? `HTTP ${r.status}`);
			done = true;
			setTimeout(() => goto('/login'), 1200);
		} catch (e) {
			error = (e as Error).message;
		} finally {
			busy = false;
		}
	}

	onMount(check);
</script>

<div class="flex h-full items-center justify-center bg-[var(--color-canvas)]">
	<div class="w-[22rem] border border-[var(--color-border-strong)] bg-[var(--color-surface)] p-6 shadow-xl">
		<div class="text-[14px] font-semibold tracking-[0.22em] text-[var(--color-accent)] mb-1">AQPI SENTINEL</div>
		<div class="text-[10.5px] uppercase tracking-wider text-[var(--color-muted)] mb-4">set password</div>

		{#if loading}
			<div class="text-[12px] text-[var(--color-muted)]">checking link…</div>
		{:else if error && !email}
			<div class="text-[12px] text-[var(--color-fail)]">{error}</div>
		{:else if done}
			<div class="text-[12px] text-[var(--color-ok)]">Password set. Redirecting to sign-in…</div>
		{:else}
			<form onsubmit={submit}>
				<div class="text-[11px] text-[var(--color-default)] mb-3">
					Setting password for <span class="num text-[var(--color-bright)]">{email}</span>
				</div>
				<label class="block mb-3">
					<span class="block text-[10px] uppercase tracking-wider text-[var(--color-muted)] mb-1">new password</span>
					<input type="password" bind:value={password} required minlength="8"
						class="w-full border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1.5 text-[12px] text-[var(--color-bright)] num focus:outline-none focus:border-[var(--color-ok)]" />
				</label>
				<label class="block mb-4">
					<span class="block text-[10px] uppercase tracking-wider text-[var(--color-muted)] mb-1">confirm</span>
					<input type="password" bind:value={confirm} required
						class="w-full border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1.5 text-[12px] text-[var(--color-bright)] num focus:outline-none focus:border-[var(--color-ok)]" />
				</label>
				{#if error}
					<div class="mb-3 border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-2 py-1 text-[11px] text-[var(--color-fail)]">{error}</div>
				{/if}
				<button type="submit" disabled={busy}
					class="w-full border border-[var(--color-border-strong)] px-3 py-2 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-elevated)] disabled:opacity-50">
					{busy ? 'setting…' : 'set password'}
				</button>
			</form>
		{/if}
	</div>
</div>
