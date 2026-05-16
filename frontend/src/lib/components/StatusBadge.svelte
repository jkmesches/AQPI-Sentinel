<script lang="ts">
	import { statusColor } from '$lib/format';
	let { status, label }: { status: string; label?: string } = $props();
	const glyphs: Record<string, string> = {
		pass: '▲',
		warn: '◆',
		fail: '▽',
		error: '✕',
		skip: '·'
	};
</script>

<span class="inline-flex items-center gap-1 {statusColor(status)}">
	<span aria-hidden="true">{glyphs[status] ?? '·'}</span>
	<span class="uppercase tracking-wider text-[11px]">{label ?? status}</span>
</span>
