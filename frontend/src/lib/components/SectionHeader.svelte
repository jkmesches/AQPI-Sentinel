<script lang="ts">
	let {
		title,
		count = '',
		right = ''
	}: { title: string; count?: string | number; right?: string } = $props();
</script>

<header class="flex items-center gap-3 border-b border-[var(--color-border)] px-3 py-1.5">
	<span class="label">{title}</span>
	{#if count !== ''}
		<span class="num text-[10.5px] text-[var(--color-faint)]">·  {count}</span>
	{/if}
	{#if right}
		<span class="ml-auto text-[10.5px] text-[var(--color-muted)] num">{right}</span>
	{/if}
</header>
