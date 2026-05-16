<script lang="ts">
	import { statusDotBg } from '$lib/format';
	import { diag } from '$lib/diag';
	let {
		status = 'pass',
		size = 8,
		pulseKey = 0
	}: { status?: string; size?: number; pulseKey?: number } = $props();
</script>

{#if diag.pulse}
	{#key pulseKey}
		<span
			class="inline-block rounded-full {statusDotBg(status)} {pulseKey > 0 ? 'pulse' : ''}"
			style="width:{size}px; height:{size}px"
			aria-label={status}
		></span>
	{/key}
{:else}
	<span
		class="inline-block rounded-full {statusDotBg(status)}"
		style="width:{size}px; height:{size}px"
		aria-label={status}
	></span>
{/if}
