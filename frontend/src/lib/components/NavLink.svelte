<script lang="ts">
	import { page } from '$app/state';
	let { href, label }: { href: string; label: string } = $props();
	const active = $derived(page.url.pathname === href);
</script>

<a
	{href}
	class="px-2 py-1 text-[11px] uppercase tracking-wider {active
		? 'text-[var(--color-bright)] border-b border-[var(--color-bright)]'
		: 'text-[var(--color-muted)] hover:text-[var(--color-default)]'}"
>
	{label}
</a>
