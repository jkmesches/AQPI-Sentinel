<script lang="ts">
	import { onMount } from 'svelte';
	import { auth } from '$lib/stores/auth.svelte';

	interface GroupRef { id: number; name: string; }
	interface UserRow {
		id: number;
		email: string;
		role: 'admin' | 'user';
		display_name: string | null;
		created_at: string | null;
		last_login_at: string | null;
		disabled_at: string | null;
		groups?: GroupRef[];
	}

	let rows = $state<UserRow[]>([]);
	let allGroups = $state<GroupRef[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	let newEmail = $state('');
	let newRole = $state<'admin' | 'user'>('user');
	let newDisplay = $state('');
	let newSendEmail = $state(true);
	let newGroupIds = $state<number[]>([]);
	let busy = $state(false);

	// Per-row inline group editor state. Keyed by user id.
	let editingGroupsFor = $state<number | null>(null);
	let groupDraft = $state<number[]>([]);

	function toggleNewGroup(gid: number) {
		newGroupIds = newGroupIds.includes(gid)
			? newGroupIds.filter((x) => x !== gid)
			: [...newGroupIds, gid];
	}
	function toggleDraftGroup(gid: number) {
		groupDraft = groupDraft.includes(gid)
			? groupDraft.filter((x) => x !== gid)
			: [...groupDraft, gid];
	}
	function startEditGroups(u: UserRow) {
		editingGroupsFor = u.id;
		groupDraft = (u.groups ?? []).map((g) => g.id);
	}
	async function saveGroupsFor(u: UserRow) {
		try {
			const r = await fetch(`/api/admin/users/${u.id}`, {
				method: 'PATCH',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ group_ids: groupDraft })
			});
			if (!r.ok) throw new Error(`HTTP ${r.status}`);
			editingGroupsFor = null;
			await load();
		} catch (e) {
			alert(`update groups failed: ${(e as Error).message}`);
		}
	}
	let lastResult = $state<{
		email: string;
		link: string;
		emailStatus: 'sent' | 'failed' | 'no_smtp' | null;
		emailError: string | null;
	} | null>(null);

	async function load() {
		loading = true;
		error = null;
		try {
			const [ur, gr] = await Promise.all([
				fetch('/api/admin/users'),
				fetch('/api/admin/groups')
			]);
			if (!ur.ok) throw new Error(`users HTTP ${ur.status}`);
			rows = await ur.json();
			if (gr.ok) {
				const groups = await gr.json();
				allGroups = groups.map((g: any) => ({ id: g.id, name: g.name }));
			}
		} catch (e) {
			error = (e as Error).message;
		} finally {
			loading = false;
		}
	}

	async function invite(e: Event) {
		e.preventDefault();
		busy = true;
		lastResult = null;
		const targetEmail = newEmail.trim();
		try {
			const r = await fetch('/api/admin/users', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({
					email: targetEmail,
					role: newRole,
					display_name: newDisplay || null,
					send_email: newSendEmail,
					group_ids: newGroupIds,
				})
			});
			const j = await r.json();
			if (!r.ok) throw new Error(j?.detail ?? `HTTP ${r.status}`);
			lastResult = {
				email: targetEmail,
				link: `${location.origin}${j.reset_url}`,
				emailStatus: j.email_status ?? null,
				emailError: j.email_error ?? null,
			};
			newEmail = '';
			newDisplay = '';
			newRole = 'user';
			newGroupIds = [];
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
		const sendEmail = confirm(
			`Issue a password-reset link for ${u.email}?\n\n` +
			`OK = email the link to them (if SMTP is configured)\n` +
			`Cancel = just generate the link, don't email`
		);
		// confirm() returns true/false — but "Cancel" should still issue the
		// link; we use it only to choose whether to email it.
		try {
			const r = await fetch(`/api/admin/users/${u.id}/reset`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ send_email: sendEmail }),
			});
			const j = await r.json();
			if (!r.ok) throw new Error(j?.detail ?? `HTTP ${r.status}`);
			lastResult = {
				email: u.email,
				link: `${location.origin}${j.reset_url}`,
				emailStatus: j.email_status ?? null,
				emailError: j.email_error ?? null,
			};
		} catch (e) {
			alert(`reset failed: ${(e as Error).message}`);
		}
	}

	async function del(u: UserRow) {
		if (!confirm(`Delete user ${u.email}? Cannot be undone.`)) return;
		try {
			const r = await fetch(`/api/admin/users/${u.id}`, {
				method: 'DELETE'
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

	{#if lastResult}
		{@const r = lastResult}
		<div class="mb-4 border {r.emailStatus === 'sent'
			? 'border-[var(--color-ok)]/40 bg-[var(--color-ok)]/10'
			: r.emailStatus === 'failed'
				? 'border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10'
				: 'border-[var(--color-warn)]/40 bg-[var(--color-warn)]/10'} px-3 py-2 text-[12px]">
			{#if r.emailStatus === 'sent'}
				<div class="text-[var(--color-ok)] mb-1 uppercase tracking-wider text-[10px]">invite email sent to {r.email}</div>
				<div class="text-[10px] text-[var(--color-muted)]">Link below is a backup in case they don't receive the email. Expires in 72h.</div>
			{:else if r.emailStatus === 'failed'}
				<div class="text-[var(--color-fail)] mb-1 uppercase tracking-wider text-[10px]">email send failed — copy the link instead</div>
				<div class="text-[10px] text-[var(--color-muted)] mb-1">{r.emailError ?? 'unknown error'}</div>
			{:else if r.emailStatus === 'no_smtp'}
				<div class="text-[var(--color-warn)] mb-1 uppercase tracking-wider text-[10px]">SMTP not configured — share this link manually</div>
				<div class="text-[10px] text-[var(--color-muted)] mb-1">Set it up at <a href="/admin/email" class="underline">Email / SMTP</a> so future invites can be emailed automatically.</div>
			{:else}
				<div class="text-[var(--color-bright)] mb-1 uppercase tracking-wider text-[10px]">share this link with {r.email}</div>
			{/if}
			<div class="num text-[var(--color-bright)] break-all">{r.link}</div>
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
					<th class="px-2 py-1 text-left">groups</th>
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
						<td class="px-2 py-1 text-[11px]">
							{#if editingGroupsFor === u.id}
								<div class="flex flex-wrap gap-1 items-center">
									{#each allGroups as g}
										{@const checked = groupDraft.includes(g.id)}
										<button
											type="button"
											onclick={() => toggleDraftGroup(g.id)}
											class="rounded-sm border px-1.5 py-0.5 text-[10px] num {checked
												? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
												: 'border-[var(--color-border-strong)] text-[var(--color-muted)] hover:bg-[var(--color-elevated)]/40'}"
										>
											{g.name}
										</button>
									{/each}
									<button class="text-[10px] uppercase tracking-wider text-[var(--color-ok)] ml-1" onclick={() => saveGroupsFor(u)}>save</button>
									<button class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]" onclick={() => (editingGroupsFor = null)}>cancel</button>
								</div>
							{:else}
								<div class="flex flex-wrap gap-1 items-center">
									{#each u.groups ?? [] as g}
										<span class="rounded-sm border border-[var(--color-border-strong)] bg-[var(--color-elevated)]/40 px-1.5 py-0.5 text-[10px] num text-[var(--color-bright)]">{g.name}</span>
									{:else}
										<span class="text-[var(--color-faint)] italic">none</span>
									{/each}
									{#if allGroups.length > 0}
										<button class="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-bright)] underline" onclick={() => startEditGroups(u)}>edit</button>
									{/if}
								</div>
							{/if}
						</td>
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

			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">groups</label>
			{#if allGroups.length === 0}
				<span class="text-[11px] text-[var(--color-faint)] italic">No groups defined yet. Create some at <a href="/admin/groups" class="underline">/admin/groups</a> to assign new users to them.</span>
			{:else}
				<div class="flex flex-wrap gap-1">
					{#each allGroups as g}
						{@const checked = newGroupIds.includes(g.id)}
						<button
							type="button"
							onclick={() => toggleNewGroup(g.id)}
							class="rounded-sm border px-2 py-0.5 text-[11px] num {checked
								? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
								: 'border-[var(--color-border-strong)] text-[var(--color-muted)] hover:bg-[var(--color-elevated)]/40'}"
						>
							{g.name}
						</button>
					{/each}
				</div>
			{/if}

			<label class="text-[var(--color-muted)] uppercase tracking-wider text-[10px]">send email</label>
			<label class="flex items-center gap-2 text-[12px] text-[var(--color-default)]">
				<input type="checkbox" bind:checked={newSendEmail} />
				Email the reset link to the user (requires <a href="/admin/email" class="underline">SMTP</a>)
			</label>
		</div>
		<div class="mt-4">
			<button type="submit" disabled={busy}
				class="border border-[var(--color-border-strong)] px-3 py-1.5 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-elevated)] disabled:opacity-50">
				{busy ? 'inviting…' : (newSendEmail ? 'invite + send email' : 'invite + generate link')}
			</button>
		</div>
	</form>
</div>
