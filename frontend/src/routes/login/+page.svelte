<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { auth } from '$lib/stores/auth.svelte';

	let email = $state('');
	let password = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);

	async function submit(e: Event) {
		e.preventDefault();
		if (busy) return;
		busy = true;
		error = null;
		const r = await auth.login(email.trim(), password);
		busy = false;
		if (r.ok) {
			const next = new URL(page.url).searchParams.get('next') || '/';
			goto(next);
		} else {
			error = r.error;
		}
	}
</script>

<div class="flex h-full items-center justify-center bg-[var(--color-canvas)]">
	<form
		class="w-[22rem] border border-[var(--color-border-strong)] bg-[var(--color-surface)] p-6 shadow-xl"
		onsubmit={submit}
	>
		<div class="mb-4">
			<div class="text-[14px] font-semibold tracking-[0.22em] text-[var(--color-accent)]">AQPI SENTINEL</div>
			<div class="text-[10.5px] uppercase tracking-wider text-[var(--color-muted)] mt-1">sign in</div>
		</div>

		<label class="block mb-3">
			<span class="block text-[10px] uppercase tracking-wider text-[var(--color-muted)] mb-1">email</span>
			<input
				type="email"
				bind:value={email}
				autocomplete="email"
				required
				class="w-full border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1.5 text-[12px] text-[var(--color-bright)] num focus:outline-none focus:border-[var(--color-ok)]"
			/>
		</label>

		<label class="block mb-4">
			<span class="block text-[10px] uppercase tracking-wider text-[var(--color-muted)] mb-1">password</span>
			<input
				type="password"
				bind:value={password}
				autocomplete="current-password"
				required
				class="w-full border border-[var(--color-border-strong)] bg-[var(--color-canvas)] px-2 py-1.5 text-[12px] text-[var(--color-bright)] num focus:outline-none focus:border-[var(--color-ok)]"
			/>
		</label>

		{#if error}
			<div class="mb-3 border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-2 py-1 text-[11px] text-[var(--color-fail)]">
				{error}
			</div>
		{/if}

		<button
			type="submit"
			disabled={busy}
			class="w-full border border-[var(--color-border-strong)] px-3 py-2 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-elevated)] disabled:opacity-50"
		>
			{busy ? 'signing in…' : 'sign in'}
		</button>
	</form>
</div>
