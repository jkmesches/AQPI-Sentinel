<script lang="ts">
	import { statusDotBg } from '$lib/format';
	let {
		status = 'pass',
		size = 8,
		pulseKey = 0
	}: { status?: string; size?: number; pulseKey?: number } = $props();
</script>

{#key pulseKey}
	<span
		class="inline-block rounded-full {statusDotBg(status)} {pulseKey > 0 ? 'pulse' : ''}"
		style="width:{size}px; height:{size}px"
		aria-label={status}
	></span>
{/key}
