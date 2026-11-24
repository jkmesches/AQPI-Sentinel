<script lang="ts">
	import { sentinel } from '$lib/stores/state.svelte';
	import { rankAlarms } from '$lib/alarmRank';
	import StatusDot from '$lib/components/StatusDot.svelte';
	import DisclosureArrow from '$lib/components/DisclosureArrow.svelte';
	import Sparkline from '$lib/components/Sparkline.svelte';
	import SectionHeader from '$lib/components/SectionHeader.svelte';
	import {
		fmtAge, severityChip, statusText, statusBorder,
		stageLabel, prettyCheckLabel, productLabel,
		productCategory, PRODUCT_CATEGORY_ORDER, PRODUCT_CATEGORY_LABEL,
		sparklineMetric, sparklineDomain, sparklineWarnAt, isFleetCheck,
		arrivalRowOf, radarRowRank, BACKEND_FLEET_CHECK_ID,
		readoutMetric, fmtCadence, secondsToNextRun,
		parseImageQc, qcLabel } from '$lib/format';
	import { groupByTarget, worstOf, isAlerting } from '$lib/grouping';
	import { radarVerdicts } from '$lib/radarStatus';
	import { getSite } from '$lib/site';
	import { api, type CheckMeta } from '$lib/api';
	import { diag } from '$lib/diag';
	import { auth } from '$lib/stores/auth.svelte';
	import { hiddenCheckIds, sourceCounts } from '$lib/checkFilter';

	// Hide the checks whose readings come from RadarCA, or that watch a NEXRAD
	// radar, from every rail and from the alarm band. The backend classifies
	// each check (see $lib/checkFilter). Persisted per browser, and read in the
	// initializer so the effect that saves it cannot write the default over the
	// stored choice first.
	const storedFlag = (key: string): boolean => {
		try { return localStorage.getItem(key) === '1'; } catch { return false; }
	};
	let hideRadarca = $state(storedFlag('sentinel.dashboard.hideRadarca'));
	let hideNexrad = $state(storedFlag('sentinel.dashboard.hideNexrad'));
	$effect(() => {
		try {
			localStorage.setItem('sentinel.dashboard.hideRadarca', hideRadarca ? '1' : '0');
			localStorage.setItem('sentinel.dashboard.hideNexrad', hideNexrad ? '1' : '0');
		} catch { /* storage blocked: the filters still apply for this visit */ }
	});
	const hiddenIds = $derived(hiddenCheckIds(sentinel.checks, { hideRadarca, hideNexrad }));
	const sourceCount = $derived(sourceCounts(sentinel.checks));
	const shown = (r: { check_id: string }) => !hiddenIds.has(r.check_id);

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
			.filter(shown)
			.slice().sort((a, b) => a.target.localeCompare(b.target))
	);

	// One expandable row per radar, carrying every reading of it.
	//
	// Stage order inside a group is CAUSAL, not alphabetical: arrival (LB2)
	// explains a missing composite contribution (LB3), which in turn explains
	// what radarca reports (L2). Reading the consequence before the cause is
	// how an operator ends up investigating the wrong thing.
	// Ordering lives in $lib so it is testable and so the two LB3 families stay
	// in pipeline order rather than alphabetical — see radarRowRank.
	//
	// The collapsed row carries the map pin's verdict rather than the worst of
	// the group, so one radar is not two colors on two pages. $lib/radarStatus
	// holds that rule: arrival decides where the deployment reads the
	// filesystem, and a composite shortfall is reported on the Products rail
	// instead of reddening a radar whose volumes are landing on time. Expanding
	// the row still shows every reading, which is where that detail belongs.
	const radarVerdictMap = $derived(radarVerdicts(sentinel.rollup?.stages));
	const radarGroups = $derived(
		groupByTarget(radarRows.slice().sort((a, b) => radarRowRank(a) - radarRowRank(b)))
			.map((g) => {
				const worst = radarVerdictMap[g.target]?.status ?? g.worst;
				return { ...g, worst, alerting: isAlerting(worst) };
			})
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
		(sentinel.rollup?.stages?.LB3 ?? []).filter(shown)
			.find((r) => r.check_id === 'layer3.backend.drops') ?? null
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
		(sentinel.rollup?.stages?.LB2 ?? []).filter(shown)
			.find((r) => r.check_id === BACKEND_FLEET_CHECK_ID)
			?? (sentinel.rollup?.stages?.L2 ?? []).filter(shown)
				.find((r) => isFleetCheck(r.check_id))
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
				// A hidden side is absent for layout, so hiding RadarCA turns
				// the paired rails into backend-only ones.
				if (hiddenIds.has(c.id)) continue;
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
	//   - no mount at all (the test deployment) → every `backend` is null
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
		(sentinel.rollup?.stages?.L1 ?? []).filter(shown),
		(sentinel.rollup?.stages?.LB1 ?? []).filter(shown)
	));
	// How many products each category holds before the source filters. The
	// National Water Model feeds all read RadarCA and have no backend half, so
	// "hide RadarCA" empties that group, which then claimed upstream exposed
	// no such products.
	const productsBeforeFilters = $derived.by(() => {
		const targets: Record<string, Set<string>> = {};
		const all = [...(sentinel.rollup?.stages?.L1 ?? []), ...(sentinel.rollup?.stages?.LB1 ?? [])];
		for (const r of all) {
			(targets[productCategory(r.target)] ??= new Set()).add(r.target);
		}
		return Object.fromEntries(Object.entries(targets).map(([cat, set]) => [cat, set.size]));
	});
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
			.filter(shown)
			.sort((a, b) => a.target.localeCompare(b.target))
	);
	// Site = L0 only (origin liveness, TLS, public page, root-404).
	// Renamed from "Edge" since it now drops the L1 static feeds.
	const siteRows = $derived((sentinel.rollup?.stages?.L0 ?? []).filter(shown));

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
	// Which product the participation rows belong to, straight from the
	// backend. Empty on a backend that predates the field, and the rail then
	// shows no input row: attaching a composite's fault to a guessed product
	// is worse than leaving the reading in the radar rail alone.
	let compositeProduct = $state('');
	$effect(() => { getSite().then((s) => (compositeProduct = s.compositeProduct)); });

	// What the composite's own row says about its inputs.
	//
	// `layer3.composite.<RADAR>` targets a radar, so it groups under that radar
	// in the rail beside it. The fault it reports belongs to the composite: a
	// radar arriving on time with no block in the latest run leaves the
	// composite short, and an operator needs that named on the composite with
	// the radars it is missing. Each rail answers one question.
	//
	// Null when no participation check is registered, which is every
	// deployment without a mount.
	const compositeInput = $derived.by(() => {
		const rows = (sentinel.rollup?.stages?.LB3 ?? [])
			.filter((r) => r.check_id.startsWith('layer3.composite.'))
			.filter(shown);
		if (!rows.length) return null;
		const short = rows.filter((r) => isAlerting(r.status))
			.slice().sort((a, b) => a.target.localeCompare(b.target));
		return {
			total: rows.length,
			clean: rows.length - short.length,
			// Each id with its own status word. A radar absent from the run and
			// one whose contribution went stale are different faults, and the
			// collapsed color cannot tell them apart.
			detail: short.map((r) => `${r.target} ${r.status}`).join(', '),
			worst: worstOf(rows.map((r) => r.status))
		};
	});

	// === Image quality stopped being a count ===
	//
	// L4 ran nine checks that reached the page as the number 9/9 in the top
	// strip and a 6px dot on six of the radar rows. Everything it measures was
	// discarded on the way: the echo coverage, the frame's self-similarity,
	// and which of its four detectors fired. Six of those nine targets are
	// radars that already have a row, so the numbers belong on it, and the
	// other three are mosaics that had nowhere to appear at all.
	const l4Rows = $derived(
		(sentinel.rollup?.stages?.['L4-T1T2'] ?? [])
			.filter(shown).sort((a, b) => a.target.localeCompare(b.target))
	);
	const l4ByTarget = $derived.by(() => {
		const out: Record<string, (typeof l4Rows)[number]> = {};
		for (const r of l4Rows) out[r.target] = r;
		return out;
	});
	/** The quality rail's own rows: the mosaics, which no other rail carries. */
	const l4MosaicRows = $derived(
		l4Rows.filter((r) => !r.check_id.startsWith('layer4.xband.'))
	);

	// L3 compares what the console draws against what the API says, and it had
	// no row anywhere. One check today, so a rail of its own would be mostly
	// heading; it sits under the quality rows, which is the same question
	// asked of the rendered product rather than of the image.
	const l3Rows = $derived(
		(sentinel.rollup?.stages?.L3 ?? [])
			.filter(shown).sort((a, b) => a.target.localeCompare(b.target))
	);

	/** The newest value of one check's metric, or null when none has landed. */
	function latestMetric(checkId: string, metric: string | null): number | null {
		if (!metric) return null;
		const arr = sentinel.metrics[`${checkId}|${metric}`];
		if (!arr?.length) return null;
		return arr[arr.length - 1].value;
	}

	// Re-read on the same tick as everything else, so a countdown does not sit
	// frozen at whatever it said when the page loaded.
	const nowTick = $derived(sentinel.rollup?.at ?? '');

	/**
	 * How often a check runs, and when it is next due, as one hover line.
	 *
	 * An age alone cannot be judged. 55 minutes is a fault on a 2-minute radar
	 * and routine on a 30-minute forecast, and a row past its cadence is a
	 * check that is slow or stuck rather than a subject that is stale.
	 */
	function cadenceTitle(checkId: string): string {
		const every = fmtCadence(checksById[checkId]?.cadence_s);
		if (!every) return '';
		void nowTick;
		const row = allRowsById[checkId];
		const left = secondsToNextRun(row?.finished_at, checksById[checkId]?.cadence_s);
		if (left === null) return `sampled every ${every}`;
		return left >= 0
			? `sampled every ${every} · next in ${fmtAge(left)}`
			: `sampled every ${every} · overdue by ${fmtAge(-left)}`;
	}
	/** Every reading on the page, by check id, for the cadence hover. */
	const allRowsById = $derived.by(() => {
		const out: Record<string, { finished_at?: string }> = {};
		for (const rows of Object.values(sentinel.rollup?.stages ?? {})) {
			for (const r of rows ?? []) out[r.check_id] = r;
		}
		return out;
	});

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

	const keptAlarms = $derived(sentinel.alarms.filter(shown));
	// The header keeps the true count, so the band says how much of it the
	// source filters hold back. Without it a filtered band reads as a lie.
	const filteredAlarmCount = $derived(sentinel.alarms.length - keptAlarms.length);
	const ranking = $derived(rankAlarms(keptAlarms, { showAcked, limit: ALARM_ROWS }));
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
  welded to the trace survive both. The backend tag is the deployment's own
  (config.BACKEND_SOURCE, or SPECIAL_TREE_SOURCE for CBAND); RC = radarca, the
  scraped upstream.

  A null `row` means this source does not cover this target on this
  deployment — no backend mount at all, CBAND without SENTINEL_SSCB_ROOT, or a
  vector/stream feed that has no backend counterpart. Saying so is deliberate:
  a blank cell reads as a failure.
