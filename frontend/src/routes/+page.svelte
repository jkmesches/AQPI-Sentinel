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
		sparklineMetric, sparklineDomain, sparklineWarnAt, isFleetCheck,
		arrivalRowOf, BACKEND_FLEET_CHECK_ID } from '$lib/format';
	import { groupByTarget, worstOf, isAlerting } from '$lib/grouping';
	import GroupRow from '$lib/components/GroupRow.svelte';
	import { api, type CheckMeta } from '$lib/api';
	import { diag } from '$lib/diag';
	import { auth } from '$lib/stores/auth.svelte';

	// Every reading of every radar, flat, for the header's pass count. The rail
	// itself renders `radarGroups`, which collapses a radar's several readings
	// onto one expandable row — see $lib/grouping.
	const radarRows = $derived(
		[...(sentinel.rollup?.stages?.L2 ?? []), ...(sentinel.rollup?.stages?.LB2 ?? []),
		 ...(sentinel.rollup?.stages?.LB3 ?? [])]
			.filter((r) => !isFleetCheck(r.check_id))
			// The DROPS producer is in LB3 but is not about a radar, so it must
			// not land in the radar rail or its count. It gets its own row in
			// the Site rail, where a backend processing step belongs.
			.filter((r) => r.check_id !== 'layer3.backend.drops')
			.slice().sort((a, b) => a.target.localeCompare(b.target))
	);

	// One expandable row per radar, carrying every reading of it.
	//
	// Stage order inside a group is CAUSAL, not alphabetical: arrival (LB2)
	// explains a missing composite contribution (LB3), which in turn explains
	// what radarca reports (L2). Reading the consequence before the cause is
	// how an operator ends up investigating the wrong thing.
	const RADAR_STAGE_ORDER = ['LB2', 'LB3', 'L2'];
	const radarGroups = $derived(
		groupByTarget(
			radarRows.slice().sort(
				(a, b) => RADAR_STAGE_ORDER.indexOf(a.stage) - RADAR_STAGE_ORDER.indexOf(b.stage)
			)
		)
	);

	// Open state. Default is "open if alerting", with explicit operator choices
	// recorded separately so a radar that starts alerting can open itself
	// WITHOUT overwriting a collapse someone chose on purpose.
	let radarOverride = $state<Record<string, boolean>>({});
	const radarIsOpen = (target: string, alerting: boolean) =>
		radarOverride[target] ?? alerting;
	const toggleRadar = (target: string, alerting: boolean) => {
		radarOverride[target] = !radarIsOpen(target, alerting);
	};
	const allRadarsOpen = $derived(
		radarGroups.length > 0 && radarGroups.every((g) => radarIsOpen(g.target, g.alerting))
	);
	const setAllRadars = (open: boolean) => {
		for (const g of radarGroups) radarOverride[g.target] = open;
	};

	// The DROPS producer row, for the Site rail. Informational by design — see
	// backend Layer3DropsProducerCheck — so it is shown always rather than only
	// while alerting: an operator needs to be able to see that it IS running,
	// which was the whole gap on 2026-10-05.
	const dropsRow = $derived(
		(sentinel.rollup?.stages?.LB3 ?? []).find((r) => r.check_id === 'layer3.backend.drops')
			?? null
	);

	// The fleet correlation verdict is about the rail, not a row in it. Pulled
	// out so the count reads 6 radars rather than 7, and rendered as a banner
	// only when it has something to say — it is `pass` 99% of the time and a
	// permanently-green row that cannot be acted on is just furniture.
	//
	// The BACKEND fleet verdict wins when both exist. Both answer "one event or
	// N", but the backend one reads the filesystem and CBAND arrives on a
	// separate mount — so it can say whether the host, NFS and clock are fine
	// and localise the fault to the X-band path. The radarca-derived one cannot
	// distinguish "the radars stopped" from "the API we ask about them
	// stopped". Showing both would be two banners making the same claim with
	// different confidence.
	const fleetRow = $derived(
		(sentinel.rollup?.stages?.LB2 ?? []).find((r) => r.check_id === BACKEND_FLEET_CHECK_ID)
			?? (sentinel.rollup?.stages?.L2 ?? []).find((r) => isFleetCheck(r.check_id))
			?? null
	);
	const fleetAlerting = $derived(!!fleetRow && isAlerting(fleetRow.status));

	// Which sources exist on THIS deployment, per family.
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
	//
	// === Both sides are tracked, not just the backend ===
	//
	// This asked only `stages.has('LB1')` and treated the answer as "are there
	// two sources". That holds on AQPI, where the radarca stages always exist
	// and only the backend is ever absent — so the two questions coincide and
	// the bug is invisible.
	//
	// It breaks on the inverse, which is exactly what the XQPI deployment is:
	// LB1/LB2 exist and L1/L2 do not, because nothing serves FLOW over HTTP.
	// The rail would take the paired branch, find `primary` null on every row,
	// and print "not mounted" down the whole RadarCA column — on a deployment
	// that is working as designed, and with a tooltip that is wrong twice over
	// (it is the radarca side that is absent, and there is no backend tree
	// failing to mount).
	// Keyed by TARGET, not by stage presence. "Does stage L1 exist" is too
	// coarse: one unrelated check sitting in that stage — the xband-fleet
	// correlation row is in L2, the NWM stream feed is in L1 — would flip the
	// whole rail into the paired layout and print "not mounted" on every row
	// that has no radarca counterpart. Asking whether any TARGET actually has
	// both readings cannot be fooled that way.
	const sources = $derived.by(() => {
		const pairable = (radarcaStage: string, backendStage: string) => {
			const rc = new Set<string>();
			const bk = new Set<string>();
			for (const c of sentinel.checks) {
				if (c.stage === radarcaStage) rc.add(c.target);
				else if (c.stage === backendStage) bk.add(c.target);
			}
			return {
				radarca: rc.size > 0,
				backend: bk.size > 0,
				// Paired means two readings of ONE target exist to compare —
				// not merely that both stages are populated.
				paired: [...bk].some((t) => rc.has(t))
			};
		};
		return { products: pairable('L1', 'LB1'), radars: pairable('L2', 'LB2') };
	});
	const pairedProducts = $derived(sources.products.paired);
	// In an unpaired rail the single reading may come from EITHER side, so
	// nothing downstream may assume it is `primary`.
	const loneOf = (p: { primary: any; backend: any }) => p.primary ?? p.backend;
	// The stage descriptor for whichever sources a family actually has.
	const sourceLabelFor = (fam: { radarca: boolean; backend: boolean; paired: boolean },
	                        rc: string, bk: string) =>
		fam.paired ? `${stageLabel(rc)} + ${stageLabel(bk)}`
		: fam.backend ? stageLabel(bk)
		: stageLabel(rc);

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

	const productPairs = $derived(pairByTarget(
		sentinel.rollup?.stages?.L1 ?? [],
		sentinel.rollup?.stages?.LB1 ?? []
	));
	// CheckMeta lookup by check_id — used per-row to feed cadence into the
	// Sparkline so its visible window auto-sizes to each check (radars at
	// 2-min cadence get a 1-hour window; forecasts at 30-min cadence get
	// several hours).
	const checksById = $derived.by(() => {
		const out: Record<string, CheckMeta> = {};
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
{#snippet sourceCell(row: any, fallbackTag: string, paired: boolean, w: number)}
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
		{@const meta = checksById[row.check_id]}
		{@const cadenceS = meta?.cadence_s ?? 120}
		<!-- The check says which tree it read. Hardcoding "K2" was wrong for
		     CBAND, which comes off Trinity — see Check.source_tag. -->
		{@const tag = meta?.source_tag || fallbackTag}
		<span
			class="flex min-w-0 items-center gap-1.5 {statusText(row.status)}"
			title={`${row.check_id} · ${row.target}${meta?.source_label ? ` · read from ${meta.source_label}` : ''}${row.summary ? `\n${row.summary}` : ''}`}
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
	<!-- LEFT RAIL: SITE + RADARS -------------------------------------------------------- -->
	<!-- Site sits ABOVE Radars: the L0 connectivity tier is the root cause
	     of most cascading failures, so keeping it at eye level makes
	     "what's actually broken" the first thing the operator sees.
	     (Image QC is no longer a section of its own — it is the small dot in
	     each radar row's trailing cell.)

	     THE WHOLE COLUMN SCROLLS, not just the radar list. It used to pin the
	     Site rows and both headers and scroll only the radars in a nested
	     pane, which went wrong once a radar row could EXPAND: the inner pane
	     is sized by flex, so opening two or three radars left the detail
	     scrolling inside a few hundred pixels while a third of the column sat
	     fixed above it. One scroll region for one column — the expanders are
	     the reason the old arrangement stopped working. -->
	<aside class="panel col-span-3 row-span-1 flex flex-col overflow-y-auto">
		<SectionHeader title="Site" right={stageLabel('L0')} />
		<ul class="shrink-0 divide-y divide-[var(--color-border)]">
			{#each siteRows as r}
				<!-- The summary is clipped here too, but these are one-line L0
				     rows in a quarter-width rail and the full text is on the row's
				     hover title rather than nowhere. Left single-line on purpose:
				     eleven site rows that each wrap to two would push the radars
				     below the fold, and the radars are what the column is for. -->
				<li class="row-hover flex items-center gap-2 px-3 py-1.5 text-[11.5px]"
					title={`${prettyCheckLabel(r.check_id, r.target)} · ${r.check_id}${r.summary ? `\n${r.summary}` : ''}`}>
					<StatusDot status={r.status} size={7} pulseKey={sentinel.pulseTick[r.check_id] ?? 0} />
					<span class="text-[var(--color-default)] num">{prettyCheckLabel(r.check_id, r.target)}</span>
					<span class="ml-auto truncate text-[var(--color-muted)] num text-[10.5px]">{r.summary}</span>
				</li>
			{/each}
			{#if dropsRow}
				<!-- Informational by design: nothing in the live product chain
				     reads the DROPS tree, so this never pages (see alerts.yaml
				     and Layer3DropsProducerCheck). Shown even when healthy,
				     because being unable to see that it IS running is exactly
				     the gap that let it die unnoticed for twelve hours. -->
				<li class="row-hover flex items-center gap-2 px-3 py-1.5 text-[11.5px]"
					title={`${prettyCheckLabel(dropsRow.check_id, dropsRow.target)} · ${dropsRow.check_id} — informational, non-paging${dropsRow.summary ? `\n${dropsRow.summary}` : ''}`}>
					<StatusDot status={dropsRow.status} size={7} pulseKey={sentinel.pulseTick[dropsRow.check_id] ?? 0} />
					<!-- Via prettyCheckLabel, not hardcoded. Hardcoding it is how
					     this check ended up with two names in one app: the label
					     function said "DROPS producer" and this row said "QPE
					     producer". One source for the label means they cannot
					     disagree again. -->
					<span class="text-[var(--color-default)] num">{prettyCheckLabel(dropsRow.check_id, dropsRow.target)}</span>
					<span class="num shrink-0 rounded border border-[var(--color-border-strong)] px-[3px] text-[8.5px] leading-[1.5] tracking-[0.06em] text-[var(--color-faint)]">INFO</span>
					<span class="ml-auto truncate text-[var(--color-muted)] num text-[10.5px]">{dropsRow.summary}</span>
				</li>
			{/if}
		</ul>

		<div class="flex items-center gap-3 border-b border-[var(--color-border)] px-3 py-1.5">
			<span class="label">Radars</span>
			<span class="num text-[10.5px] text-[var(--color-faint)]">·  {radarRows.filter((r) => r.status === 'pass').length}/{radarRows.length}</span>
			<!-- One control, two states. Two buttons would leave one of them
			     always a no-op, and the label says what the click will DO
			     rather than what the rail currently is. -->
			<button
				type="button"
				class="ml-auto num text-[9.5px] uppercase tracking-[0.1em] text-[var(--color-muted)] hover:text-[var(--color-bright)]"
				onclick={() => setAllRadars(!allRadarsOpen)}
				title={allRadarsOpen ? 'collapse every radar' : 'expand every radar'}
			>
				{allRadarsOpen ? 'Collapse all' : 'Expand all'}
			</button>
			<span class="num text-[10.5px] text-[var(--color-muted)]">{sourceLabelFor(sources.radars, 'L2', 'LB2')}</span>
		</div>
		{#if fleetAlerting && fleetRow}
			<!-- The fleet verdict, where it belongs: above the radars it is a
			     statement about, and only while it is making one. Everything
			     below is explained by this one line, which is the whole point
			     of the check — one upstream event, not six radar outages. -->
			<div
				class="flex items-start gap-2 border-b border-[var(--color-border)] bg-[var(--color-fail)]/10 px-3 py-2"
				title={fleetRow.check_id}
			>
				<!-- shrink-0: the dot is a flex child in an items-start row and
				     gets squashed into a bar without it. -->
				<span class="mt-[3px] shrink-0"><StatusDot status={fleetRow.status} size={8} /></span>
				<div class="min-w-0">
					<div class="label text-[10px] tracking-[0.14em] {statusText(fleetRow.status)}">
						{fleetRow.check_id === BACKEND_FLEET_CHECK_ID ? 'Radar fleet · backend' : 'X-band fleet'}
					</div>
					<div class="num text-[11px] leading-snug text-[var(--color-default)]">
						{fleetRow.summary}
					</div>
				</div>
			</div>
		{/if}
		<!-- No flex-1/min-h-0/overflow here: the <aside> above owns the single
		     scroll region. A nested one would reintroduce the cramped inner
		     pane that expanding a radar made unusable. -->
		<ul class="divide-y divide-[var(--color-border)]">
			{#each radarGroups as g (g.target)}
				{@const imgQc = l4XbandByRadar[g.target]}
				{@const open = radarIsOpen(g.target, g.alerting)}
				{@const worstRow = g.rows.find((r) => r.status === g.worst) ?? g.rows[0]}
				{@const arrival = arrivalRowOf(g.rows)}
				<GroupRow
					label={g.target}
					worst={g.worst}
					{open}
					ontoggle={() => toggleRadar(g.target, g.alerting)}
					count={g.rows.length}
					pulseKey={sentinel.pulseTick[arrival?.check_id ?? ''] ?? 0}
					title={`${g.target} · ${g.rows.length} check${g.rows.length === 1 ? '' : 's'}`
						+ (worstRow?.summary ? `\n${worstRow.summary}` : '')}
				>
					<!-- No collapsed prose. The summary is written to be read in
					     full — in an email, the timeline, the detail modal — and
					     this rail is a quarter of the viewport wide. After the
					     radar name, the sparkline, the count and the QC dot there
					     is room for about twenty characters, so it rendered as
					     "BACKEND SILENT — newest volu…": an arbitrary truncation
					     that says less than the status dot already did, and can be
					     misread (that tail looks like the start of a filename).
					     The information is not superfluous, the PROSE FORM is wrong
					     for the width.
					     Three non-redundant signals remain, none of which can
					     truncate: the dot carries severity (worst-of across the
					     radar's checks), the sparkline carries magnitude against
					     the check's own limit on a fixed 0..1 axis, and the count
					     says how many readings are behind it. The full summary is
					     the row's hover title, and the expander has every check's
					     own words untruncated. -->
					{#snippet trailing()}
						<span class="flex items-center gap-2">
							{#if arrival && diag.spark}
								<!-- Data arrival, specifically: LB2 where it exists. LB3
								     measures a composite receipt written two steps later
								     and L2 measures what radarca says, so plotting
								     whichever came first would make the trace mean a
								     different thing on different rows. -->
								{@const metric = sparklineMetric(arrival.check_id)}
								<Sparkline
									data={sentinel.metrics[`${arrival.check_id}|${metric ?? 'age_s'}`] ?? []}
									cadenceS={checksById[arrival.check_id]?.cadence_s ?? 120}
									width={62}
									height={14}
									domain={sparklineDomain(metric)}
									warnAt={sparklineWarnAt(metric)}
									showLabel={false}
								/>
							{/if}
							{#if imgQc}
								<span class="inline-block align-middle" title="image QC">
									<StatusDot status={imgQc} size={6} />
								</span>
							{/if}
						</span>
					{/snippet}
					{#snippet detail()}
						<ul class="flex flex-col gap-1">
							{#each g.rows as r (r.check_id)}
								{@const meta = checksById[r.check_id]}
								{@const metric = sparklineMetric(r.check_id)}
								<!-- Two lines, not four columns. The first carries the
								     stage, dot, source tag and trace; the second gives
								     the summary THE WHOLE RAIL WIDTH and lets it wrap.
								     This was a single row with the summary in a `1fr`
								     column beside a 62px sparkline, with `truncate` on
								     it — so the expander clipped the text just like the
								     collapsed row did, which defeated the entire point
								     of moving the prose down here. An expander exists
								     to show what does not fit; one that truncates is
								     only a quieter version of the problem. -->
								<li
									class="flex flex-col gap-0.5 text-[11px]"
									title={`${r.check_id}${meta?.source_label ? ` · read from ${meta.source_label}` : ''}`}
								>
									<div class="flex items-center gap-2">
										<span class="num shrink-0 text-[9.5px] uppercase tracking-[0.08em] text-[var(--color-faint)]">
											{stageLabel(r.stage)}
										</span>
										<StatusDot status={r.status} size={7} pulseKey={sentinel.pulseTick[r.check_id] ?? 0} />
										{#if meta?.source_tag}
											<span class="num shrink-0 rounded border border-[var(--color-border-strong)] px-[3px] text-[8.5px] leading-[1.5] tracking-[0.06em] text-[var(--color-faint)]">{meta.source_tag}</span>
										{/if}
										{#if diag.spark}
											<span class="ml-auto shrink-0">
												<Sparkline
													data={sentinel.metrics[`${r.check_id}|${metric ?? 'age_s'}`] ?? []}
													cadenceS={meta?.cadence_s ?? 120}
													width={62}
													height={12}
													domain={sparklineDomain(metric)}
													warnAt={sparklineWarnAt(metric)}
													showLabel={false}
												/>
											</span>
										{/if}
									</div>
									<!-- break-words, not truncate: these summaries carry
									     the threshold in parentheses at the END, which is
									     the half a clipped line always loses. -->
									<span class="num break-words text-[10.5px] leading-snug {statusText(r.status)}">
										{r.summary}
									</span>
								</li>
							{/each}
						</ul>
					{/snippet}
				</GroupRow>
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
		<SectionHeader title="Products" count="{productRows.filter((r) => r.status === 'pass').length}/{productRows.length}" right={sourceLabelFor(sources.products, 'L1', 'LB1')} />
		{#if pairedProducts}
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
							{@const lone = loneOf(p)}
							{@const ageS = lone ? ageFromMetrics(lone.check_id, 'age_s') : null}
							<li class="row-hover {pairIsBad(p) ? 'row-bad' : ''} grid {pairedProducts ? 'grid-cols-[1fr_auto_auto]' : 'grid-cols-[1fr_4.5rem_auto]'} items-center gap-2 px-3 py-1.5 text-[12px]">
								<!-- Named off whichever reading exists. prettyCheckLabel
								     suffixes "· backend" for layer1.backend.*, which is
								     right in a paired rail and wrong when the backend IS
								     the only source, so an unpaired row uses the bare
								     product label. -->
								<span class="num truncate text-[var(--color-bright)]"
									  title={lone ? `${lone.check_id} · ${p.target}` : p.target}>
									{pairedProducts && p.primary
										? prettyCheckLabel(p.primary.check_id, p.target)
										: productLabel(p.target)}
								</span>
								{#if pairedProducts}
									{@render sourceCell(p.backend, 'K2', true, 64)}
									{@render sourceCell(p.primary, 'RC', true, 64)}
								{:else}
									<!-- Unpaired: keep the age readout this rail has always
									     had. It only loses its column when the second source
									     needs the room. -->
									<span class="num text-right text-[10.5px] {statusText(lone?.status ?? 'skip')}">
										{ageS !== null ? fmtAge(ageS, { signed: true }) : '—'}
									</span>
									{@render sourceCell(lone, 'RC', false, 56)}
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
