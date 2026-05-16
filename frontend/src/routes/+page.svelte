<script lang="ts">
	import { sentinel } from '$lib/stores/state.svelte';
	import StatusDot from '$lib/components/StatusDot.svelte';
	import Sparkline from '$lib/components/Sparkline.svelte';
	import MapView from '$lib/components/MapView.svelte';
	import SectionHeader from '$lib/components/SectionHeader.svelte';
	import { fmtAge, severityChip, statusText, statusBorder } from '$lib/format';
	import { api } from '$lib/api';
	import { diag } from '$lib/diag';
	import { auth } from '$lib/stores/auth.svelte';

	const radarRows = $derived(
		(sentinel.rollup?.stages?.L2 ?? []).slice().sort((a, b) => a.target.localeCompare(b.target))
	);
	const productRows = $derived(
		(sentinel.rollup?.stages?.L1 ?? [])
			.filter((r) => r.check_id.startsWith('layer1.product.'))
			.slice()
			.sort((a, b) => a.target.localeCompare(b.target))
	);
	const vectorRows = $derived(
		(sentinel.rollup?.stages?.L1 ?? []).filter((r) => r.check_id.startsWith('layer1.vector.'))
	);
	const streamRows = $derived(
		(sentinel.rollup?.stages?.L1 ?? []).filter((r) => r.check_id.startsWith('layer1.stream.'))
	);
	const edgeRows = $derived([...(sentinel.rollup?.stages?.L0 ?? []), ...vectorRows, ...streamRows]);
	const l4XbandByRadar = $derived.by(() => {
		const out: Record<string, string> = {};
		for (const r of sentinel.rollup?.stages?.['L4-T1T2'] ?? []) {
			if (r.check_id.startsWith('layer4.xband.')) out[r.target] = r.status;
		}
		return out;
	});

	function ageFromMetrics(checkId: string, metric: string): number | null {
		const k = `${checkId}|${metric}`;
		const arr = sentinel.metrics[k];
		if (!arr?.length) return null;
		return arr[arr.length - 1];
	}

	async function ack(id: number) {
		try {
			await api.ack(id, { note: 'acked from UI' });
			await sentinel.refresh();
		} catch (e) {
			alert(`ack failed: ${(e as Error).message}`);
		}
	}
	async function unack(id: number) {
		try {
			await api.unack(id);
			await sentinel.refresh();
		} catch (e) {
			alert(`unack failed: ${(e as Error).message}`);
		}
	}

	const sevColor: Record<string, string> = {
		info: 'text-[var(--color-info)]',
		warn: 'text-[var(--color-warn)]',
		critical: 'text-[var(--color-critical)]'
	};
</script>

