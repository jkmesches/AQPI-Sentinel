<script lang="ts">
	import { sentinel } from '$lib/stores/state.svelte';
	import { rankAlarms } from '$lib/alarmRank';
	import StatusDot from '$lib/components/StatusDot.svelte';
	import Sparkline from '$lib/components/Sparkline.svelte';
	import MapView from '$lib/components/MapView.svelte';
	import SectionHeader from '$lib/components/SectionHeader.svelte';
	import {
		fmtAge, severityChip, statusText, statusBorder,
		stageLabel, prettyCheckLabel, productLabel,
		productCategory, PRODUCT_CATEGORY_ORDER, PRODUCT_CATEGORY_LABEL,
		sparklineMetric, sparklineDomain, sparklineWarnAt } from '$lib/format';
	import { api } from '$lib/api';
	import { diag } from '$lib/diag';
	import { auth } from '$lib/stores/auth.svelte';

	// L2 + LB2 together: the same radar seen through radarca and read off the
	// backend. Kept flat for the header's pass count; the rails render
	// `radarPairs` instead, which puts the two sources on ONE row.
	const radarRows = $derived(
		[...(sentinel.rollup?.stages?.L2 ?? []), ...(sentinel.rollup?.stages?.LB2 ?? [])]
			.slice().sort((a, b) => a.target.localeCompare(b.target))
	);

	// Does THIS deployment have the backend tree mounted?
	//
	// Read off the /api/checks catalog, not the rollup. The backend checks are
	// registered at import time behind `if SETTINGS.backend_root:`, so the
	// catalog answers correctly from the first paint; keying off an empty
	// stages.LB2 would render the one-column layout until the first LB run
	// landed and then jump to two columns.
	//
	// Absent is not the same as failing: without the mount these checks do not
	// exist, so there is nothing to show and no column to show it in. If the
	// mount drops *after* boot the checks stay registered and report fail,
	// which is what we want to see.
	const hasBackend = $derived.by(() => {
		const stages = new Set(sentinel.checks.map((c) => c.stage));
		return { products: stages.has('LB1'), radars: stages.has('LB2') };
	});

	// One row per target, carrying whichever of the two sources reported it.
	// Both halves are optional on purpose:
	//   - no mount at all (the shirejoe deployment) → every `backend` is null
	//     and the rails collapse to a single column
	//   - CBAND is skipped when SENTINEL_SSCB_ROOT is unset even though the
	//     rest of the tree is mounted, so one radar can lack a backend row
	//     while its neighbours have one
	//   - L1 carries vector + stream feeds that have no backend counterpart
	//     at all
	// Those cells say so rather than rendering a blank, which reads as broken.
	function pairByTarget(primary: any[], backend: any[]) {
		const out = new Map<string, { target: string; primary: any; backend: any }>();
		const slot = (t: string) => {
			let s = out.get(t);
			if (!s) { s = { target: t, primary: null, backend: null }; out.set(t, s); }
			return s;
		};
		for (const r of primary) slot(r.target).primary = r;
		for (const r of backend) slot(r.target).backend = r;
		return [...out.values()].sort((a, b) => a.target.localeCompare(b.target));
	}
	const pairStatuses = (p: { primary: any; backend: any }) =>
		[p.primary, p.backend].filter(Boolean);
	// A row is "bad" when EITHER source is unhappy — including the case the
	// paired layout exists for, where exactly one of them is.
	const pairIsBad = (p: { primary: any; backend: any }) =>
		pairStatuses(p).some((r: any) => r.status !== 'pass' && r.status !== 'skip');

	// Group headers count CHECKS, not rows: a paired row holds up to two.
	const groupCount = (pairs: any[]) => {
		const all = pairs.flatMap(pairStatuses);
		return { pass: all.filter((r: any) => r.status === 'pass').length, total: all.length };
	};

	const radarPairs = $derived(pairByTarget(
		sentinel.rollup?.stages?.L2 ?? [],
		sentinel.rollup?.stages?.LB2 ?? []
	));
	const productPairs = $derived(pairByTarget(
		sentinel.rollup?.stages?.L1 ?? [],
		sentinel.rollup?.stages?.LB1 ?? []
	));
	// CheckMeta lookup by check_id — used per-row to feed cadence into the
	// Sparkline so its visible window auto-sizes to each check (radars at
	// 2-min cadence get a 1-hour window; forecasts at 30-min cadence get
	// several hours).
	const checksById = $derived.by(() => {
		const out: Record<string, { cadence_s: number }> = {};
		for (const c of sentinel.checks) out[c.id] = c;
		return out;
	});
	// All L1 checks together: products + vector overlays + stream feeds.
	// They all carry the "Product Freshness" stage descriptor and behave
	// the same way (an upstream feed with an expected refresh cadence), so
	// users see them as one category. Previously vectors + streams were
	// grouped under "Edge" alongside L0 site checks which made the labels
	// inconsistent — flagged in the 2026-05-18 review.
	const productRows = $derived(
		[...(sentinel.rollup?.stages?.L1 ?? []), ...(sentinel.rollup?.stages?.LB1 ?? [])]
			.slice()
			.sort((a, b) => a.target.localeCompare(b.target))
	);
	// Site = L0 only (origin liveness, TLS, public page, root-404).
	// Renamed from "Edge" since it now drops the L1 static feeds.
	const siteRows = $derived(sentinel.rollup?.stages?.L0 ?? []);

	// Group product rows by category (Radar Data / Atmospheric Forecast /
	// CoSMoS / NWM / Other). Vectors + stream feeds fall into "Other".
	// `nwm` stays in the list with an empty row count so the operator
	// knows it's accounted for upstream even though no products land here.
	const productGroups = $derived.by(() => {
		const groups: Record<string, any[]> = {};
		for (const cat of PRODUCT_CATEGORY_ORDER) groups[cat] = [];
		// Groups hold PAIRS now, categorised by target — the same key for both
		// sources, so a product and its backend reading never land in
		// different groups.
		for (const r of productPairs) {
			// Category is purely target-keyed now. The mapping in format.ts
			// covers layer1.product.* (radar/forecast/cosmos), plus
			// layer1.vector.* + layer1.stream.* targets that source from
			// NWM (flowlines, watersheds, stream, stream_csv). Anything
			// unknown still falls to "other".
			groups[productCategory(r.target)].push(r);
		}
		return PRODUCT_CATEGORY_ORDER
			.map((cat) => ({ category: cat, label: PRODUCT_CATEGORY_LABEL[cat], rows: groups[cat] }))
			.filter((g) => g.category !== 'other' || g.rows.length > 0);
	});
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
		return arr[arr.length - 1].value;
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

	// The front page is a glance surface, so the alarm list is capped and
	// ordered by urgency rather than by arrival. Everything hidden here is one
	// click away in /history?tab=alarms, and the header always states the true
	// open count — a dashboard that quietly shows fewer problems than exist is
	// worse than one that shows too many.
	const ALARM_ROWS = 5;

	// Acknowledged means someone has taken it; it stays in the record and in
	// the count, it just stops occupying one of five scarce rows.
	let showAcked = $state(false);

	const ranking = $derived(rankAlarms(sentinel.alarms, { showAcked, limit: ALARM_ROWS }));
	const shownAlarms   = $derived(ranking.shown);
	const overflowCount = $derived(ranking.overflow);
	const ackedCount    = $derived(ranking.acked);