-->
{#snippet sourceCell(row: any, fallbackTag: string, paired: boolean, w: number)}
	{#if !row}
		<span
			class="num justify-self-start whitespace-nowrap text-[12px] text-[var(--color-faint)]"
			title="not monitored on this deployment — the backend tree is not mounted for this target"
		>
			not mounted
		</span>
	{:else}
		{@const metric = sparklineMetric(row.check_id)}
		{@const spark = sentinel.metrics[`${row.check_id}|${metric ?? 'age_s'}`] ?? []}
		{@const meta = checksById[row.check_id]}
		{@const cadenceS = meta?.cadence_s ?? 120}
		<!-- The check says which tree it read. A tag baked in here was wrong
		     for CBAND, which comes off its own tree. See Check.source_tag. -->
		{@const tag = meta?.source_tag || fallbackTag}
		<span
			class="flex min-w-0 items-center gap-1.5 {statusText(row.status)}"
			title={`${row.check_id} · ${row.target}${meta?.source_label ? ` · read from ${meta.source_label}` : ''}${row.summary ? `\n${row.summary}` : ''}`}
		>
			<StatusDot status={row.status} size={8} pulseKey={sentinel.pulseTick[row.check_id] ?? 0} />
			{#if paired}
				<span class="num shrink-0 rounded border border-[var(--color-border-strong)] px-[3px] text-[12px] leading-[1.5] tracking-[0.06em] text-[var(--color-faint)]">{tag}</span>
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

<!--
  Four rails, no map, one row.

  The locator map was the largest element on the page and, on a healthy
  deployment, the one carrying the least: nine green pins restating nine green
  rows. The console at / draws the same geography with the imagery on it, so
  the dashboard gives the space to the readings instead.

  What the space bought: Radars left a shared column with Site and took one of
  its own, which is what let its rows carry numbers rather than a bare trace.
  Image quality and overlay parity got rows at all — ten checks that reached
  the page only as two counts in the top strip. Alarms moved out of a
  full-width band at the foot, which held one line of text and cost every rail
  its last 36vh, into the fourth column under the quality rows.
-->
<!-- items-start, so a rail ends where its content does. Stretching them to a
     common height left Site and Radars as tall empty boxes, which reads as
     something failing to load rather than as a rail with nothing more to say.
     Each still takes max-h-full and scrolls, so a long rail is bounded by the
     viewport rather than by its neighbors. -->
<!-- The rails take whatever height the alarm band leaves, and the band sits
     below them at the foot of the page. In one grid with the rails, the band
     followed the tallest rail's content up and down the page as rows
     expanded and collapsed. -->
<!-- The page scrolls as a whole once the rails reach their minimum height.
     Held sideways (734 x 343), a phone squeezed the rails to nothing and
     showed only the alarm band.
     Below lg the rails stack in one column at their full height, and only
     the page scrolls. Bounded there by the viewport, each rail took a
     quarter of a phone and showed its header and one clipped row. -->

<div class="flex h-full flex-col gap-2 overflow-y-auto p-2">
<h1 class="sr-only">Dashboard</h1>
<!-- Source filters. Each appears only where the backend classifies some
     checks that way, so XQPI, which reads no RadarCA and watches no NEXRAD
     radar, shows neither. -->
{#if sourceCount.radarca > 0 || sourceCount.nexrad > 0}
	<div data-dashboard-filters
		class="flex shrink-0 items-center justify-end gap-4 px-1 text-[12px]">
		{#if sourceCount.radarca > 0}
			<label
				data-hide-radarca
				class="flex items-center gap-1 uppercase tracking-wider text-[var(--color-muted)]"
				title={`Hide the ${sourceCount.radarca} checks whose readings come from RadarCA, from every rail and the alarm band. Pairs keep their backend half.`}
			>
				<input type="checkbox" bind:checked={hideRadarca} class="accent-[var(--color-ok)]" />
				<span>hide RadarCA{hideRadarca ? ` (${sourceCount.radarca})` : ''}</span>
			</label>
		{/if}
		{#if sourceCount.nexrad > 0}
			<label
				data-hide-nexrad
				class="flex items-center gap-1 uppercase tracking-wider text-[var(--color-muted)]"
				title={`Hide the ${sourceCount.nexrad} checks that watch a NEXRAD radar, from every rail and the alarm band. Their alarms never notify either way.`}
			>
				<input type="checkbox" bind:checked={hideNexrad} class="accent-[var(--color-ok)]" />
				<span>hide NEXRAD{hideNexrad ? ` (${sourceCount.nexrad})` : ''}</span>
			</label>
		{/if}
	</div>
{/if}
<div data-rails class="grid flex-1 grid-cols-12 items-start gap-2 lg:min-h-[18rem] lg:overflow-y-auto">
	<!-- RAIL 1: SITE ------------------------------------------------------------------- -->
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
	<!-- Each rail scrolls, so each takes focus for a keyboard (WCAG 2.1.1,
	     axe scrollable-region-focusable) and a name for a screen reader.
	     Svelte's noninteractive-tabindex rule does not know scroll regions,
	     hence the ignores on the four rails. -->
	<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
	<aside
		class="panel col-span-12 flex flex-col lg:col-span-3 lg:max-h-full lg:overflow-y-auto"
		tabindex="0"
		aria-label="Site checks"
	>
		<SectionHeader title="Site" count="{siteRows.filter((r) => r.status === 'pass').length}/{siteRows.length}" right={stageLabel('L0')} />
		<ul class="shrink-0 divide-y divide-[var(--color-border)]">
			{#each siteRows as r}
				<!-- The summary is clipped here too, but these are one-line L0
				     rows in a narrow rail and the full text is on the row's hover
				     title rather than nowhere. Left single-line on purpose:
				     eleven site rows that each wrap to two would fill the
				     column. -->
				<li class="row-hover flex items-center gap-2 px-3 py-1.5 text-[12px]"
					title={`${prettyCheckLabel(r.check_id, r.target)} · ${r.check_id}\n${cadenceTitle(r.check_id)}${r.summary ? `\n${r.summary}` : ''}`}>
					<StatusDot status={r.status} size={7} pulseKey={sentinel.pulseTick[r.check_id] ?? 0} />
					<!-- The label never wraps and the summary gives way instead.
					     In the narrower column "Sentinel DNS" broke across two
					     lines while the summary beside it kept its full width,
					     so the name of the thing was harder to read than its
					     detail. The summary's full text is on the hover title. -->
					<span class="num shrink-0 whitespace-nowrap text-[var(--color-default)]">{prettyCheckLabel(r.check_id, r.target)}</span>
					<span class="num ml-auto min-w-0 truncate text-[12px] text-[var(--color-muted)]">{r.summary}</span>
					<!-- How often this is sampled. The L0 rows carry no age, so
					     the cadence is the only thing saying whether a summary
					     from "just now" means thirty seconds or an hour. -->
					<span class="num w-[1.9rem] shrink-0 text-right text-[12px] text-[var(--color-faint)]">
						{fmtCadence(checksById[r.check_id]?.cadence_s)}
					</span>
				</li>
			{/each}
			{#if dropsRow}
				<!-- Informational by design: nothing in the live product chain
				     reads the DROPS tree, so this never pages (see alerts.yaml
				     and Layer3DropsProducerCheck). Shown even when healthy,
				     because being unable to see that it IS running is exactly
				     the gap that let it die unnoticed for twelve hours. -->
				<li class="row-hover flex items-center gap-2 px-3 py-1.5 text-[12px]"
					title={`${prettyCheckLabel(dropsRow.check_id, dropsRow.target)} · ${dropsRow.check_id} — informational, non-paging${dropsRow.summary ? `\n${dropsRow.summary}` : ''}`}>
					<StatusDot status={dropsRow.status} size={7} pulseKey={sentinel.pulseTick[dropsRow.check_id] ?? 0} />
					<!-- Via prettyCheckLabel, not hardcoded. Hardcoding it is how
					     this check ended up with two names in one app: the label
					     function said "DROPS producer" and this row said "QPE
					     producer". One source for the label means they cannot
					     disagree again. -->
					<span class="text-[var(--color-default)] num">{prettyCheckLabel(dropsRow.check_id, dropsRow.target)}</span>
					<span class="num shrink-0 rounded border border-[var(--color-border-strong)] px-[3px] text-[12px] leading-[1.5] tracking-[0.06em] text-[var(--color-faint)]">INFO</span>
					<span class="ml-auto truncate text-[var(--color-muted)] num text-[12px]">{dropsRow.summary}</span>
				</li>
			{/if}
		</ul>
	</aside>

	<!-- RAIL 2: RADARS ----------------------------------------------------------------- -->
	<!-- Its own column now. In the shared one it had room for a name, a dot
	     and a 62px trace, so the rail could say that a radar had moved and
	     not what it had moved to. The width buys four readings per row: the
	     arrival age, its trace, the frame's echo coverage and which image-QC
	     detector, if any, fired. -->
	<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
	<aside
		class="panel col-span-12 flex flex-col lg:col-span-3 lg:max-h-full lg:overflow-y-auto"
		tabindex="0"
		aria-label="Radars"
	>
		<div class="flex items-center gap-3 border-b border-[var(--color-border)] px-3 py-1.5">
			<span class="label">Radars</span>
			<span class="num text-[12px] text-[var(--color-faint)]">·  {radarRows.filter((r) => r.status === 'pass').length}/{radarRows.length}</span>
			<!-- One control, two states. Two buttons would leave one of them
			     always a no-op, and the label says what the click will DO
			     rather than what the rail currently is. -->
			<button
				type="button"
				class="ml-auto num text-[12px] uppercase tracking-[0.1em] text-[var(--color-muted)] hover:text-[var(--color-bright)]"
				onclick={() => setAllRadars(!allRadarsOpen)}
				title={allRadarsOpen ? 'collapse every radar' : 'expand every radar'}
			>
				{allRadarsOpen ? 'Collapse all' : 'Expand all'}
			</button>
			<span class="num text-[12px] text-[var(--color-muted)]">{sourceLabelFor(sources.radars, 'L2', 'LB2')}</span>
		</div>
		{#if fleetAlerting && fleetRow}
			<!-- The fleet verdict, where it belongs: above the radars it is a
			     statement about, and only while it is making one. Everything
			     below is explained by this one line, which is the whole point
			     of the check — one upstream event, not six radar outages. -->
			<div
				data-fleet-banner
				class="flex items-start gap-2 border-b border-[var(--color-border)] bg-[var(--color-fail)]/10 px-3 py-2"
				title={fleetRow.check_id}
			>
				<!-- shrink-0: the dot is a flex child in an items-start row and
				     gets squashed into a bar without it. -->
				<span class="mt-[3px] shrink-0"><StatusDot status={fleetRow.status} size={8} /></span>
				<div class="min-w-0">
					<div class="label text-[12px] tracking-[0.14em] {statusText(fleetRow.status)}">
						{fleetRow.check_id === BACKEND_FLEET_CHECK_ID ? 'Radar fleet · backend' : 'X-band fleet'}
					</div>
					<div class="num text-[12px] leading-snug text-[var(--color-default)]">
						{fleetRow.summary}
					</div>
				</div>
			</div>
		{/if}
		<!-- Cards, not rows.
		     A row gave each radar one line, and a radar now carries five
		     readings: the arrival age, its trace, the echo coverage, which
		     image-QC detector fired, and how many checks are behind the
		     verdict. That is more than a line holds without truncating, which
		     is how the old row ended up showing a bare sparkline and a 6px dot.
		     The column also had the height to spare, since the radars are the
		     shortest rail on the page.
		     Every card lays its readings out identically and in the same
		     places, so the eye can still run down the column and compare one
		     radar's coverage against another's. That vertical scan is what a
		     card grid usually costs, and the fixed positions are what buy it
		     back.
		     Two columns from xl. One column fits six cards in about 750px,
		     which is taller than the rail at 1366 and put the last radar
		     behind a scroll; two columns fit all six without one. Below xl
		     the rail is around 256px, where a second column would leave each
		     card about 110px and put every number on its own line. -->
		<ul class="grid grid-cols-1 gap-2 p-2 xl:grid-cols-2">
			{#each radarGroups as g (g.target)}
				{@const imgQc = l4ByTarget[g.target] ?? null}
				{@const open = radarIsOpen(g.target, g.alerting)}
				{@const worstRow = g.rows.find((r) => r.status === g.worst) ?? g.rows[0]}
				{@const arrival = arrivalRowOf(g.rows)}
				{@const ageS = arrival ? latestMetric(arrival.check_id, readoutMetric(arrival.check_id)) : null}
				{@const cov = imgQc ? latestMetric(imgQc.check_id, 'coverage_pct') : null}
				{@const qc = imgQc ? parseImageQc(imgQc.summary) : []}
				{@const fired = qc.filter((v) => !v.ok)}
				{@const arrivalMetric = arrival ? sparklineMetric(arrival.check_id) : null}
				<!-- min-w-0 and overflow-hidden are the backstop. A grid item
				     defaults to min-width:auto, so one child that refuses to
				     shrink widens the card past its track, and anything drawn
				     at that width crosses the border the card is wearing. -->
				<li
					class="flex min-w-0 flex-col gap-1.5 overflow-hidden rounded border p-2
					       {g.alerting
							? 'border-[var(--color-fail)]/40 bg-[var(--color-fail)]/[0.04]'
							: 'border-[var(--color-border)] bg-[var(--color-canvas)]/30'}"
				>
					<!-- The whole header toggles, and it is a real button with
					     aria-expanded: these rails are read with a keyboard
					     during an incident. -->
					<button
						type="button"
						class="flex min-h-6 w-full items-center gap-1.5 text-left"
						aria-expanded={open}
						onclick={() => toggleRadar(g.target, g.alerting)}
						title={`${g.target} · ${g.rows.length} check${g.rows.length === 1 ? '' : 's'}`
							+ (worstRow?.summary ? `\n${worstRow.summary}` : '')}
					>
						<StatusDot
							status={g.worst}
							size={8}
							pulseKey={sentinel.pulseTick[arrival?.check_id ?? ''] ?? 0}
						/>
						<span class="num tracking-wide text-[13px] text-[var(--color-bright)]">{g.target}</span>
						<!-- The check count lives in the title. At 12 px it left
						     the name about 20 px in a 150 px card. -->
						<span class="ml-auto flex"><DisclosureArrow {open} /></span>
					</button>

					<!-- The arrival age, large, because it is the reading that
					     decides whether anything else on the card matters. The
					     cadence sits beside it: 55 minutes is a fault on a
					     2-minute radar and routine on a 30-minute forecast. -->
					<div
						class="flex items-baseline gap-1.5"
						title={arrival ? `${stageLabel(arrival.stage)} · ${cadenceTitle(arrival.check_id)}` : ''}
					>
						<span class="num text-[16px] leading-none {statusText(arrival?.status ?? 'skip')}">
							{ageS !== null ? fmtAge(ageS, { signed: true }) : '—'}
						</span>
						{#if arrival}
							<span class="num text-[12px] text-[var(--color-faint)]">
								every {fmtCadence(checksById[arrival.check_id]?.cadence_s)}
							</span>
						{/if}
					</div>

					{#if arrival && diag.spark}
						<!-- Data arrival, specifically: LB2 where it exists. LB3
						     measures a composite receipt written two steps later and
						     L2 measures what radarca says, so plotting whichever came
						     first would make the trace mean a different thing on
						     different cards.
						     Fluid, so the trace is as wide as the card rather than a
						     62px stub with the rest of the card empty beside it. -->
						<Sparkline
							data={sentinel.metrics[`${arrival.check_id}|${arrivalMetric ?? 'age_s'}`] ?? []}
							cadenceS={checksById[arrival.check_id]?.cadence_s ?? 120}
							width={180}
							height={22}
							domain={sparklineDomain(arrivalMetric)}
							warnAt={sparklineWarnAt(arrivalMetric)}
							showLabel={false}
							fluid
						/>
					{/if}

					<!-- Image quality, which the page used to discard. A detector
					     that fired gets its own token, so a card says WHICH one;
					     the ones that passed fold into "QC ok", since four faint
					     names at 12 px are wider than the card. -->
					<!-- Wraps, because the card's width comes from a grid that
					     reflows: at the narrow end the tokens go to their own
					     line rather than squeezing the coverage figure out. -->
					<div class="flex flex-wrap items-center gap-x-1.5 gap-y-1 border-t border-[var(--color-border)]/60 pt-1.5">
						{#if imgQc}
							<span
								class="num text-[12px] text-[var(--color-muted)]"
								title={`${imgQc.check_id} · ${cadenceTitle(imgQc.check_id)}\n${imgQc.summary}`}
							>
								cov {cov !== null ? `${cov.toFixed(1)}%` : '—'}
							</span>
							<span
								class="ml-auto flex flex-wrap items-center gap-1"
								title={`image QC · ${imgQc.summary}`}
							>
								{#each fired as v (v.name)}
									<span
										class="num rounded bg-[var(--color-fail)]/15 px-[3px] text-[12px] leading-[1.6] text-[var(--color-fail)]"
										title={`${v.name}=${v.verdict}`}
									>{v.name}</span>
								{:else}
									{#if qc.length}
										<span class="num text-[12px] text-[var(--color-faint)]">QC ok</span>
									{/if}
								{/each}
							</span>
						{:else}
							<span class="num text-[12px] text-[var(--color-faint)]">no image check</span>
						{/if}
					</div>

					{#if open}
						<!-- Every reading behind the card's verdict, with its own
						     words untruncated. The card above carries the numbers;
						     this carries what each check actually said. -->
						<div class="flex flex-col gap-1 border-t border-[var(--color-border)]/60 pt-1.5">
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
									class="flex flex-col gap-0.5 text-[12px]"
									title={`${r.check_id}${meta?.source_label ? ` · read from ${meta.source_label}` : ''}`}
								>
									<!-- Nothing in this row may refuse to shrink. Both the
									     label and the trace were shrink-0, and in a 222px
									     card "BACKEND RADAR ARRIVAL" plus a dot, a source
									     tag and a 62px trace needs 228px, so the trace was
									     drawn 28px outside the card's own border. The rail
									     on the test deployment has one check per radar with
									     a short stage name, which is why it fitted there
									     and nowhere else. -->
									<div class="flex min-w-0 items-center gap-1.5">
										<span class="num min-w-0 truncate text-[12px] uppercase tracking-[0.08em] text-[var(--color-faint)]">
											{stageLabel(r.stage)}
										</span>
										<StatusDot status={r.status} size={7} pulseKey={sentinel.pulseTick[r.check_id] ?? 0} />
										{#if meta?.source_tag}
											<span class="num shrink-0 rounded border border-[var(--color-border-strong)] px-[3px] text-[12px] leading-[1.5] tracking-[0.06em] text-[var(--color-faint)]">{meta.source_tag}</span>
										{/if}
										{#if diag.spark}
											<span class="ml-auto w-[62px] min-w-0 shrink">
												<Sparkline
													data={sentinel.metrics[`${r.check_id}|${metric ?? 'age_s'}`] ?? []}
													cadenceS={meta?.cadence_s ?? 120}
													width={62}
													height={12}
													domain={sparklineDomain(metric)}
													warnAt={sparklineWarnAt(metric)}
													showLabel={false}
													fluid
												/>
											</span>
										{/if}
									</div>
									<!-- break-words, not truncate: these summaries carry
									     the threshold in parentheses at the END, which is
									     the half a clipped line always loses. -->
									<span class="num break-words text-[12px] leading-snug {statusText(r.status)}">
										{r.summary}
									</span>
								</li>
							{/each}
						</ul>
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	</aside>

	<!-- RAIL 3: PRODUCTS --------------------------------------------------------------- -->
	<aside
		class="panel col-span-12 flex flex-col lg:col-span-4 lg:max-h-full lg:overflow-hidden"
		aria-label="Products"
	>
		<SectionHeader title="Products" count="{productRows.filter((r) => r.status === 'pass').length}/{productRows.length}" right={sourceLabelFor(sources.products, 'L1', 'LB1')} />
		{#if pairedProducts}
			<div class="grid grid-cols-[1fr_auto_auto] items-end gap-2 border-b border-[var(--color-border)] px-3 py-1 text-[12px] uppercase tracking-[0.1em] text-[var(--color-muted)]">
				<span></span><span>Filesystem</span><span>RadarCA</span>
			</div>
		{/if}
		<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
		<div class="lg:overflow-y-auto" tabindex="0" role="region" aria-label="Product list">
			{#each productGroups as g}
				{@const gc = groupCount(g.rows)}
				{@const hidden = g.rows.length === 0 ? (productsBeforeFilters[g.category] ?? 0) : 0}
				<div class="border-b border-[var(--color-border)] bg-[var(--color-canvas)]/40 px-3 py-1 text-[12px] uppercase tracking-[0.16em] text-[var(--color-muted)] flex items-center gap-2">
					<span>{g.label}</span>
					<span class="num text-[12px] text-[var(--color-faint)] ml-auto">
						{g.rows.length > 0 ? `${gc.pass}/${gc.total}` : hidden ? 'filtered' : 'not exposed upstream'}
					</span>
				</div>
				{#if g.rows.length === 0}
					<div data-group-empty class="px-3 py-1.5 text-[12px] text-[var(--color-faint)] italic">
						{hidden
							? `${hidden} product${hidden === 1 ? '' : 's'} hidden by the source filters.`
							: 'no products in this group.'}
					</div>
				{:else}
					<ul class="divide-y divide-[var(--color-border)]">
						{#each g.rows as p (p.target)}
							{@const lone = loneOf(p)}
							{@const ageS = lone ? latestMetric(lone.check_id, 'age_s') : null}
							<li class="row-hover {pairIsBad(p) ? 'row-bad' : ''} grid {pairedProducts ? 'grid-cols-[1fr_auto_auto]' : 'grid-cols-[1fr_4.5rem_auto]'} items-center gap-2 px-3 py-1.5 text-[12px]">
								<!-- Named off whichever reading exists. prettyCheckLabel
								     suffixes "· backend" for layer1.backend.*, which is
								     right in a paired rail and wrong when the backend IS
								     the only source, so an unpaired row uses the bare
								     product label. -->
								<span class="num truncate text-[var(--color-bright)]"
									  title={lone ? `${lone.check_id} · ${p.target}\n${cadenceTitle(lone.check_id)}` : p.target}>
									{pairedProducts && p.primary
										? prettyCheckLabel(p.primary.check_id, p.target)
										: productLabel(p.target)}
								</span>
								{#if pairedProducts}
									{@render sourceCell(p.backend, 'FS', true, 64)}
									{@render sourceCell(p.primary, 'RC', true, 64)}
								{:else}
									<!-- Unpaired: keep the age readout this rail has always
									     had. It only loses its column when the second source
									     needs the room. -->
									<span class="num text-right text-[12px] {statusText(lone?.status ?? 'skip')}">
										{ageS !== null ? fmtAge(ageS, { signed: true }) : '—'}
									</span>
									{@render sourceCell(lone, 'RC', false, 56)}
								{/if}
							</li>
							{#if compositeInput && compositeProduct && p.target === compositeProduct}
								<!-- The composite's input shortfall, on the composite's own
								     row. A radar arriving on time with no block in the
								     latest run is the composite running short, so the radar
								     rail and the map pin keep their arrival verdict and this
								     is where the fault is named. -->
								<li class="row-hover grid grid-cols-[1fr_auto] items-center gap-2
								           py-1 pl-6 pr-3 text-[12px]"
									title="layer3.composite.* · {compositeInput.total} expected radars">
									<span class="num truncate {statusText(compositeInput.worst)}">
										radar input · {compositeInput.clean}/{compositeInput.total}{
											compositeInput.detail ? ` · ${compositeInput.detail}` : ''}
									</span>
									<StatusDot status={compositeInput.worst} size={7} />
								</li>
							{/if}
						{/each}
					</ul>
				{/if}
			{/each}
		</div>
	</aside>

	<!-- RAIL 4: QUALITY ----------------------------------------------------------------- -->
	<!-- The two stages that had no row anywhere. L4 reached the page as
	     "IMAGE QUALITY 9/9" in the top strip and a dot on six radar rows;
	     L3 reached it as "MAP OVERLAYS 1/1" and nothing else. Six of L4's
	     nine targets are radars and their numbers now sit on the radar
	     cards, so what is left here is the three mosaics, which belong to
	     no other rail. -->
	<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
	<aside
		class="panel col-span-12 flex flex-col lg:col-span-2 lg:max-h-full lg:overflow-y-auto"
		tabindex="0"
		aria-label="Quality"
	>
			<!-- No source label on the right: the rail has one source, and
			     "Image Quality" repeated the title and misnamed its L3 row. -->
			<SectionHeader
				title="Quality"
				count="{[...l4Rows, ...l3Rows].filter((r) => r.status === 'pass').length}/{l4Rows.length + l3Rows.length}"
			/>
			<ul class="divide-y divide-[var(--color-border)]">
				{#each l4MosaicRows as r (r.check_id)}
					{@const cov = latestMetric(r.check_id, 'coverage_pct')}
					{@const qc = parseImageQc(r.summary)}
					<li
						class="row-hover {isAlerting(r.status) ? 'row-bad' : ''} flex flex-col gap-0.5 px-3 py-1.5 text-[12px]"
						title={`${r.check_id} · ${cadenceTitle(r.check_id)}\n${r.summary}`}
					>
						<div class="flex items-center gap-1.5">
							<StatusDot status={r.status} size={7} pulseKey={sentinel.pulseTick[r.check_id] ?? 0} />
							<span class="num truncate text-[var(--color-default)]">{productLabel(r.target)}</span>
							<span class="num ml-auto shrink-0 text-[12px] {statusText(r.status)}">{qcLabel(qc)}</span>
						</div>
						<div class="flex items-center gap-1.5 pl-[13px]">
							<span class="num text-[12px] text-[var(--color-muted)]">
								{cov !== null ? `${cov.toFixed(1)}%` : '—'}
							</span>
							{#if diag.spark}
								<span class="ml-auto shrink-0">
									<Sparkline
										data={sentinel.metrics[`${r.check_id}|coverage_pct`] ?? []}
										cadenceS={checksById[r.check_id]?.cadence_s ?? 300}
										width={56}
										height={12}
										domain={null}
										warnAt={null}
										showLabel={false}
									/>
								</span>
							{/if}
						</div>
					</li>
				{/each}
				{#each l3Rows as r (r.check_id)}
					<!-- Overlay parity: what the console drew against what the API
					     says it should have. A different question from the image
					     checks above, and the only stage with no rail of its own. -->
					<li
						class="row-hover {isAlerting(r.status) ? 'row-bad' : ''} flex items-center gap-1.5 px-3 py-1.5 text-[12px]"
						title={`${r.check_id} · ${cadenceTitle(r.check_id)}\n${r.summary}`}
					>
						<StatusDot status={r.status} size={7} pulseKey={sentinel.pulseTick[r.check_id] ?? 0} />
						<span class="num truncate text-[var(--color-default)]">{productLabel(r.target)}</span>
						<span class="num ml-auto shrink-0 text-[12px] text-[var(--color-faint)]" title={stageLabel('L3')}>
							{(r.summary ?? '').includes('verdict=MATCH') ? 'MATCH' : stageLabel('L3')}
						</span>
					</li>
				{/each}
				{#if l4Rows.length === 0 && l3Rows.length === 0}
					<li class="px-3 py-2 text-[12px] italic text-[var(--color-faint)]">
						no image or overlay checks on this deployment.
					</li>
				{/if}
			</ul>
	</aside>
</div>

	<!-- ALARM STREAM, full width at the foot of the page --------------------------------- -->
	<!-- An alarm row is a severity chip, a stage, an id, the subject, the
	     time it opened, its age, the message and an ack button: eight
	     columns needing around 500px before anything truncates. It spent one
	     release in the 2/12 rail beside Quality, where it had 310px, and the
	     ack control was the part that gave way. A row this shape wants the
	     page's width, so it takes the full span below the four rails.
	     It is as tall as its rows, up to 36vh, so a quiet page gives the
	     rails everything but a header's height. -->
	<section data-alarm-band class="panel flex max-h-[36vh] shrink-0 flex-col overflow-hidden">
		<SectionHeader
			title="Alarms"
			count="{sentinel.alarms.length} open"
			right={sentinel.alarms.length
				? `oldest ${fmtAge(Math.max(...sentinel.alarms.map((a) => (Date.now() - new Date(a.opened_at).getTime()) / 1000)))}`
				: 'all clear'}
		/>
		{#if sentinel.alarms.length}
			<!-- Spaced so that, wrapped on a phone, the checkbox and the link
			     sit outside each other's 24 px target (WCAG 2.2 2.5.8). -->
			<div class="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-[var(--color-border)] px-4 py-1 text-[12px] uppercase tracking-wider text-[var(--color-faint)]">
				<span>most severe first · top {ALARM_ROWS}</span>
				{#if filteredAlarmCount}
					<span data-alarms-filtered={filteredAlarmCount} class="text-[var(--color-muted)]">
						{filteredAlarmCount} hidden by the source filters
					</span>
				{/if}
				{#if ackedCount}
					<label class="flex cursor-pointer items-center gap-1.5 text-[var(--color-muted)] hover:text-[var(--color-bright)]">
						<input type="checkbox" bind:checked={showAcked} class="h-4 w-4 shrink-0 accent-[var(--color-ok)]" />
						<span>show acknowledged ({ackedCount})</span>
					</label>
				{/if}
				<span class="ml-auto normal-case tracking-normal">
					{#if overflowCount}
						<a class="inline-flex min-h-6 items-center text-[var(--color-muted)] underline decoration-dotted hover:text-[var(--color-bright)]"
						   href="/history?tab=alarms">+{overflowCount} more not shown</a>
					{:else}
						<a class="inline-flex min-h-6 items-center text-[var(--color-faint)] hover:text-[var(--color-bright)]"
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
					data-alarm-row
					class="row-hover sev-bar grid grid-cols-[2.5rem_6.5rem_3.5rem_12rem_auto_auto_1fr_auto] items-center gap-3 px-4 py-1.5 text-[12px] {isAcked ? 'opacity-50' : ''} {sevColor[a.severity] ??
						''}"
					title={isAcked ? `acked by ${a.ack?.acked_by} at ${a.ack?.acked_at}${a.ack?.note ? ' — ' + a.ack.note : ''}` : ''}
				>
					<span class={severityChip(a.severity)}>{a.severity}</span>
					<!-- truncate, and a column wide enough for the longest stage
					     name. "Connectivity" needs about 70px and the column was
					     51px, so it ran into the id beside it and the row read
					     "Connectivi#28200". -->
					<span class="num truncate text-[12px] text-[var(--color-muted)]" title={a.stage}>{stageLabel(a.stage)}</span>
					<span class="num text-[var(--color-bright)]">#{a.id}</span>
					<span class="text-[var(--color-default)] truncate" title={a.target}>{prettyCheckLabel(a.check_id, a.target)}</span>
					<span
						class="num text-[12px] text-[var(--color-muted)]"
						title="opened {a.opened_at}"
					>
						{openedZ}
					</span>
					<span class="num text-[12px] text-[var(--color-faint)]">
						{fmtAge(ageS)}
					</span>
					<span class="truncate text-[var(--color-muted)] text-[12px]">
						{#if isAcked}
							<span class="text-[12px] uppercase tracking-wider text-[var(--color-ok)] mr-1">acked · {a.ack?.acked_by}</span>
						{/if}
						{a.message}
					</span>
					{#if !auth.user}
						<a
							class="border border-[var(--color-border-strong)] px-2 py-0.5 text-[12px] uppercase tracking-wider text-[var(--color-faint)] hover:text-[var(--color-bright)]"
							href="/login?next={encodeURIComponent('/')}"
							title="sign in to acknowledge alarms"
						>
							sign in
						</a>
					{:else if isAcked}
						<button
							type="button"
							class="border border-[var(--color-border-strong)] px-2 py-0.5 text-[12px] uppercase tracking-wider text-[var(--color-muted)] hover:bg-[var(--color-elevated)] hover:text-[var(--color-bright)]"
							onclick={() => unack(a.id)}
							title="undo acknowledgment"
						>
							unack
						</button>
					{:else}
						<button
							type="button"
							class="border border-[var(--color-border-strong)] px-2 py-0.5 text-[12px] uppercase tracking-wider text-[var(--color-default)] hover:bg-[var(--color-elevated)] hover:text-[var(--color-bright)]"
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
				<!-- Open alarms exist but none has a row, because each is
				     acknowledged or held back by a source filter. Say which,
				     since "no open alarms" here would be a lie. -->
				<li data-alarms-empty class="px-4 py-3 text-[12px] text-[var(--color-faint)]">
					{#if ackedCount}
						{ackedCount} open alarm{ackedCount === 1 ? ' is' : 's are'} acknowledged,
						<button type="button" class="underline decoration-dotted hover:text-[var(--color-bright)]"
						        onclick={() => (showAcked = true)}>show {ackedCount === 1 ? 'it' : 'them'}</button>.
					{/if}
					{#if filteredAlarmCount}
						{filteredAlarmCount} open alarm{filteredAlarmCount === 1 ? ' is' : 's are'}
						hidden by the source filters above.
					{/if}
				</li>
			{/if}
		</ul>
	</section>
</div>