<div class="grid h-full grid-cols-12 grid-rows-[1fr_auto] gap-2 p-2">
	<!-- LEFT RAIL: RADARS + IMAGE QC ------------------------------------------------- -->
	<aside class="panel col-span-3 row-span-1 flex flex-col overflow-hidden">
		<SectionHeader title="Radars" count="{radarRows.filter((r) => r.status === 'pass').length}/{radarRows.length}" right="L2" />
		<ul class="divide-y divide-[var(--color-border)]">
			{#each radarRows as r}
				{@const spark = sentinel.metrics[`${r.check_id}|images_Reflectivity`] ?? []}
				{@const imgQc = l4XbandByRadar[r.target]}
				{@const latest = spark[spark.length - 1] ?? 0}
				<li class="row-hover grid grid-cols-[auto_3.2rem_auto_1fr_auto] items-center gap-2 px-3 py-2 text-[12px]">
					<StatusDot status={r.status} size={9} pulseKey={sentinel.pulseTick[r.check_id] ?? 0} />
					<span class="num text-[13px] text-[var(--color-bright)] tracking-wide">{r.target}</span>
					<span class="label {statusText(r.status)}">{r.status === 'pass' ? 'UP' : r.status === 'fail' ? 'DOWN' : r.status.toUpperCase()}</span>
					<span class="ml-2 {statusText(r.status)}">
						{#if diag.spark}
							<Sparkline data={spark} width={72} height={16} />
						{/if}
					</span>
					<span class="num text-[10.5px] text-[var(--color-muted)]">
						{latest ? `${latest|0}/h` : ''}
						{#if imgQc}
							<span class="ml-1 inline-block h-1.5 w-1.5 rounded-full align-middle" style="background:{({pass:'#34d399',warn:'#fbbf24',fail:'#f87171',skip:'#404657'} as Record<string,string>)[imgQc] ?? '#404657'}"></span>
						{/if}
					</span>
				</li>
			{/each}
		</ul>

		<SectionHeader title="Edge" right="L0  +  static" />
		<ul class="divide-y divide-[var(--color-border)]">
			{#each edgeRows as r}
				<li class="row-hover flex items-center gap-2 px-3 py-1.5 text-[11.5px]">
					<StatusDot status={r.status} size={7} pulseKey={sentinel.pulseTick[r.check_id] ?? 0} />
					<span class="text-[var(--color-default)] num">{r.target}</span>
					<span class="ml-auto truncate text-[var(--color-muted)] num text-[10.5px]">{r.summary}</span>
				</li>
			{/each}
		</ul>
	</aside>

	<!-- CENTER: MAP (hero) ----------------------------------------------------------- -->
	<section class="panel col-span-6 row-span-1 flex flex-col overflow-hidden">
		<SectionHeader title="Network" count="6 radars · 3 NEXRAD" right="EPSG:3857 · Stadia · alidade" />
		<div class="flex-1">
			{#if diag.map}
				<MapView />
			{:else}
				<div class="flex h-full items-center justify-center text-[12px] text-[var(--color-muted)] uppercase tracking-[0.18em]">
					map disabled · ?diag=no-map
				</div>
			{/if}
		</div>
	</section>

	<!-- RIGHT RAIL: PRODUCTS --------------------------------------------------------- -->
	<aside class="panel col-span-3 row-span-1 flex flex-col overflow-hidden">
		<SectionHeader title="Products" count="{productRows.filter((r) => r.status === 'pass').length}/{productRows.length}" right="L1" />
		<ul class="divide-y divide-[var(--color-border)] overflow-y-auto">
			{#each productRows as r}
				{@const spark = sentinel.metrics[`${r.check_id}|age_s`] ?? []}
				{@const ageS = ageFromMetrics(r.check_id, 'age_s')}
				<li class="row-hover grid grid-cols-[auto_1fr_auto_auto] items-center gap-2 px-3 py-1.5 text-[12px]">
					<StatusDot status={r.status} size={8} pulseKey={sentinel.pulseTick[r.check_id] ?? 0} />
					<span class="num truncate text-[var(--color-bright)]">{r.target}</span>
					<span class="num text-[10.5px] {statusText(r.status)}">
						{ageS !== null ? fmtAge(ageS, { signed: true }) : '—'}
					</span>
					<span class={statusText(r.status)}>
						{#if diag.spark}
							<Sparkline data={spark} width={48} height={14} />
						{/if}
					</span>
				</li>
			{/each}
		</ul>
	</aside>

	<!-- BOTTOM: ALARM STREAM --------------------------------------------------------- -->
	<section class="panel col-span-12 row-span-1 flex max-h-[36vh] flex-col overflow-hidden">
		<SectionHeader
			title="Alarms"
			count="{sentinel.alarms.length} open"
			right={sentinel.alarms.length
				? `oldest ${fmtAge(Math.max(...sentinel.alarms.map((a) => (Date.now() - new Date(a.opened_at).getTime()) / 1000)))}`
				: 'all clear'}
		/>
		<ul class="divide-y divide-[var(--color-border)] overflow-y-auto">
			{#each sentinel.alarms as a}
				{@const opened = new Date(a.opened_at)}
				{@const ageS = (Date.now() - opened.getTime()) / 1000}
				{@const olderThanDay = ageS > 86400}
				{@const openedZ = olderThanDay
					? opened.toISOString().slice(5, 10).replace('-', '/') +
					  ' ' +
					  opened.toISOString().slice(11, 16) +
					  'Z'
					: opened.toISOString().slice(11, 19) + 'Z'}
				{@const isAcked = !!a.ack}
				<li
					class="row-hover sev-bar grid grid-cols-[2.5rem_3.2rem_3rem_12rem_auto_auto_1fr_auto] items-center gap-3 px-4 py-1.5 text-[12px] {isAcked ? 'opacity-50' : ''} {sevColor[a.severity] ??
						''}"
					title={isAcked ? `acked by ${a.ack?.acked_by} at ${a.ack?.acked_at}${a.ack?.note ? ' — ' + a.ack.note : ''}` : ''}
				>
					<span class={severityChip(a.severity)}>{a.severity}</span>
					<span class="num text-[10.5px] text-[var(--color-muted)]">{a.stage}</span>
					<span class="num text-[var(--color-bright)]">#{a.id}</span>
					<span class="num text-[var(--color-default)] truncate">{a.target}</span>
					<span
						class="num text-[10.5px] text-[var(--color-muted)]"
						title="opened {a.opened_at}"
					>
						{openedZ}
					</span>
					<span class="num text-[10.5px] text-[var(--color-faint)]">
						{fmtAge(ageS)}
					</span>
					<span class="truncate text-[var(--color-muted)] text-[11.5px]">
						{#if isAcked}
							<span class="text-[10px] uppercase tracking-wider text-[var(--color-ok)] mr-1">acked · {a.ack?.acked_by}</span>
						{/if}
						{a.message}
					</span>
					{#if !auth.user}
						<a
							class="border border-[var(--color-border-strong)] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[var(--color-faint)] hover:text-[var(--color-bright)]"
							href="/login?next={encodeURIComponent('/')}"
							title="sign in to acknowledge alarms"
						>
							sign in
						</a>
					{:else if isAcked}
						<button
							type="button"
							class="border border-[var(--color-border-strong)] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:bg-[var(--color-elevated)] hover:text-[var(--color-bright)]"
							onclick={() => unack(a.id)}
							title="undo acknowledgement"
						>
							unack
						</button>
					{:else}
						<button
							type="button"
							class="border border-[var(--color-border-strong)] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[var(--color-default)] hover:bg-[var(--color-elevated)] hover:text-[var(--color-bright)]"
							onclick={() => ack(a.id)}
						>
							ack
						</button>
					{/if}
				</li>
			{/each}
			{#if !sentinel.alarms.length}
				<li class="px-4 py-3 text-[12px] text-[var(--color-faint)]">no open alarms.</li>
			{/if}
		</ul>
	</section>
</div>
