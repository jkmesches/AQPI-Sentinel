<script lang="ts">
	import { theme, type ThemeMode } from '$lib/stores/theme.svelte';

	const opts: { mode: ThemeMode; label: string; glyph: string }[] = [
		{ mode: 'light', label: 'Light', glyph: '○' },
		{ mode: 'auto', label: 'Auto', glyph: '◐' },
		{ mode: 'dark', label: 'Dark', glyph: '●' }
	];
</script>

<div
	class="flex items-center gap-0 rounded-sm border border-[var(--color-border)] text-[10.5px]"
	role="radiogroup"
	aria-label="theme"
>
	{#each opts as o}
		<button
			class="px-2 py-0.5 transition-colors {theme.mode === o.mode
				? 'bg-[var(--color-elevated)] text-[var(--color-bright)]'
				: 'text-[var(--color-muted)] hover:bg-[var(--color-elevated)]/60 hover:text-[var(--color-default)]'}"
			aria-checked={theme.mode === o.mode}
			role="radio"
			title={o.label}
			onclick={() => theme.set(o.mode)}
		>
			<span class="num">{o.glyph}</span>
			<span class="ml-1 label">{o.label}</span>
		</button>
	{/each}
</div>
