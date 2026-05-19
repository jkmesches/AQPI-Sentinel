<script lang="ts">
	/** /m/uptime — vertical version of the desktop timeline grid.
	 *
	 *  Rows = time buckets, newest at the top. Columns = checks, grouped
	 *  per subtab (Connectivity / Products / Radar). The grid scrolls
	 *  horizontally when columns overflow the viewport so the data model
	 *  stays identical to the desktop view — one square per (check, bucket).
	 *
	 *  Tap a cell to drill into what happened in that bucket for that
	 *  check (reuses MobileDrillDown).
	 */
	import { onMount, onDestroy, untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { api, type CheckMeta, type CheckRun, type TimelineBucket } from '$lib/api';
	import {
		prettyCheckLabel, stageLabel, productCategory,
		PRODUCT_CATEGORY_ORDER, PRODUCT_CATEGORY_LABEL
	} from '$lib/format';
	import MobileDrillDown from '$lib/components/mobile/MobileDrillDown.svelte';
	import LazyImage from '$lib/components/LazyImage.svelte';
	import { url as apiUrl } from '$lib/origin';

	// Pull the captured-PNG source out of a check_run payload. L4 image-
	// quality checks set `payload.source` (legacy: `image_source`). Other
	// checks don't carry imagery so this returns null and the caller skips
	// rendering. Mirror of HistoryDetailModal.capturedImage on desktop —
	// keep the two definitions aligned if the L4 module changes the key.
	function capturedImage(run: any): string | null {
		const src = run?.payload?.source ?? run?.payload?.image_source;
		if (!src) return null;
		return apiUrl(`/api/upstream/image_by_source.png?source=${encodeURIComponent(src)}`);
	}

	type Bucket = '1m' | '5m' | '15m' | '1h';
	const BUCKETS: { key: Bucket; label: string; pageLimit: number; seconds: number }[] = [
		{ key: '1m',  label: '1m',  pageLimit: 30, seconds: 60 },
		{ key: '5m',  label: '5m',  pageLimit: 60, seconds: 300 },
		{ key: '15m', label: '15m', pageLimit: 60, seconds: 900 },
		{ key: '1h',  label: '1h',  pageLimit: 48, seconds: 3600 }
	];

	type SubTab = 'connectivity' | 'products' | 'radar';
	const SUBTABS: { key: SubTab; label: string; stages: string[] }[] = [
		{ key: 'connectivity', label: 'Connectivity', stages: ['L0'] },
		{ key: 'products',     label: 'Products',     stages: ['L1'] },
		{ key: 'radar',        label: 'Radar',        stages: ['L2', 'L3', 'L4-T1T2'] }
	];

	const STATUS_BG: Record<string, string> = {
		pass:  'var(--color-ok)',
		warn:  'var(--color-warn)',
		fail:  'var(--color-fail)',
		error: 'var(--color-fail)',
		skip:  'var(--color-faint)'
	};

	// Cell geometry — sized so 14 columns + the time label fit in a
	// 390px viewport without horizontal scroll. Wider columns (Radar
	// tab, ~24 checks) overflow into the inner scroll container.
	const CELL = 20;          // px square
	const GAP  = 2;           // px between cells
	const TIME_W = 56;        // px sticky left time-label

	let bucket  = $state<Bucket>('5m');
	let subtab  = $state<SubTab>('radar');
	let columns = $state<CheckMeta[]>([]);
	let buckets = $state<TimelineBucket[]>([]);

	// Focus mode: when ?check_id=X is set in the URL, render only that
	// check + checks "directly relevant" to it (same target, direct deps,
	// direct reverse-deps). Coarser default grain so the longitudinal
	// pattern is visible. The user clears focus via the banner.
	const focusCheckId = $derived(page.url.searchParams.get('check_id') ?? '');

	// "Directly relevant" set for the focus check. Cap at 8 — investigation
	// rarely needs more, and beyond that the radar tab's horizontal scroll
	// stops being useful.
	function computeRelatedSet(allChecks: CheckMeta[], focusId: string): Set<string> {
		const out = new Set<string>();
		if (!focusId) return out;
		const byId = new Map(allChecks.map((c) => [c.id, c]));
		const focus = byId.get(focusId);
		if (!focus) return out;
		out.add(focus.id);

		// Same-target siblings: same target string across all stages. Captures
		// e.g. layer1.product.comp_ref + layer4.mosaic.comp_ref, or
		// layer2.radar.XSCW + layer4.xband.XSCW.
		if (focus.target) {
			for (const c of allChecks) {
				if (c.target === focus.target) out.add(c.id);
			}
		}

		// Direct deps (upstream — what this check relies on).
		for (const dep of focus.depends_on ?? []) out.add(dep);

		// Direct reverse-deps (downstream — what relies on this check).
		const reverseDeps: string[] = [];
		for (const c of allChecks) {
			if ((c.depends_on ?? []).includes(focus.id)) reverseDeps.push(c.id);
		}
		// Cap: keep focus + upstream first; drop reverse-deps if we're
		// already at the limit.
		const MAX = 8;
		for (const id of reverseDeps) {
			if (out.size >= MAX) break;
			out.add(id);
		}
		return out;
	}

	const relatedIds = $derived(computeRelatedSet(columns, focusCheckId));
	const focusCheck = $derived(columns.find((c) => c.id === focusCheckId) ?? null);
	let olderCursor = $state<string | null>(null);
	let loading = $state(true);
	let loadingMore = $state(false);
	let live = $state(true);
	let error = $state<string | null>(null);
	let liveTimer: ReturnType<typeof setInterval> | undefined;

	const cfg = $derived(BUCKETS.find((b) => b.key === bucket)!);
	const tabCfg = $derived(SUBTABS.find((t) => t.key === subtab)!);

	// Column subgroups per subtab. Products → category. Radar → stage.
	// Connectivity → single flat group. When focus mode is on (?check_id),
	// the subtab is ignored and we render the related-set grouped by stage —
	// the related set deliberately crosses tab boundaries (e.g. an L1
	// product check + its L0 origin dep) so cramming it into one subtab
	// would hide the dependency picture.
	type Subgroup = { label: string; cols: CheckMeta[] };
	const subgroups = $derived.by<Subgroup[]>(() => {
		if (focusCheckId && relatedIds.size) {
			const inFocus = columns.filter((c) => relatedIds.has(c.id));
			inFocus.sort((a, b) => a.stage.localeCompare(b.stage) || a.id.localeCompare(b.id));
			const byStage = new Map<string, CheckMeta[]>();
			for (const c of inFocus) {
				if (!byStage.has(c.stage)) byStage.set(c.stage, []);
				byStage.get(c.stage)!.push(c);
			}
			const out: Subgroup[] = [];
			for (const s of ['L0', 'L1', 'L2', 'L3', 'L4-T1T2']) {
				const cols = byStage.get(s);
				if (cols?.length) out.push({ label: stageLabel(s), cols });
			}
			return out;
		}

		const inTab = columns.filter((c) => tabCfg.stages.includes(c.stage));
		inTab.sort((a, b) => a.id.localeCompare(b.id));

		if (subtab === 'connectivity') {
			return inTab.length ? [{ label: 'Connectivity', cols: inTab }] : [];
		}
		if (subtab === 'products') {
			const byCat: Record<string, CheckMeta[]> = {};
			for (const cat of PRODUCT_CATEGORY_ORDER) byCat[cat] = [];
			for (const c of inTab) byCat[productCategory(c.target)].push(c);
			return PRODUCT_CATEGORY_ORDER
				.map((cat) => ({ label: PRODUCT_CATEGORY_LABEL[cat], cols: byCat[cat] }))
				.filter((sg) => sg.cols.length > 0);
		}
		// radar: group by stage
		const byStage = new Map<string, CheckMeta[]>();
		for (const c of inTab) {
			if (!byStage.has(c.stage)) byStage.set(c.stage, []);
			byStage.get(c.stage)!.push(c);
		}
		const out: Subgroup[] = [];
		for (const s of ['L2', 'L3', 'L4-T1T2']) {
			const cols = byStage.get(s);
			if (cols?.length) out.push({ label: stageLabel(s), cols });
		}
		return out;
	});

	const flatCols = $derived(subgroups.flatMap((g) => g.cols));
	const colCount = $derived(flatCols.length);

	// Grid total width: time column + cells + gaps + subgroup dividers.
	// 4px divider after each subgroup except the last.
	const gridW = $derived(
		TIME_W + GAP +
		flatCols.length * (CELL + GAP) +
		Math.max(0, subgroups.length - 1) * 6
	);

	const cellKey = (c: CheckMeta) => `${c.id}|${c.target}`;
	function shortLabel(c: CheckMeta): string {
		// Compact column header. Strip prefixes, drop underscores.
		const t = c.target || c.id.split('.').pop() || c.id;
		return t.replace(/_/g, ' ');
	}

	async function loadInitial() {
		loading = true;
		error = null;
		try {
			const [cks, page] = await Promise.all([
				api.checks(),
				api.timeline(bucket, cfg.pageLimit)
			]);
			columns = cks;
			buckets = page.buckets;
			olderCursor = page.older_cursor;
		} catch (e) {
			error = (e as Error).message;
		} finally {
			loading = false;
		}
	}

	async function loadMore() {
		if (loadingMore || !olderCursor) return;
		loadingMore = true;
		try {
			const page = await api.timeline(bucket, cfg.pageLimit, olderCursor);
			let merged = [...buckets, ...page.buckets];
			if (merged.length > 240) merged = merged.slice(0, 240);
			buckets = merged;
			olderCursor = page.older_cursor;
		} catch (e) {
			error = (e as Error).message;
		} finally {
			loadingMore = false;
		}
	}

	async function tickLive() {
		if (!live || loading) return;
		try {
			const page = await api.timeline(bucket, cfg.pageLimit);
			const newest = new Map<string, TimelineBucket>();
			for (const b of page.buckets) newest.set(b.ts, b);
			buckets = buckets.map((b) => newest.get(b.ts) ?? b);
			const have = new Set(buckets.map((b) => b.ts));
			const prepend = page.buckets.filter((b) => !have.has(b.ts));
			if (prepend.length) buckets = [...prepend, ...buckets];
		} catch {
			/* swallow */
		}
	}

	function setBucket(b: Bucket) {
		if (b === bucket) return;
		bucket = b;
		buckets = [];
		olderCursor = null;
		untrack(() => loadInitial());
	}

	// Persist subtab + grain + live across reloads, mobile-ergonomic.
	$effect(() => {
		try { localStorage.setItem('sentinel.muptime.tab',    subtab); } catch { /* */ }
	});
	$effect(() => {
		try { localStorage.setItem('sentinel.muptime.bucket', bucket); } catch { /* */ }
	});
	$effect(() => {
		try { localStorage.setItem('sentinel.muptime.live',   live ? '1' : '0'); } catch { /* */ }
	});

	// When focus mode toggles on, coarsen the grain so a week of context
	// fits in one screen of rows. When it toggles off, restore the prior
	// grain (we persist the user's chosen grain separately for this).
	let priorBucket: Bucket | null = null;
	$effect(() => {
		const fid = focusCheckId; // reactive read
		if (fid && bucket !== '1h') {
			priorBucket = bucket;
			bucket = '1h';
			buckets = [];
			olderCursor = null;
			untrack(() => loadInitial());
		} else if (!fid && priorBucket) {
			bucket = priorBucket;
			priorBucket = null;
			buckets = [];
			olderCursor = null;
			untrack(() => loadInitial());
		}
	});

	let visHandler: (() => void) | undefined;
	onMount(() => {
		try {
			const t = localStorage.getItem('sentinel.muptime.tab');
			if (t === 'connectivity' || t === 'products' || t === 'radar') subtab = t;
			const b = localStorage.getItem('sentinel.muptime.bucket');
			if (b === '1m' || b === '5m' || b === '15m' || b === '1h') bucket = b;
			const l = localStorage.getItem('sentinel.muptime.live');
			if (l === '0') live = false;
		} catch { /* */ }
		loadInitial();
		liveTimer = setInterval(tickLive, 30_000);
		visHandler = () => {
			if (document.hidden) {
				if (liveTimer) { clearInterval(liveTimer); liveTimer = undefined; }
			} else if (live && !liveTimer) {
				liveTimer = setInterval(tickLive, 30_000);
				tickLive();
			}
		};
		document.addEventListener('visibilitychange', visHandler);
	});
	onDestroy(() => {
		if (liveTimer) clearInterval(liveTimer);
		if (visHandler) document.removeEventListener('visibilitychange', visHandler);
	});

	// Buckets come newest-first from the API — render in that order so
	// the most recent activity sits at the top of the page.
	const rows = $derived(buckets);

	// Day-change separators between adjacent rows. We compare each row's
	// UTC date against the previous row's; the topmost row gets its date
	// label up front regardless.
	function dayLabel(iso: string): string {
		const d = new Date(iso);
		const today = new Date();
		const todayKey = today.toISOString().slice(0, 10);
		const yKey = new Date(Date.now() - 86400_000).toISOString().slice(0, 10);
		const k = iso.slice(0, 10);
		if (k === todayKey) return 'Today';
		if (k === yKey) return 'Yesterday';
		return d.toLocaleDateString(undefined, {
			weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC'
		});
	}
	function bucketTime(iso: string): string {
		return iso.slice(11, 16);
	}
	function sameDay(a: string, b: string): boolean {
		return a.slice(0, 10) === b.slice(0, 10);
	}

	// Drill-down state.
	let detailOpen   = $state(false);
	let detailCol    = $state<CheckMeta | null>(null);
	let detailTs     = $state<string>('');
	let detailRuns   = $state<CheckRun[]>([]);
	let detailLoading = $state(false);
	let detailError  = $state<string | null>(null);
	let detailToken  = 0;

	async function openDetail(b: TimelineBucket, col: CheckMeta) {
		const my = ++detailToken;
		detailCol = col;
		detailTs = b.ts;
		detailRuns = [];
		detailError = null;
		detailLoading = true;
		detailOpen = true;
		try {
			const start = new Date(b.ts).getTime();
			const end = start + cfg.seconds * 1000;
			const runs = await api.historyRuns(
				col.id, col.target,
				new Date(start).toISOString(),
				new Date(end).toISOString(),
				30
			);
			if (my !== detailToken) return;
			detailRuns = runs;
		} catch (e) {
			if (my !== detailToken) return;
			detailError = (e as Error).message;
		} finally {
			if (my === detailToken) detailLoading = false;
		}
	}

	// Drilldown nav: from an Uptime cell, the two "broader" views the user
	// might want are Timeline (when did alarms fire for this check?) and
	// History (raw runs ±2h around this bucket — wider than the bucket the
	// drilldown already lists).
	async function showInHistory() {
		if (!detailCol) return;
		const t = new Date(detailTs).getTime();
		const qs = new URLSearchParams({
			tab:      'checks',
			check_id: detailCol.id,
			target:   detailCol.target ?? '',
			since:    new Date(t - 2 * 3600_000).toISOString(),
			until:    new Date(t + 2 * 3600_000).toISOString()
		});
		detailOpen = false;
		await goto(`/m/history?${qs}`);
	}
	async function showInTimeline() {
		if (!detailCol) return;
		const qs = new URLSearchParams({
			check_id: detailCol.id,
			target:   detailCol.target ?? '',
			stage:    detailCol.stage ?? ''
		});
		detailOpen = false;
		await goto(`/m/timeline?${qs}`);
	}

	function detailCellStatus(): string {
		if (!detailCol) return 'skip';
		const b = buckets.find((b) => b.ts === detailTs);
		if (!b) return 'skip';
		return b.cells[cellKey(detailCol)]?.status ?? 'skip';
	}
</script>

<div class="flex h-full flex-col overflow-hidden">
	<!-- HEADER (fixed top, scrolls grid below) -->
	<header class="shrink-0 border-b border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2">
		<div class="flex items-center justify-between gap-2">
			<h1 class="text-[15px] font-semibold tracking-wide text-[var(--color-bright)]">Uptime</h1>
			<label class="flex items-center gap-1 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
				<input type="checkbox" bind:checked={live} class="accent-[var(--color-ok)] h-3.5 w-3.5" />
				<span class="text-[var(--color-default)]">Live</span>
			</label>
		</div>

		<!-- Focus banner replaces the subtab strip when a single check is in
		     focus — the related-set deliberately spans stages so subtab
		     filtering would just hide what the user is investigating. -->
		{#if focusCheckId}
			<div class="mt-2 flex items-center gap-2 rounded-md border border-[var(--color-info)]/40 bg-[var(--color-info)]/10 px-2.5 py-1.5 text-[11px]">
				<svg viewBox="0 0 24 24" class="h-3.5 w-3.5 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
					<circle cx="11" cy="11" r="7" />
					<line x1="20" y1="20" x2="16.65" y2="16.65" />
				</svg>
				<div class="min-w-0 flex-1 leading-tight">
					<div class="truncate text-[var(--color-bright)]">
						Focused on {focusCheck ? prettyCheckLabel(focusCheck.id, focusCheck.target) : focusCheckId}
					</div>
					<div class="text-[10px] num text-[var(--color-faint)]">
						{relatedIds.size} check{relatedIds.size === 1 ? '' : 's'} · 1h grain · related: same target + deps
					</div>
				</div>
				<a href="/m/uptime" class="shrink-0 rounded-md border border-[var(--color-info)]/60 px-2 py-1 text-[10px] uppercase tracking-wider text-[var(--color-info)] active:text-[var(--color-bright)]">× clear</a>
			</div>
		{:else}
			<!-- Subtab chip strip -->
			<div class="mt-2 flex gap-1">
				{#each SUBTABS as t}
					<button type="button" onclick={() => (subtab = t.key)}
						class="flex-1 rounded-md border px-2 py-1.5 text-[12px] uppercase tracking-wider {subtab === t.key
							? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
							: 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
						style="-webkit-tap-highlight-color: transparent; min-height: 36px;"
					>
						{t.label}
					</button>
				{/each}
			</div>
		{/if}

		<!-- Grain chip strip -->
		<div class="mt-2 flex items-center gap-1 text-[11px]">
			<span class="mr-1 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">grain</span>
			{#each BUCKETS as b}
				<button type="button" onclick={() => setBucket(b.key)}
					class="rounded-md border px-2.5 py-1 num {bucket === b.key
						? 'border-[var(--color-ok)] bg-[var(--color-ok)]/15 text-[var(--color-bright)]'
						: 'border-[var(--color-border-strong)] text-[var(--color-muted)] active:bg-[var(--color-elevated)]/60'}"
					style="-webkit-tap-highlight-color: transparent;"
				>
					{b.label}
				</button>
			{/each}
			<span class="ml-auto text-[10px] uppercase tracking-wider text-[var(--color-faint)] num">{buckets.length} rows · {colCount} checks · UTC</span>
		</div>
	</header>

	<!-- GRID -->
	{#if loading}
		<div class="flex-1 px-4 py-8 text-[12px] text-[var(--color-muted)]">loading…</div>
	{:else if error}
		<div class="m-3 rounded-sm border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-3 py-2 text-[12px] text-[var(--color-fail)]">{error}</div>
	{:else if colCount === 0}
		<div class="flex-1 px-4 py-8 text-center text-[12px] text-[var(--color-muted)]">No checks in this tab.</div>
	{:else}
		<div class="grid-wrap flex-1 overflow-auto">
			<div class="inline-block min-w-full" style="width: {gridW}px;">
				<!-- COLUMN HEADER (sticky top) -->
				<div class="sticky top-0 z-10 border-b border-[var(--color-border)] bg-[var(--color-surface)]">
					<!-- Subgroup labels row -->
					<div class="flex items-end" style="height: 18px;">
						<div style="width: {TIME_W + GAP}px;" class="shrink-0"></div>
						{#each subgroups as sg, gi}
							<div
								class="text-[9px] uppercase tracking-[0.14em] text-[var(--color-muted)] truncate px-1 num"
								style="width: {sg.cols.length * (CELL + GAP)}px; min-width: {sg.cols.length * (CELL + GAP)}px;"
							>{sg.label}</div>
							{#if gi < subgroups.length - 1}
								<div class="shrink-0" style="width: 6px;"></div>
							{/if}
						{/each}
					</div>
					<!-- Per-check column labels -->
					<div class="flex items-end pb-1" style="height: 56px;">
						<div style="width: {TIME_W + GAP}px;" class="shrink-0"></div>
						{#each subgroups as sg, gi}
							{#each sg.cols as col}
								<div
									class="num text-[9px] text-[var(--color-default)] leading-[1.05] shrink-0 origin-bottom-left -rotate-45 overflow-hidden whitespace-nowrap"
									style="width: {CELL + GAP}px; transform-origin: 0 100%;"
									title={prettyCheckLabel(col.id, col.target)}
								>
									<span class="inline-block max-w-[68px] truncate">{shortLabel(col)}</span>
								</div>
							{/each}
							{#if gi < subgroups.length - 1}
								<div class="shrink-0" style="width: 6px;"></div>
							{/if}
						{/each}
					</div>
				</div>

				<!-- BODY ROWS -->
				{#each rows as b, ri (b.ts)}
					{#if ri === 0 || !sameDay(b.ts, rows[ri - 1].ts)}
						<!-- Day separator. position: sticky; left: 0 pins it to
						     the viewport's left edge so the label stays visible
						     even when the user has scrolled the grid horizontally
						     (radar tab has more columns than fit on a phone). -->
						<div class="day-sep border-b border-[var(--color-border)]/50 bg-[var(--color-canvas)]/85 py-1">
							<span class="day-sep-text px-3 text-[9.5px] uppercase tracking-[0.16em] text-[var(--color-muted)]">
								{dayLabel(b.ts)}
							</span>
						</div>
					{/if}
					<div class="flex items-center" style="height: {CELL + GAP}px;">
						<div
							class="num text-[10px] text-[var(--color-faint)] tabular-nums shrink-0 text-right pr-1"
							style="width: {TIME_W}px;"
						>
							{bucketTime(b.ts)}
						</div>
						<div class="shrink-0" style="width: {GAP}px;"></div>
						{#each subgroups as sg, gi}
							{#each sg.cols as col}
								{@const cell = b.cells[cellKey(col)]}
								{@const isUpstream = cell?.reason === 'upstream_unhealthy'}
								<button
									type="button"
									onclick={() => openDetail(b, col)}
									aria-label="{prettyCheckLabel(col.id, col.target)} at {b.ts.slice(11,16)}Z: {cell?.status ?? 'no data'}{isUpstream ? ' (cascade from upstream)' : ''}"
									class="shrink-0 rounded-[3px] border border-[var(--color-border)]/30 active:opacity-70 relative"
									style="
										width: {CELL}px; height: {CELL}px;
										margin-right: {GAP}px;
										background: {cell ? STATUS_BG[cell.status] : 'transparent'};
										-webkit-tap-highlight-color: transparent;
									"
									title={cell ? `${prettyCheckLabel(col.id, col.target)} — ${cell.status}${isUpstream ? ' (cascade from upstream)' : ''} (×${cell.n})` : prettyCheckLabel(col.id, col.target)}
								>
									{#if isUpstream}
										<!-- Up-arrow badge marks cells whose runs were cascade-
										     demoted to skip because an upstream was unhealthy.
										     Lets operators distinguish "skipped because something
										     ELSE failed" from "intrinsically skipped" at a glance. -->
										<span
											class="pointer-events-none absolute inset-0 flex items-center justify-center text-[10px] font-bold leading-none text-[var(--color-canvas)]"
											aria-hidden="true"
										>↑</span>
									{/if}
								</button>
							{/each}
							{#if gi < subgroups.length - 1}
								<div class="shrink-0 self-stretch" style="width: 6px; background: linear-gradient(to right, transparent, var(--color-border) 50%, transparent);"></div>
							{/if}
						{/each}
					</div>
				{/each}

				<!-- LOAD OLDER -->
				{#if olderCursor}
					<div class="border-t border-[var(--color-border)] px-3 py-3">
						<button type="button" onclick={loadMore} disabled={loadingMore}
							class="w-full rounded-md border border-[var(--color-border-strong)] bg-[var(--color-elevated)]/30 px-3 py-2 text-[12px] uppercase tracking-wider text-[var(--color-default)] active:bg-[var(--color-elevated)]/60 disabled:opacity-50"
							style="-webkit-tap-highlight-color: transparent;"
						>
							{loadingMore ? 'loading…' : 'load older'}
						</button>
					</div>
				{:else}
					<div class="px-3 py-3 text-center text-[10.5px] text-[var(--color-faint)] italic">— end of history —</div>
				{/if}
			</div>
		</div>
	{/if}

	<!-- Bottom-nav clearance -->
	<div class="shrink-0" style="height: calc(72px + env(safe-area-inset-bottom, 0px));"></div>
</div>

<MobileDrillDown
	bind:open={detailOpen}
	title={detailCol ? prettyCheckLabel(detailCol.id, detailCol.target) : ''}
	subtitle={detailCol?.id ?? ''}
	stage={detailCol?.stage ?? ''}
	status={detailCellStatus()}
>
	{#if detailCol}
		<dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1.5 text-[12px] num">
			<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Bucket</dt>
			<dd class="text-[var(--color-default)]">{detailTs.slice(0,19).replace('T',' ')} UTC ({cfg.label})</dd>
			<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Target</dt>
			<dd class="text-[var(--color-default)]">{detailCol.target || '—'}</dd>
			<dt class="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Stage</dt>
			<dd class="text-[var(--color-default)]">{stageLabel(detailCol.stage)}</dd>
		</dl>

		<!-- Captured image (L4 image-quality checks only — most runs have no
		     payload.source and this section silently drops out). Show the
		     most recent run with an image; runs are pre-sorted newest-first
		     by the historyRuns endpoint. -->
		{#each (() => { const r = detailRuns.find(capturedImage); return r ? [r] : []; })() as imgRun}
			<section class="mt-4">
				<div class="mb-1 flex items-baseline justify-between text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
					<span>Captured image</span>
					<span class="num text-[var(--color-faint)] normal-case">{imgRun.finished_at.slice(11,19)}Z</span>
				</div>
				<div class="overflow-hidden rounded-sm border border-[var(--color-border)] bg-black">
					<LazyImage src={capturedImage(imgRun) ?? ''} alt="captured radar scan" minHeight={200} />
				</div>
			</section>
		{/each}

		<section class="mt-4">
			<div class="mb-1 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">Runs in this bucket</div>
			{#if detailLoading}
				<div class="text-[12px] text-[var(--color-muted)]">loading…</div>
			{:else if detailError}
				<div class="rounded-sm border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-3 py-2 text-[12px] text-[var(--color-fail)]">{detailError}</div>
			{:else if detailRuns.length === 0}
				<div class="text-[12px] text-[var(--color-faint)] italic">No runs recorded.</div>
			{:else}
				<ul class="divide-y divide-[var(--color-border)]/60 rounded-sm border border-[var(--color-border)]">
					{#each detailRuns as r}
						<li class="flex items-baseline gap-2 px-2 py-1.5">
							<span class="num shrink-0 text-[10.5px] text-[var(--color-faint)]">{r.finished_at.slice(11,19)}Z</span>
							<span class="num shrink-0 text-[10px] uppercase tracking-wider"
								style="color: {STATUS_BG[r.status] ?? 'var(--color-muted)'};">
								{r.status}
							</span>
							<span class="truncate text-[11.5px] num text-[var(--color-default)]" title={r.summary}>{r.summary}</span>
						</li>
					{/each}
				</ul>
			{/if}
		</section>

		<div class="mt-5 grid grid-cols-2 gap-2">
			<button type="button" onclick={showInTimeline}
				class="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-elevated)]/30 px-3 py-2.5 text-center text-[11.5px] uppercase tracking-wider text-[var(--color-bright)] active:bg-[var(--color-elevated)]/60"
				style="-webkit-tap-highlight-color: transparent;"
			>Show in Timeline</button>
			<button type="button" onclick={showInHistory}
				class="rounded-md border border-[var(--color-border-strong)] bg-[var(--color-elevated)]/30 px-3 py-2.5 text-center text-[11.5px] uppercase tracking-wider text-[var(--color-bright)] active:bg-[var(--color-elevated)]/60"
				style="-webkit-tap-highlight-color: transparent;"
			>Show in History</button>
		</div>
	{/if}
</MobileDrillDown>

<style>
	/* Hide scrollbars on the grid container — touch-driven scroll on mobile,
	   and the visible scrollbar is more visual noise than affordance on a
	   colored grid. */
	.grid-wrap {
		-webkit-overflow-scrolling: touch;
		scrollbar-width: thin;
	}
	.grid-wrap::-webkit-scrollbar {
		height: 4px; width: 4px;
	}
	/* Day separator: the inner grid container is wider than the viewport
	   (horizontal scroll), so a normal full-width div would render its left
	   padding inside the grid's x=0 — which on iOS Safari occasionally
	   clips the leading glyph due to letter-spacing kerning. Pin the inner
	   text span to the viewport's left edge via position: sticky so the
	   label tracks horizontal scroll instead of drifting off-screen. */
	.day-sep {
		position: relative;
		width: 100%;
	}
	.day-sep-text {
		position: sticky;
		left: 0;
		display: inline-block;
	}
</style>
