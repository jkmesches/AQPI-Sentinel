<script lang="ts">
	import { auth } from '$lib/stores/auth.svelte';
	import NavLink from '$lib/components/NavLink.svelte';

	let { children }: { children: any } = $props();
</script>

{#if auth.loading}
	<div class="flex h-full items-center justify-center text-[12px] text-[var(--color-muted)]">
		checking authentication…
	</div>
{:else if !auth.isAdmin}
	<div class="flex h-full items-center justify-center">
		<div class="border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-6 py-4 text-center">
			<div class="text-[12px] uppercase tracking-wider text-[var(--color-fail)] mb-1">admin only</div>
			<div class="text-[12px] text-[var(--color-default)]">
				{#if auth.user}
					Signed in as {auth.user.email} ({auth.user.role}), but this page requires admin role.
				{:else}
					<a class="underline" href="/login?next={encodeURIComponent('/admin/email')}">sign in</a>
					to access admin settings.
				{/if}
			</div>
		</div>
	</div>
{:else}
	<div class="flex h-full">
		<aside class="w-44 shrink-0 border-r border-[var(--color-border)] bg-[var(--color-canvas)]">
			<div class="px-3 py-3 text-[10px] uppercase tracking-[0.18em] text-[var(--color-faint)]">
				admin
			</div>
			<nav class="flex flex-col text-[12px]">
				<a
					class="px-3 py-2 hover:bg-[var(--color-elevated)] text-[var(--color-default)] hover:text-[var(--color-bright)]"
					href="/admin/email"
				>
					Email / SMTP
				</a>
				<a
					class="px-3 py-2 hover:bg-[var(--color-elevated)] text-[var(--color-default)] hover:text-[var(--color-bright)]"
					href="/admin/silences"
				>
					Silences
				</a>
				<a
					class="px-3 py-2 hover:bg-[var(--color-elevated)] text-[var(--color-default)] hover:text-[var(--color-bright)]"
					href="/admin/users"
				>
					Users
				</a>
				<a
					class="px-3 py-2 hover:bg-[var(--color-elevated)] text-[var(--color-default)] hover:text-[var(--color-bright)]"
					href="/admin/alerts"
				>
					Alert routing
				</a>
				<a
					class="px-3 py-2 hover:bg-[var(--color-elevated)] text-[var(--color-default)] hover:text-[var(--color-bright)]"
					href="/admin/audit"
				>
					Audit log
				</a>
			</nav>
		</aside>
		<main class="flex-1 overflow-auto">
			{@render children?.()}
		</main>
	</div>
{/if}
