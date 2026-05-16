<script lang="ts">
	import { onMount } from 'svelte';
	import { auth } from '$lib/stores/auth.svelte';

	interface UserRow {
		id: number;
		email: string;
		role: 'admin' | 'user';
		display_name: string | null;
		created_at: string | null;
		last_login_at: string | null;
		disabled_at: string | null;
	}

	let rows = $state<UserRow[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	let newEmail = $state('');
	let newRole = $state<'admin' | 'user'>('user');
	let newDisplay = $state('');
	let busy = $state(false);
	let lastResetLink = $state<string | null>(null);

	async function load() {
		loading = true;
		error = null;
		try {
			const r = await fetch('/api/admin/users', { credentials: 'include' });
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			rows = await r.json();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			loading = false;
		}
	}

	async function invite(e: Event) {
		e.preventDefault();
		busy = true;
		lastResetLink = null;
		try {
			const r = await fetch('/api/admin/users', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ email: newEmail.trim(), role: newRole, display_name: newDisplay || null })
			});
			const j = await r.json();
			if (!r.ok) throw new Error(j?.detail ?? `HTTP ${r.status}`);
			lastResetLink = `${location.origin}${j.reset_url}`;
			newEmail = '';
			newDisplay = '';
			newRole = 'user';
			await load();
		} catch (e) {
			alert(`invite failed: ${(e as Error).message}`);
		} finally {
			busy = false;
		}
	}

	async function toggleDisabled(u: UserRow) {
		try {
			const r = await fetch(`/api/admin/users/${u.id}`, {
				method: 'PATCH',
				headers: { 'content-type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ disabled: !u.disabled_at })
			});
			const j = await r.json();
			if (!r.ok) throw new Error(j?.detail ?? `HTTP ${r.status}`);
			await load();
		} catch (e) {
			alert(`update failed: ${(e as Error).message}`);
		}
	}

	async function setRole(u: UserRow, role: 'admin' | 'user') {
		try {
			const r = await fetch(`/api/admin/users/${u.id}`, {
				method: 'PATCH',
				headers: { 'content-type': 'application/json' },
				credentials: 'include',
				body: JSON.stringify({ role })
			});
			const j = await r.json();
			if (!r.ok) throw new Error(j?.detail ?? `HTTP ${r.status}`);
			await load();
		} catch (e) {
			alert(`update failed: ${(e as Error).message}`);
		}
	}

	async function reissueReset(u: UserRow) {
		try {
			const r = await fetch(`/api/admin/users/${u.id}/reset`, {
				method: 'POST', credentials: 'include'
			});
			const j = await r.json();
			if (!r.ok) throw new Error(j?.detail ?? `HTTP ${r.status}`);
			lastResetLink = `${location.origin}${j.reset_url}`;
		} catch (e) {
			alert(`reset failed: ${(e as Error).message}`);
		}
	}

	async function del(u: UserRow) {
		if (!confirm(`Delete user ${u.email}? Cannot be undone.`)) return;
		try {
			const r = await fetch(`/api/admin/users/${u.id}`, {
				method: 'DELETE', credentials: 'include'
			});
			const j = await r.json();
			if (!r.ok) throw new Error(j?.detail ?? `HTTP ${r.status}`);
			await load();
		} catch (e) {
			alert(`delete failed: ${(e as Error).message}`);
		}
	}

	function fmt(iso: string | null): string {
		return iso ? iso.slice(0, 19).replace('T', ' ') : '—';
	}

	onMount(load);
</script>

<div class="p-6 max-w-5xl">
	<div class="label tracking-[0.18em] text-[var(--color-bright)] mb-1">USERS</div>
	<div class="text-[11px] text-[var(--color-muted)] mb-6">
		Manage who can sign in and ack alarms / change settings.
	</div>

	{#if error}
		<div class="mb-4 border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-3 py-2 text-[12px] text-[var(--color-fail)]">{error}</div>
	{/if}

	{#if lastResetLink}
		<div class="mb-4 border border-[var(--color-ok)]/40 bg-[var(--color-ok)]/10 px-3 py-2 text-[12px]">
			<div class="text-[var(--color-ok)] mb-1 uppercase tracking-wider text-[10px]">share this link with the user</div>
			<div class="num text-[var(--color-bright)] break-all">{lastResetLink}</div>
			<div class="text-[10px] text-[var(--color-muted)] mt-1">Expires in 72 hours. They'll set their password from there.</div>
		</div>
	{/if}

	{#if loading}
		<div class="text-[12px] text-[var(--color-muted)]">loading…</div>
	{:else}
		<table class="w-full text-[11px] mb-6">
			<thead class="text-[var(--color-muted)]">
				<tr class="border-b border-[var(--color-border)]">
					<th class="px-2 py-1 text-left">email</th>
					<th class="px-2 py-1 text-left">role</th>
					<th class="px-2 py-1 text-left">display name</th>
					<th class="px-2 py-1 text-left">last login</th>
					<th class="px-2 py-1 text-left">status</th>
					<th class="px-2 py-1"></th>
				</tr>
			</thead>
			<tbody>
				{#each rows as u}
					{@const me = u.id === auth.user?.id}
					<tr class="border-b border-[var(--color-border)] {u.disabled_at ? 'opacity-50' : ''}">
						<td class="px-2 py-1 num text-[var(--color-bright)]">
							{u.email}
							{#if me}<span class="ml-1 text-[9.5px] uppercase tracking-wider text-[var(--color-faint)]">(you)</span>{/if}
						</td>
						<td class="px-2 py-1">
							<select
								class="bg-[var(--color-canvas)] border border-[var(--color-border-strong)] text-[11px] px-1 py-0.5 num"
								value={u.role}
								onchange={(e) => setRole(u, (e.currentTarget as HTMLSelectElement).value as 'admin' | 'user')}
							>
								<option value="user">user</option>
								<option value="admin">admin</option>
							</select>
						</td>
						<td class="px-2 py-1 text-[var(--color-default)]">{u.display_name ?? '—'}</td>
						<td class="px-2 py-1 num text-[var(--color-muted)]">{fmt(u.last_login_at)}</td>
						<td class="px-2 py-1 text-[10px] uppercase tracking-wider">
							{u.disabled_at
								? `disabled · ${fmt(u.disabled_at)}`
								: 'active'}
						</td>
						<td class="px-2 py-1 text-right whitespace-nowrap">
							<button class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)] mr-2"
								onclick={() => reissueReset(u)}>reset pw</button>
							<button class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-warn)] mr-2"
								onclick={() => toggleDisabled(u)}>{u.disabled_at ? 'enable' : 'disable'}</button>
							{#if !me}
								<button class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-fail)]"
									onclick={() => del(u)}>delete</button>
							{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}

	<form class="border border-[var(--color-border)] bg-[var(--color-canvas)] p-4" onsubmit={invite}>
		<div class="label text-[var(--color-bright)] mb-3">INVITE USER</div>
		<div class="grid grid-cols-[7rem_1fr] gap-x-4 gap-y-3 text-[12px] items-center">
			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">email</label>
			<input bind:value={newEmail} type="email" required class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[12px] num" />

			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">role</label>
			<select bind:value={newRole} class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[12px] num w-32">
				<option value="user">user</option>
				<option value="admin">admin</option>
			</select>

			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">display name</label>
			<input bind:value={newDisplay} class="border border-[var(--color-border-strong)] bg-[var(--color-surface)] px-2 py-1 text-[12px]" placeholder="optional" />
		</div>
		<div class="mt-4">
			<button type="submit" disabled={busy}
				class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-elevated)] disabled:opacity-50">
				{busy ? 'inviting…' : 'invite + generate reset link'}
			</button>
			<span class="ml-3 text-[10px] text-[var(--color-faint)]">No email is sent; copy the link to the user yourself.</span>
		</div>
	</form>
</div>