</script>

<!--
  One source's reading of one target: its status dot, a two-character tag
  naming WHICH source, and its headroom sparkline.

  The tag is the point of the paired layout. A column header only works while
  it is on screen — it is gone the moment the rail scrolls, and gone again
  when someone pastes a screenshot of three rows into a thread. Two characters
  welded to the trace survive both. K2 = the backend tree (K2, or Trinity for
  CBAND); RC = radarca, the scraped upstream.

  A null `row` means this source does not cover this target on this
  deployment — no backend mount at all, CBAND without SENTINEL_SSCB_ROOT, or a
  vector/stream feed that has no backend counterpart. Saying so is deliberate:
  a blank cell reads as a failure.
-->
{#snippet sourceCell(row: any, tag: string, paired: boolean, w: number)}
	{#if !row}
		<span
			class="num justify-self-start whitespace-nowrap text-[9.5px] text-[var(--color-faint)]"
			title="not monitored on this deployment — the backend tree is not mounted for this target"
		>
			not mounted
		</span>
	{:else}
		{@const metric = sparklineMetric(row.check_id)}
		{@const spark = sentinel.metrics[`${row.check_id}|${metric ?? 'age_s'}`] ?? []}
		{@const cadenceS = checksById[row.check_id]?.cadence_s ?? 120}
		<span
			class="flex min-w-0 items-center gap-1.5 {statusText(row.status)}"
			title={`${row.check_id} · ${row.target}${row.summary ? `\n${row.summary}` : ''}`}
		>
			<StatusDot status={row.status} size={8} pulseKey={sentinel.pulseTick[row.check_id] ?? 0} />
			{#if paired}
				<span class="num shrink-0 rounded border border-[var(--color-border-strong)] px-[3px] text-[8.5px] leading-[1.5] tracking-[0.06em] text-[var(--color-faint)]">{tag}</span>
			{/if}
			{#if diag.spark}
				<Sparkline
					data={spark}
					{cadenceS}
					width={w}
					height={14}
					domain={sparklineDomain(metric)}
					warnAt={sparklineWarnAt(metric)}
					showLabel={!paired}
				/>
			{/if}
		</span>
	{/if}
{/snippet}

<div class="grid h-full grid-cols-12 grid-rows-[1fr_auto] gap-2 p-2">
	<!-- LEFT RAIL: SITE + RADARS + IMAGE QC ------------------------------------------- -->
	<!-- Site sits ABOVE Radars: the L0 connectivity tier is the root cause
	     of most cascading failures, so keeping it at eye level makes
	     "what's actually broken" the first thing the operator sees. -->
	<aside class="panel col-span-3 row-span-1 flex flex-col overflow-hidden">
		<SectionHeader title="Site" right={stageLabel('L0')} />
		<ul class="shrink-0 divide-y divide-[var(--color-border)]">
			{#each siteRows as r}
				<li class="row-hover flex items-center gap-2 px-3 py-1.5 text-[11.5px]">
					<StatusDot status={r.status} size={7} pulseKey={sentinel.pulseTick[r.check_id] ?? 0} />
					<span class="text-[var(--color-default)] num" title={r.check_id}>{prettyCheckLabel(r.check_id, r.target)}</span>
					<span class="ml-auto truncate text-[var(--color-muted)] num text-[10.5px]">{r.summary}</span>
				</li>
			{/each}
		</ul>

		<SectionHeader title="Radars" count="{radarRows.filter((r) => r.status === 'pass').length}/{radarRows.length}" right={hasBackend.radars ? `${stageLabel('L2')} + ${stageLabel('LB2')}` : stageLabel('L2')} />
		{#if hasBackend.radars}
			<!-- Column headers only exist in the paired layout. They name the
			     sources in full: "K2/TRIN" was not parseable cold, and these
			     labels are the first thing a new operator reads. -->
			<div class="grid grid-cols-[3.6rem_1fr_1fr_auto] items-end gap-2 border-b border-[var(--color-border)] px-3 py-1 text-[9.5px] uppercase tracking-[0.1em] text-[var(--color-muted)]">
				<span></span><span>K2 / Trinity</span><span>RadarCA</span><span></span>
			</div>
		{/if}
		<ul class="min-h-0 flex-1 divide-y divide-[var(--color-border)] overflow-y-auto">
			{#each radarPairs as p (p.target)}
				{@const imgQc = l4XbandByRadar[p.target]}
				<li class="row-hover {pairIsBad(p) ? 'row-bad' : ''} grid {hasBackend.radars ? 'grid-cols-[3.6rem_1fr_1fr_auto]' : 'grid-cols-[3.6rem_3rem_1fr_auto]'} items-center gap-2 px-3 py-2 text-[12px]">
					<!-- The bare target, once. The old rail rendered prettyCheckLabel
					     per row, which read "XEBY" then "XEBY · backend" down the
					     list; with the two sources on one row the radar is named
					     once and each source is named by its own column + tag. -->
					<span class="num truncate text-[13px] text-[var(--color-bright)] tracking-wide"
						  title={p.target}>{p.target}</span>
					{#if hasBackend.radars}
						{@render sourceCell(p.backend, 'K2', true, 76)}
					{:else}
						<!-- Unpaired, so there is room for the word. Dropping it would
						     change today's rail on a deployment this change otherwise
						     leaves alone. With two sources there is no room for two of
						     these, and the dot plus the trace colour carry it. -->
						<span class="label text-left {statusText(p.primary?.status ?? 'skip')}">
							{p.primary?.status === 'pass' ? 'UP'
								: p.primary?.status === 'fail' ? 'DOWN'
								: (p.primary?.status ?? 'skip').toUpperCase()}
						</span>
					{/if}
					{@render sourceCell(p.primary, 'RC', hasBackend.radars, hasBackend.radars ? 76 : 86)}
					<span class="num text-[10.5px] text-[var(--color-muted)]">
						{#if imgQc}
							<span class="inline-block align-middle" title="image QC">
								<StatusDot status={imgQc} size={6} />
							</span>
						{/if}
					</span>
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
		<SectionHeader title="Products" count="{productRows.filter((r) => r.status === 'pass').length}/{productRows.length}" right={hasBackend.products ? `${stageLabel('L1')} + ${stageLabel('LB1')}` : stageLabel('L1')} />
		{#if hasBackend.products}
			<div class="grid grid-cols-[1fr_auto_auto] items-end gap-2 border-b border-[var(--color-border)] px-3 py-1 text-[9.5px] uppercase tracking-[0.1em] text-[var(--color-muted)]">
				<span></span><span>K2 / Trinity</span><span>RadarCA</span>
			</div>
		{/if}
		<div class="overflow-y-auto">
			{#each productGroups as g}
				{@const gc = groupCount(g.rows)}
				<div class="border-b border-[var(--color-border)] bg-[var(--color-canvas)]/40 px-3 py-1 text-[10px] uppercase tracking-[0.16em] text-[var(--color-muted)] flex items-center gap-2">
					<span>{g.label}</span>
					<span class="num text-[9.5px] text-[var(--color-faint)] ml-auto">
						{g.rows.length === 0 ? 'not exposed upstream' : `${gc.pass}/${gc.total}`}
					</span>
				</div>
				{#if g.rows.length === 0}
					<div class="px-3 py-1.5 text-[10.5px] text-[var(--color-faint)] italic">
						no products in this group.
					</div>
				{:else}
					<ul class="divide-y divide-[var(--color-border)]">
						{#each g.rows as p (p.target)}
							{@const ageS = p.primary ? ageFromMetrics(p.primary.check_id, 'age_s') : null}
							<li class="row-hover {pairIsBad(p) ? 'row-bad' : ''} grid {hasBackend.products ? 'grid-cols-[1fr_auto_auto]' : 'grid-cols-[1fr_4.5rem_auto]'} items-center gap-2 px-3 py-1.5 text-[12px]">
								<span class="num truncate text-[var(--color-bright)]"
									  title={p.primary ? `${p.primary.check_id} · ${p.target}` : p.target}>
									{p.primary ? prettyCheckLabel(p.primary.check_id, p.target) : productLabel(p.target)}
								</span>
								{#if hasBackend.products}
									{@render sourceCell(p.backend, 'K2', true, 64)}
									{@render sourceCell(p.primary, 'RC', true, 64)}
								{:else}
									<!-- Unpaired: keep the age readout this rail has always
									     had. It only loses its column when the second source
									     needs the room. -->
									<span class="num text-right text-[10.5px] {statusText(p.primary?.status ?? 'skip')}">
										{ageS !== null ? fmtAge(ageS, { signed: true }) : '—'}
									</span>
									{@render sourceCell(p.primary, 'RC', false, 56)}
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			{/each}
		</div>
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
		{#if sentinel.alarms.length}
			<div class="flex items-center gap-3 border-b border-[var(--color-border)] px-4 py-1 text-[10.5px] uppercase tracking-wider text-[var(--color-faint)]">
				<span>most severe first · top {ALARM_ROWS}</span>
				{#if ackedCount}
					<label class="flex cursor-pointer items-center gap-1.5 text-[var(--color-muted)] hover:text-[var(--color-bright)]">
						<input type="checkbox" bind:checked={showAcked} class="h-3 w-3 accent-[var(--color-ok)]" />
						show acknowledged ({ackedCount})
					</label>
				{/if}
				<span class="ml-auto normal-case tracking-normal">
					{#if overflowCount}
						<a class="text-[var(--color-muted)] underline decoration-dotted hover:text-[var(--color-bright)]"
						   href="/history?tab=alarms">+{overflowCount} more not shown</a>
					{:else}
						<a class="text-[var(--color-faint)] hover:text-[var(--color-bright)]"
						   href="/history?tab=alarms">all alarms</a>
					{/if}
				</span>
			</div>
		{/if}
		<ul class="divide-y divide-[var(--color-border)] overflow-y-auto">
			{#each shownAlarms as a (a.id)}
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
					<span class="num text-[10.5px] text-[var(--color-muted)]" title={a.stage}>{stageLabel(a.stage)}</span>
					<span class="num text-[var(--color-bright)]">#{a.id}</span>
					<span class="text-[var(--color-default)] truncate" title={a.target}>{prettyCheckLabel(a.check_id, a.target)}</span>
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
							title="undo acknowledgment"
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
			{:else if !shownAlarms.length}
				<!-- Open alarms exist but every one is acknowledged. Say so
				     explicitly: "no open alarms" here would be a lie. -->
				<li class="px-4 py-3 text-[12px] text-[var(--color-faint)]">
					all {sentinel.alarms.length} open alarm{sentinel.alarms.length === 1 ? '' : 's'}
					{sentinel.alarms.length === 1 ? 'is' : 'are'} acknowledged —
					<button type="button" class="underline decoration-dotted hover:text-[var(--color-bright)]"
					        onclick={() => (showAcked = true)}>show {sentinel.alarms.length === 1 ? 'it' : 'them'}</button>.
				</li>
			{/if}
		</ul>
	</section>
</div>
