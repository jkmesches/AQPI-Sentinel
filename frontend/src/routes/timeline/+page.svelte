<script lang="ts">
	import { onMount, onDestroy, untrack } from 'svelte';
	import { api, type CheckMeta, type CheckRun, type TimelineBucket } from '$lib/api';
	import { prettyCheckLabel, stageLabel, stageColor, fmtAge, statusText } from '$lib/format';

	type Bucket = '1m' | '5m' | '15m' | '1h' | '6h' | '1d';
	const BUCKETS: { key: Bucket; label: string; pageLimit: number }[] = [
		{ key: '1m',  label: '1 min',   pageLimit: 60 },
		{ key: '5m',  label: '5 min',   pageLimit: 120 },
		{ key: '15m', label: '15 min',  pageLimit: 96 },
		{ key: '1h',  label: '1 hour',  pageLimit: 168 },
		{ key: '6h',  label: '6 hours', pageLimit: 120 },
		{ key: '1d',  label: '1 day',   pageLimit: 90 }
	];

	const STAGE_ORDER = ['L0', 'L1', 'L2', 'L3', 'L4-T1T2'];

	// Tabs slice the columns into digestible chunks. Each tab maps to the
	// stages that belong to it.
	type Tab = 'website' | 'products' | 'radars';
	const TABS: { key: Tab; label: string; stages: string[]; desc: string }[] = [
		{ key: 'website',  label: 'Website',  stages: ['L0'],
		  desc: 'Site liveness, TLS, public page.' },
		{ key: 'products', label: 'Products', stages: ['L1'],
		  desc: 'Per-product image freshness, streams, overlays.' },
		{ key: 'radars',   label: 'Radars',   stages: ['L2', 'L3', 'L4-T1T2'],
		  desc: 'Per-radar rollup, cross-checks, and image quality control.' }
	];
	const STATUS_BG: Record<string, string> = {
		pass:    'var(--color-ok)',
		warn:    'var(--color-warn)',
		fail:    'var(--color-fail)',
		error:   'var(--color-fail)',
		skip:    'var(--color-faint)',
		unknown: 'transparent'
	};
	const STATUS_WORD: Record<string, string> = {
		pass: 'PASS', warn: 'WARN', fail: 'FAIL', error: 'ERROR', skip: 'SKIP', unknown: '—'
	};

	// Static cell+row geometry — referenced both in CSS and in sticky offsets.
	const ROW_H        = 24;   // px, body cell height
	const STAGE_ROW_H  = 24;   // px, sticky stage band height
	const COL_W        = 32;   // px, body cell width
	const TIME_COL_W   = 132;  // px, sticky left column

	let tab           = $state<Tab>('radars');
	let bucket        = $state<Bucket>('5m');
	let columns       = $state<CheckMeta[]>([]);
	let buckets       = $state<TimelineBucket[]>([]);
	let olderCursor   = $state<string | null>(null);
	let loading       = $state(false);
	let loadingMore   = $state(false);
	let error         = $state<string | null>(null);
	let live          = $state(true);
	let liveTimer: ReturnType<typeof setInterval> | undefined;

	const cfg     = $derived(BUCKETS.find((b) => b.key === bucket)!);
	const tabCfg  = $derived(TABS.find((t) => t.key === tab)!);

	const groupedColumns = $derived.by(() => {
		const byStage = new Map<string, CheckMeta[]>();
		for (const c of columns) {
			if (!tabCfg.stages.includes(c.stage)) continue;
			if (!byStage.has(c.stage)) byStage.set(c.stage, []);
			byStage.get(c.stage)!.push(c);
		}
		const out: { stage: string; cols: CheckMeta[] }[] = [];
		for (const s of tabCfg.stages) {
			const cols = byStage.get(s);
			if (cols?.length) {
				cols.sort((a, b) => a.id.localeCompare(b.id));
				out.push({ stage: s, cols });
			}
		}
		return out;
	});
	const flatColumns = $derived(groupedColumns.flatMap((g) => g.cols));
	const totalCols   = $derived(flatColumns.length);

	// Use horizontal labels when there are few columns; vertical otherwise.
	const horizontalLabels = $derived(totalCols <= 5);
	const headerLabelH = $derived(horizontalLabels ? 28 : 88);  // px
	const headerColW   = $derived(horizontalLabels ? 96 : COL_W);
	const bodyColW     = $derived(horizontalLabels ? 96 : COL_W);

	const cellKey = (col: CheckMeta) => `${col.id}|${col.target}`;

	async function loadInitial() {
		loading = true;
		error = null;
		try {
			const [cks, page] = await Promise.all([api.checks(), api.timeline(bucket, cfg.pageLimit)]);
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
			buckets = [...buckets, ...page.buckets];
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

	let sentinelEl: HTMLDivElement | undefined = $state();
	let io: IntersectionObserver | undefined;
	$effect(() => {
		if (!sentinelEl) return;
		io?.disconnect();
		io = new IntersectionObserver(
			(es) => { if (es[0]?.isIntersecting) untrack(() => loadMore()); },
			{ rootMargin: '400px 0px' }
		);
		io.observe(sentinelEl);
		return () => io?.disconnect();
	});

	onMount(() => {
		loadInitial();
		liveTimer = setInterval(tickLive, 30_000);
	});
	onDestroy(() => {
		if (liveTimer) clearInterval(liveTimer);
		io?.disconnect();
	});

	let detail = $state<{
		ts: string;
		col: CheckMeta;
		cell: { status: string; n: number } | null;
	} | null>(null);
	let detailRuns    = $state<CheckRun[]>([]);
	let detailLoading = $state(false);
	let detailError   = $state<string | null>(null);
	let detailToken   = 0;
	const bucketSeconds = $derived(cfg ? { '1m':60,'5m':300,'15m':900,'1h':3600,'6h':21600,'1d':86400 }[bucket] : 300);

	async function openDetail(b: TimelineBucket, col: CheckMeta) {
		const myToken = ++detailToken;
		detail = { ts: b.ts, col, cell: b.cells[cellKey(col)] ?? null };
		detailRuns = [];
		detailError = null;
		detailLoading = true;
		try {
			const start = new Date(b.ts).getTime();
			const end   = start + bucketSeconds * 1000;
			const runs = await api.historyRuns(
				col.id, col.target,
				new Date(start).toISOString(),
				new Date(end).toISOString(),
				50
			);
			if (myToken !== detailToken) return; // a newer click superseded us
			detailRuns = runs;
		} catch (e) {
			if (myToken !== detailToken) return;
			detailError = (e as Error).message;
		} finally {
			if (myToken === detailToken) detailLoading = false;
		}
	}
	function closeDetail() { detail = null; detailRuns = []; detailError = null; }

	function isImageQc(checkId: string): boolean {
		return checkId.startsWith('layer4.xband.') || checkId.startsWith('layer4.mosaic.');
	}

	// Escape HTML, then turn **text** into <strong>text</strong>. Used so the
	// explain bullets can highlight the part that actually tripped a verdict.
	function escapeAndBold(s: string): string {
		const esc = s
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;');
		return esc.replace(/\*\*([^*]+)\*\*/g, '<strong class="text-[var(--color-bright)]">$1</strong>');
	}

	// Plain-English bullet list explaining why a run got its status. Payload
	// shape varies by check family; everything here is best-effort and falls
	// back to the run's own summary line.
	function explainRun(run: CheckRun): string[] {
		const out: string[] = [];
		const p = (run.payload ?? {}) as Record<string, any>;

		// Layer 0 — site / TLS / origin
		if (run.check_id.startsWith('layer0.')) {
			if (typeof p.http === 'number') out.push(`HTTP ${p.http} from upstream.`);
			if (p.reason) out.push(`Reason: ${p.reason}`);
			if (p.expires_in_days != null) out.push(`TLS cert expires in ${p.expires_in_days} days.`);
			if (p.elapsed_ms != null) out.push(`Latency: ${p.elapsed_ms} ms.`);
		}

		// Layer 1 — product freshness
		if (run.check_id.startsWith('layer1.product.')) {
			if (p.product_label) out.push(`Product: ${p.product_label}.`);
			if (p.last_ts) {
				const ageS = (Date.now() - new Date(p.last_ts).getTime()) / 1000;
				out.push(`Latest scan timestamp: ${p.last_ts} (${fmtAge(ageS)} ago).`);
			}
			if (typeof p.n_steps === 'number') out.push(`${p.n_steps} time steps available.`);
			if (typeof p.image_http === 'number') out.push(`Image fetch HTTP ${p.image_http}.`);
			if (typeof p.image_bytes === 'number') out.push(`Image ${p.image_bytes.toLocaleString()} bytes.`);
			if (p.parity && p.parity.verdict !== 'pass') {
				out.push(`Stream parity ${p.parity.verdict}: ${p.parity.mismatches} mismatch(es), ${p.parity.unparseable} unparseable.`);
				if (p.parity.first_mismatch) out.push(`First mismatch: ${JSON.stringify(p.parity.first_mismatch)}.`);
			}
			if (p.sub_status) {
				const failing = Object.entries(p.sub_status).filter(
					([, v]) => v !== 'pass'
				);
				if (failing.length) {
					out.push(`Failing sub-checks: ${failing.map(([k, v]) => `${k}=${v}`).join(', ')}.`);
				}
			}
		}

		// Layer 1 stream / vector
		if (run.check_id.startsWith('layer1.stream.') || run.check_id.startsWith('layer1.vector.')) {
			if (typeof p.http === 'number')         out.push(`HTTP ${p.http}.`);
			if (typeof p.rows === 'number')         out.push(`${p.rows} rows returned.`);
			if (typeof p.elapsed_ms === 'number')   out.push(`Took ${p.elapsed_ms} ms.`);
			if (p.reason)                            out.push(`Reason: ${p.reason}.`);
		}

		// Layer 2 — per-radar rollup
		if (run.check_id.startsWith('layer2.radar.')) {
			if (p.declared && p.verdict) {
				out.push(`Site declares this radar **${p.declared}**; we observe **${p.verdict.replace('_', ' ').toLowerCase()}**.`);
			}
			if (p.observed?.primary_moment != null) {
				out.push(`Primary moment "${p.observed.primary_moment}" returned ${p.observed.primary} image(s).`);
			}
			if (p.moments) {
				const parts = Object.entries(p.moments).map(([m, n]) => `${m}=${n}`);
				out.push(`Image counts by moment: ${parts.join(', ')}.`);
			}
			if (p.dead_moments?.length) {
				out.push(`Empty moments: ${p.dead_moments.join(', ')}.`);
			}
		}

		// Layer 3 — cross-source reconciliation
		if (run.check_id.startsWith('layer3.')) {
			if (p.matched != null && p.expected != null) {
				out.push(`Matched ${p.matched} of ${p.expected} expected overlays.`);
			}
			if (p.reason) out.push(`Reason: ${p.reason}.`);
		}

		// Layer 4 — image QC
		if (run.check_id.startsWith('layer4.')) {
			const t1 = p.tier1 ?? {};
			const t2 = p.tier2 ?? {};
			if (p.reason) out.push(`Image not available — ${p.reason}.`);
			if (t1.coverage_pct != null) {
				out.push(`Pixel coverage: ${t1.coverage_pct}% (${t1.n_active_px?.toLocaleString?.() ?? t1.n_active_px} active pixels).`);
			}
			if (t1.mean_value != null) {
				out.push(`Mean intensity ${t1.mean_value} · std ${t1.std_value} · autocorrelation ${t1.autocorr}.`);
			}
			if (t2.extreme && t2.extreme.verdict !== 'OK') {
				out.push(`Extreme-value check: **${t2.extreme.verdict}** (fraction ${t2.extreme.fraction}).`);
			}
			if (t2.speckle && t2.speckle.verdict !== 'OK') {
				out.push(`Speckle check: **${t2.speckle.verdict}** (ratio ${t2.speckle.ratio}).`);
			}
			if (t2.range_ring && t2.range_ring.verdict !== 'OK' && t2.range_ring.verdict !== 'N/A') {
				out.push(`Range-ring artifact: **${t2.range_ring.verdict}**.`);
			}
			if (t2.frozen && t2.frozen.verdict === 'FROZEN') {
				out.push(`**Frame identical** to the previous run (frozen-frame detection tripped).`);
			}
		}

		if (!out.length && run.summary) out.push(run.summary);
		return out;
	}

	function fmtRowTs(iso: string): { primary: string; secondary: string } {
		const d = new Date(iso);
		const hh = d.getUTCHours().toString().padStart(2, '0');
		const mm = d.getUTCMinutes().toString().padStart(2, '0');
		const mo = (d.getUTCMonth() + 1).toString().padStart(2, '0');
		const da = d.getUTCDate().toString().padStart(2, '0');
		if (bucket === '1d') return { primary: `${mo}/${da}`, secondary: `${d.getUTCFullYear()}` };
		return { primary: `${hh}:${mm}`, secondary: `${mo}/${da}` };
	}
	function relativeAge(iso: string): string {
		const s = (Date.now() - new Date(iso).getTime()) / 1000;
		return s < 0 ? 'in future' : `${fmtAge(s)} ago`;
	}
	function checkBlurb(c: CheckMeta): string {
		if (c.id.startsWith('layer0.tls.'))         return 'Verifies the public TLS certificate is valid and not near expiry.';
		if (c.id.startsWith('layer0.origin.'))      return 'Probes the origin server directly (bypassing the edge cache) to confirm it is alive.';
		if (c.id.startsWith('layer0.website.public')) return 'Fetches the main public dashboard page and checks the response.';
		if (c.id.startsWith('layer0.website.root')) return 'Confirms the bare-root URL returns the expected 404 — a regression here means routing is broken.';
		if (c.id.startsWith('layer1.product.'))     return 'Tracks freshness of this product image — flags when the latest scan is older than expected.';
		if (c.id.startsWith('layer1.stream.'))      return 'Calls the streaming API used by the live dashboard and verifies it returns sane data.';
		if (c.id.startsWith('layer1.vector.'))      return 'Fetches a vector overlay (watersheds, flow lines, station markers).';
		if (c.id.startsWith('layer2.radar.'))       return `Aggregates all per-product checks for radar ${c.target} into a single "is this radar healthy" verdict.`;
		if (c.id.startsWith('layer3.'))             return 'Cross-radar / cross-source reconciliation — catches when one feed disagrees with the rest.';
		if (c.id.startsWith('layer4.xband.'))       return 'Image quality control: looks at the actual pixels (coverage, noise, frozen-frame, range-ring artifacts) for this radar.';
		if (c.id.startsWith('layer4.'))             return 'Layer 4 image-based quality check.';
		return 'No description available.';
	}
</script>

<div class="flex h-full flex-col">
	<!-- TABS -->
	<nav class="flex items-end gap-1 border-b border-[var(--color-border)] px-4 pt-2 text-[12px]">
		{#each TABS as t}
			<button
				class="px-3 py-1.5 uppercase tracking-wider {tab === t.key
					? 'text-[var(--color-bright)] border-b-2 border-[var(--color-bright)] -mb-px'
					: 'text-[var(--color-muted)] hover:text-[var(--color-default)] border-b-2 border-transparent -mb-px'}"
				onclick={() => (tab = t.key)}
			>
				{t.label}
			</button>
		{/each}
		<span class="ml-3 mb-1.5 text-[10.5px] text-[var(--color-faint)] truncate">
			{tabCfg.desc}
		</span>
	</nav>

	<header class="flex flex-wrap items-center gap-4 border-b border-[var(--color-border)] px-4 py-2 text-[11px]">
		<div class="flex items-baseline gap-2">
			<span class="label tracking-[0.18em] text-[var(--color-bright)]">{tabCfg.label.toUpperCase()} · STATE OVER TIME</span>
			<span class="text-[var(--color-muted)]">·</span>
			<span class="num text-[var(--color-muted)]">{buckets.length} rows · {totalCols} checks</span>
		</div>

		<div class="flex items-center gap-1">
			<span class="text-[var(--color-muted)] uppercase tracking-wider">grain</span>
			<div class="flex border border-[var(--color-border-strong)]">
				{#each BUCKETS as b}
					<button
						class="px-2 py-0.5 text-[11px] {bucket === b.key
							? 'bg-[var(--color-elevated)] text-[var(--color-bright)]'
							: 'text-[var(--color-muted)] hover:text-[var(--color-default)]'}"
						onclick={() => setBucket(b.key)}
					>
						{b.label}
					</button>
				{/each}
			</div>
		</div>

		<label class="flex items-center gap-1 text-[var(--color-muted)] uppercase tracking-wider">
			<input type="checkbox" bind:checked={live} class="accent-[var(--color-ok)]" />
			<span>live (30s)</span>
		</label>

		<div class="flex items-center gap-2 ml-auto">
			{#each ['pass', 'warn', 'fail', 'skip', 'unknown'] as s}
				<span class="flex items-center gap-1 text-[10.5px] text-[var(--color-muted)] uppercase tracking-wider">
					<span class="inline-block h-2.5 w-2.5" style="background:{STATUS_BG[s]}"></span>
					{s === 'unknown' ? 'no data' : s}
				</span>
			{/each}
		</div>
	</header>

	{#if error}
		<div class="px-4 py-2 text-[12px] text-[var(--color-fail)] border-b border-[var(--color-border)]">{error}</div>
	{/if}

	<div class="flex-1 overflow-auto bg-[var(--color-canvas)] relative">
		{#if loading}
			<div class="px-4 py-8 text-[12px] text-[var(--color-muted)]">loading timeline…</div>
		{:else if !buckets.length}
			<div class="px-4 py-8 text-[12px] text-[var(--color-muted)]">no data in this window.</div>
		{:else}
			<table class="border-separate timeline-grid" style="border-spacing:0;">
				<thead>
					<tr>
						<th
							class="sticky left-0 z-30 bg-[var(--color-canvas)] border-b border-r border-[var(--color-border)] px-3 text-left text-[10px] uppercase tracking-[0.18em] text-[var(--color-muted)]"
							style="top:0; height:{STAGE_ROW_H}px; min-width:{TIME_COL_W}px; width:{TIME_COL_W}px;"
						>
							time (utc)
						</th>
						{#each groupedColumns as g}
							<th
								class="sticky z-20 bg-[var(--color-canvas)] border-b border-l border-[var(--color-border)] px-2 text-center text-[10px] uppercase tracking-[0.18em] {stageColor(g.stage)}"
								style="top:0; height:{STAGE_ROW_H}px;"
								colspan={g.cols.length}
							>
								{stageLabel(g.stage)} <span class="text-[var(--color-faint)] num">({g.cols.length})</span>
							</th>
						{/each}
					</tr>
					<tr>
						<th
							class="sticky left-0 z-30 bg-[var(--color-canvas)] border-b border-r border-[var(--color-border)] px-3 text-left text-[10px] text-[var(--color-faint)]"
							style="top:{STAGE_ROW_H}px; height:{headerLabelH}px;"
						>
							<span class="num">{cfg.label} buckets</span>
						</th>
						{#each flatColumns as col}
							<th
								class="sticky z-20 bg-[var(--color-canvas)] border-b border-[var(--color-border)] p-0 text-[11px] text-[var(--color-default)] num"
								style="top:{STAGE_ROW_H}px; height:{headerLabelH}px; min-width:{headerColW}px; max-width:{headerColW}px; width:{headerColW}px;"
								title="{col.id} · {col.target} · every {col.cadence_s}s"
							>
								{#if horizontalLabels}
									<div class="h-full w-full flex items-center justify-center px-1">
										<span class="truncate">{prettyCheckLabel(col.id, col.target)}</span>
									</div>
								{:else}
									<div
										class="h-full w-full flex items-end justify-center pb-1"
										style="writing-mode:vertical-rl; transform:rotate(180deg); white-space:nowrap; line-height:1;"
									>
										<span class="truncate" style="max-height:5.2rem;">
											{prettyCheckLabel(col.id, col.target)}
										</span>
									</div>
								{/if}
							</th>
						{/each}
					</tr>
				</thead>
				<tbody>
					{#each buckets as b}
						{@const ts = fmtRowTs(b.ts)}
						{@const hourBoundary = new Date(b.ts).getUTCMinutes() === 0}
						<tr class="hover:bg-[var(--color-elevated)]/30">
							<td
								class="sticky left-0 z-10 bg-[var(--color-canvas)] border-r border-[var(--color-border)] px-3 text-[11px] num {hourBoundary
									? 'text-[var(--color-bright)] border-t border-t-[var(--color-border)]'
									: 'text-[var(--color-default)]'}"
								style="height:{ROW_H}px;"
								title={`${b.ts} · ${relativeAge(b.ts)}`}
							>
								<div class="flex items-baseline gap-2 leading-none">
									<span>{ts.primary}</span>
									<span class="text-[9.5px] text-[var(--color-faint)]">{ts.secondary}</span>
								</div>
							</td>
							{#each flatColumns as col}
								{@const cell = b.cells[cellKey(col)]}
								{@const st = cell?.status ?? 'unknown'}
								<td
									class="p-0 text-center align-middle cursor-pointer"
									style="min-width:{bodyColW}px; max-width:{bodyColW}px; width:{bodyColW}px; height:{ROW_H}px;"
									onclick={() => openDetail(b, col)}
									title={cell
										? `${prettyCheckLabel(col.id, col.target)} · ${STATUS_WORD[st]} · ${cell.n} run${cell.n === 1 ? '' : 's'} · ${ts.primary} UTC`
										: `${prettyCheckLabel(col.id, col.target)} · no data · ${ts.primary} UTC`}
								>
									<span
										class="block mx-auto"
										style="width:{bodyColW - 6}px; height:{ROW_H - 6}px; background:{STATUS_BG[st]}; border:1px solid var(--color-border); border-radius:2px;"
									></span>
								</td>
							{/each}
						</tr>
					{/each}
				</tbody>
			</table>

			<div
				bind:this={sentinelEl}
				class="px-4 py-3 text-center text-[11px] text-[var(--color-muted)]"
			>
				{#if loadingMore}
					loading older…
				{:else if olderCursor}
					· scroll for older ·
				{:else}
					end of data
				{/if}
			</div>
		{/if}
	</div>

	{#if detail}
		<button
			class="fixed inset-0 z-40 bg-black/40"
			aria-label="close"
			onclick={closeDetail}
		></button>
		<div
			class="fixed right-4 top-20 bottom-4 z-50 w-[34rem] max-w-[95vw] overflow-auto border border-[var(--color-border-strong)] bg-[var(--color-surface)] p-4 text-[12px] shadow-xl"
		>
			<!-- HEADER -->
			<div class="flex items-start justify-between gap-3">
				<div class="min-w-0">
					<div class="label text-[var(--color-faint)]">
						{stageLabel(detail.col.stage)} · {detail.col.stage}
					</div>
					<div class="text-[14px] text-[var(--color-bright)] num truncate">
						{prettyCheckLabel(detail.col.id, detail.col.target)}
					</div>
					<div class="text-[10.5px] text-[var(--color-muted)] num truncate">{detail.col.id}</div>
				</div>
				<button
					class="text-[var(--color-muted)] hover:text-[var(--color-bright)] text-lg leading-none"
					onclick={closeDetail}
					aria-label="close"
				>×</button>
			</div>

			<!-- BUCKET META -->
			<div class="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[11px]">
				<span class="text-[var(--color-muted)] uppercase tracking-wider">bucket</span>
				<span class="num">
					{detail.ts.slice(0, 19)}Z
					<span class="text-[var(--color-faint)]"> · {cfg.label} window</span>
				</span>
				<span class="text-[var(--color-muted)] uppercase tracking-wider">target</span>
				<span class="num">{detail.col.target}</span>
				<span class="text-[var(--color-muted)] uppercase tracking-wider">cadence</span>
				<span class="num">every {detail.col.cadence_s}s</span>
				<span class="text-[var(--color-muted)] uppercase tracking-wider">summary</span>
				{#if detail.cell}
					<span class="num">
						<span
							class="inline-block h-2.5 w-2.5 align-middle mr-1"
							style="background:{STATUS_BG[detail.cell.status]}"
						></span>
						worst = {STATUS_WORD[detail.cell.status]} · {detail.cell.n} run{detail.cell.n === 1 ? '' : 's'}
					</span>
				{:else}
					<span class="num text-[var(--color-faint)]">no data</span>
				{/if}
			</div>

			<div class="mt-4 text-[10.5px] text-[var(--color-muted)] uppercase tracking-wider">about this check</div>
			<p class="mt-1 text-[12px] text-[var(--color-default)] leading-relaxed">
				{checkBlurb(detail.col)}
			</p>

			<!-- RUNS -->
			<div class="mt-4 mb-1 flex items-center justify-between text-[10.5px] text-[var(--color-muted)] uppercase tracking-wider">
				<span>runs in this bucket</span>
				{#if detailLoading}<span>loading…</span>{/if}
			</div>

			{#if detailError}
				<div class="border border-[var(--color-fail)]/40 bg-[var(--color-fail)]/10 px-2 py-1 text-[11px] text-[var(--color-fail)]">{detailError}</div>
			{:else if !detailLoading && !detailRuns.length}
				<div class="text-[11px] text-[var(--color-faint)] italic">No runs landed in this bucket.</div>
			{/if}

			{#each detailRuns as run (run.id)}
				{@const dur = (new Date(run.finished_at).getTime() - new Date(run.started_at).getTime())}
				{@const explain = explainRun(run)}
				<div class="mt-2 border border-[var(--color-border)] bg-[var(--color-canvas)] p-3">
					<!-- Run header line -->
					<div class="flex items-center gap-2 text-[11px]">
						<span
							class="inline-block h-2.5 w-2.5"
							style="background:{STATUS_BG[run.status] ?? 'var(--color-faint)'}"
						></span>
						<span class="uppercase tracking-wider {statusText(run.status)}">{run.status}</span>
						<span class="num text-[var(--color-bright)]">#{run.id}</span>
						<span class="num text-[var(--color-muted)]">{run.finished_at.slice(11, 19)}Z</span>
						<span class="num text-[var(--color-faint)] ml-auto">{dur} ms</span>
					</div>

					<!-- Plain-English bullets -->
					<ul class="mt-2 space-y-0.5 text-[12px] text-[var(--color-default)] leading-snug">
						{#each explain as line}
							<li class="flex gap-1.5">
								<span class="text-[var(--color-faint)]">·</span>
								<span>{@html escapeAndBold(line)}</span>
							</li>
						{/each}
					</ul>

					<!-- Image (L4 image QC only) -->
					{#if isImageQc(run.check_id) && (run.payload as any)?.source}
						<div class="mt-3">
							<div class="text-[10px] text-[var(--color-muted)] uppercase tracking-wider mb-1">
								captured image
							</div>
							<a
								href={`/api/upstream/image_by_source.png?source=${encodeURIComponent((run.payload as any).source)}`}
								target="_blank"
								rel="noreferrer"
								class="block border border-[var(--color-border)] bg-black"
							>
								<img
									src={`/api/upstream/image_by_source.png?source=${encodeURIComponent((run.payload as any).source)}`}
									alt="captured scan"
									class="block w-full h-auto"
									loading="lazy"
								/>
							</a>
							<div class="mt-1 text-[10px] text-[var(--color-faint)] num truncate" title={(run.payload as any).source}>
								source: {(run.payload as any).source}
							</div>
						</div>
					{/if}

					<!-- Raw summary (already shown above, but useful in raw form) -->
					{#if run.summary}
						<details class="mt-2 text-[11px] text-[var(--color-muted)]">
							<summary class="cursor-pointer text-[var(--color-faint)] uppercase tracking-wider text-[10px]">raw summary</summary>
							<pre class="mt-1 whitespace-pre-wrap text-[11px] num text-[var(--color-muted)]">{run.summary}</pre>
						</details>
					{/if}

					<!-- Full payload (collapsed) -->
					{#if run.payload}
						<details class="mt-1 text-[11px]">
							<summary class="cursor-pointer text-[var(--color-faint)] uppercase tracking-wider text-[10px]">full payload</summary>
							<pre class="mt-1 max-h-72 overflow-auto whitespace-pre-wrap text-[10.5px] num text-[var(--color-muted)]">{JSON.stringify(run.payload, null, 2)}</pre>
						</details>
					{/if}
				</div>
			{/each}

			<div class="mt-4 flex gap-2">
				<a
					class="border border-[var(--color-border-strong)] px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-elevated)]"
					href={`/history?stage=${encodeURIComponent(detail.col.stage)}&target=${encodeURIComponent(detail.col.target)}`}
				>
					open in history
				</a>
			</div>
		</div>
	{/if}
</div>

<style>
	.timeline-grid {
		font-variant-numeric: tabular-nums;
	}
</style>
