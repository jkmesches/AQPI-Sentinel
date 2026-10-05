<!--
  One target, every reading of it, collapsed to the worst.

  Collapsed shows the worst status and that reading's own summary — not a
  synthesised one. An operator reading "STALE CONTRIBUTION — 20.0 min old" is
  reading the words the check itself wrote, so expanding never contradicts
  what the collapsed row said.

  The chevron is a real <button> with aria-expanded rather than a styled div:
  these rails are read with a keyboard during an incident, and a disclosure
  that cannot be tabbed to is a disclosure that cannot be opened.
-->
<script lang="ts">
	import StatusDot from './StatusDot.svelte';
	import { statusText } from '$lib/format';
	import type { Snippet } from 'svelte';

	// `open` is a plain prop with an `ontoggle` callback rather than $bindable:
	// the parent owns the open set so that Expand All / Collapse All can drive
	// every row at once, and so that a row which becomes alerting can open
	// itself without overwriting a collapse the operator chose deliberately.
	let {
		label,
		worst,
		open = false,
		ontoggle,
		count = 0,
		pulseKey = 0,
		title = '',
		collapsed,
		detail,
		trailing
	}: {
		label: string;
		worst: string;
		open?: boolean;
		ontoggle?: () => void;
		count?: number;
		pulseKey?: number;
		title?: string;
		collapsed?: Snippet;
		detail?: Snippet;
		trailing?: Snippet;
	} = $props();

	const alerting = $derived(worst !== 'pass' && worst !== 'skip');
</script>

<li class="row-hover {alerting ? 'row-bad' : ''}">
	<div class="flex items-center gap-2 px-3 py-2 text-[12px]">
		<button
			type="button"
			class="flex shrink-0 items-center gap-1.5 text-left"
			aria-expanded={open}
			title={title || label}
			onclick={() => ontoggle?.()}
		>
			<!-- Rotated rather than swapped for a different glyph, so the arrow
			     animates to its new state and the row does not reflow by a pixel
			     when two characters have different widths. -->
			<span
				class="inline-block w-[0.6rem] shrink-0 text-[9px] text-[var(--color-muted)] transition-transform duration-150"
				style:transform={open ? 'rotate(90deg)' : 'rotate(0deg)'}
				aria-hidden="true">▶</span
			>
			<StatusDot status={worst} size={7} {pulseKey} />
			<span class="num w-[3.6rem] truncate tracking-wide text-[13px] text-[var(--color-bright)]"
				>{label}</span
			>
		</button>
		<!-- min-w-0 so a long summary truncates inside the row instead of
		     pushing the trailing cell off the rail. -->
		<span class="min-w-0 flex-1 truncate text-[10.5px] num {statusText(worst)}">
			{#if collapsed}{@render collapsed()}{/if}
		</span>
		{#if count > 1}
			<span class="shrink-0 num text-[9.5px] text-[var(--color-faint)]" title="{count} checks">
				{count}
			</span>
		{/if}
		{#if trailing}
			<span class="shrink-0">{@render trailing()}</span>
		{/if}
	</div>
	{#if open && detail}
		<div class="border-t border-[var(--color-border)]/60 bg-[var(--color-fail)]/[0.02] px-3 pb-2 pt-1.5">
			{@render detail()}
		</div>
	{/if}
</li>
