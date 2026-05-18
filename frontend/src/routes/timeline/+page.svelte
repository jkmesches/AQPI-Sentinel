<script lang="ts">
	import { onMount, onDestroy, untrack } from 'svelte';
	import { api, type CheckMeta, type CheckRun, type TimelineBucket } from '$lib/api';
	import {
		prettyCheckLabel, stageLabel, stageColor, fmtAge, statusText,
		productCategory, PRODUCT_CATEGORY_ORDER, PRODUCT_CATEGORY_LABEL
	} from '$lib/format';
	import LazyImage from '$lib/components/LazyImage.svelte';
	import PieStatus from '$lib/components/PieStatus.svelte';
	import ReportExportModal from '$lib/components/ReportExportModal.svelte';
	import { sentinel } from '$lib/stores/state.svelte';
	import { url as apiUrl } from '$lib/origin';

	type Bucket = '1m' | '5m' | '15m' | '1h' | '6h' | '1d';
	const BUCKETS: { key: Bucket; label: string; pageLimit: number }[] = [
		{ key: '1m',  label: '1 min',   pageLimit: 30 },
		{ key: '5m',  label: '5 min',   pageLimit: 40 },
		{ key: '15m', label: '15 min',  pageLimit: 40 },
		{ key: '1h',  label: '1 hour',  pageLimit: 48 },
		{ key: '6h',  label: '6 hours', pageLimit: 40 },
		{ key: '1d',  label: '1 day',   pageLimit: 30 }
	];
	// Hard cap on total accumulated rows. Beyond this we evict the oldest
	// so the table doesn't grow unbounded as the user scrolls — a 4-figure
	// row count combined with ~15 cells/row tips browsers into freeze.
	const MAX_BUCKETS = 300;

	// Seconds per bucket — used to size the time window when deeplinking from
	// a clicked cell to the History page. Mirror of the backend table in
	// backend/api/routes/history.py:_BUCKETS_S.
	const BUCKET_SECONDS: Record<string, number> = {
		'1m': 60, '5m': 300, '15m': 900, '1h': 3600, '6h': 21600, '1d': 86400
	};

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

	// Static cell+row geometry. Rows = checks (horizontal labels on the left,
	// no rotation), columns = time buckets oldest→newest.
	const ROW_H         = 22;   // px, per-check row height
	const TIME_COL_W    = 12;   // px, default per-bucket column width
	const LABEL_COL_W   = 220;  // px, sticky left label column
	const TIME_HDR_H    = 32;   // px, sticky top time-header height
	const STAGE_ROW_H   = 22;   // px, height of a "[Stage]" separator row

	let tab           = $state<Tab>('radars');
	let bucket        = $state<Bucket>('5m');
	let columns       = $state<CheckMeta[]>([]);
	let buckets       = $state<TimelineBucket[]>([]);
	let olderCursor   = $state<string | null>(null);
	let loading       = $state(false);
	let loadingMore   = $state(false);
	let error         = $state<string | null>(null);
	// live = true by default; persisted per-browser so a user's explicit
	// opt-out survives reloads.
	let live          = $state(true);
	let liveTimer: ReturnType<typeof setInterval> | undefined;

	// asc = oldest on the left, newest on the right (the original layout).
	// desc = newest on the left, oldest on the right. Persisted per browser.
	// The API returns buckets newest-first, so asc applies a reverse before
	// rendering; desc passes them through unchanged. See `orderedBuckets`.
	type SortOrder = 'asc' | 'desc';
	let sortOrder = $state<SortOrder>('asc');

	const cfg     = $derived(BUCKETS.find((b) => b.key === bucket)!);
	const tabCfg  = $derived(TABS.find((t) => t.key === tab)!);

	// Per-tab status proportions for the pie icons on the tab buttons.
	// Computed from sentinel.rollup so the donut reflects current state,
	// not whatever the historical timeline view is rendering.
	type StatusCounts = { pass: number; warn: number; fail: number; error: number; skip: number };
	function emptyCounts(): StatusCounts {
		return { pass: 0, warn: 0, fail: 0, error: 0, skip: 0 };
	}
	// --- Export Report modal state ---
	let exportOpen = $state(false);

	// Defaults pulled from the on-screen state when the modal opens. `buckets`
	// is loaded oldest-last-most-recent so we read both ends for the time
	// window. datetime-local wants "YYYY-MM-DDTHH:MM" with no seconds/zone.
	function isoToLocal(iso: string): string {
		return iso.slice(0, 16);
	}
	const exportDefaults = $derived.by(() => {
		const visibleTargets = flatColumns.map((c) => c.target).filter((t) => !!t);
		const tsList = buckets.map((b) => b.ts).sort();
		const earliest = tsList[0] ?? new Date(Date.now() - 3_600_000).toISOString();
		const latest   = tsList[tsList.length - 1] ?? new Date().toISOString();
		return {
			since: isoToLocal(earliest),
			until: isoToLocal(latest),
			bucket: bucket as string,
			stages: [...tabCfg.stages],
			targets: [...new Set(visibleTargets)]
		};
	});
	const exportTargetOptions = $derived.by(() => {
		const seen = new Set<string>();
		const out: { value: string; label: string; hint?: string }[] = [];
		for (const [stageId, list] of Object.entries(sentinel.rollup?.stages ?? {})) {
			for (const r of list as any[]) {
				if (!r.target || seen.has(r.target)) continue;
				seen.add(r.target);
				out.push({ value: r.target, label: r.target, hint: stageLabel(stageId) });
			}
		}
		out.sort((a, b) => a.label.localeCompare(b.label));
		return out;
	});

	const tabCounts = $derived.by<Record<string, StatusCounts>>(() => {
		const out: Record<string, StatusCounts> = {};
		for (const t of TABS) {
			const c = emptyCounts();
			for (const s of t.stages) {
				for (const r of sentinel.rollup?.stages?.[s] ?? []) {
					const st = (r.status ?? 'skip') as keyof StatusCounts;
					if (st in c) c[st] += 1;
					else c.skip += 1;
				}
			}
			out[t.key] = c;
		}
		return out;
	});

	// `subgroups` is populated for L1 only (Products tab) so the grid
	// renders a category sub-header before each cohort — same four
	// categories the home page uses (Radar Data / Atmospheric Forecast /
	// CoSMoS / NWM / Other). For other stages we keep a single flat list.
	const groupedColumns = $derived.by(() => {
		const byStage = new Map<string, CheckMeta[]>();
		for (const c of columns) {
			if (!tabCfg.stages.includes(c.stage)) continue;
			if (!byStage.has(c.stage)) byStage.set(c.stage, []);
			byStage.get(c.stage)!.push(c);
		}
		const out: { stage: string; cols: CheckMeta[]; subgroups?: { label: string; cols: CheckMeta[] }[] }[] = [];
		for (const s of tabCfg.stages) {
			const cols = byStage.get(s);
			if (!cols?.length) continue;
			cols.sort((a, b) => a.id.localeCompare(b.id));
			if (s === 'L1') {
				const byCat: Record<string, CheckMeta[]> = {};
				for (const cat of PRODUCT_CATEGORY_ORDER) byCat[cat] = [];
				for (const c of cols) byCat[productCategory(c.target)].push(c);
				const subgroups = PRODUCT_CATEGORY_ORDER
					.map((cat) => ({ label: PRODUCT_CATEGORY_LABEL[cat], cols: byCat[cat] }))
					.filter((sg) => sg.cols.length > 0);
				out.push({ stage: s, cols, subgroups });
			} else {
				out.push({ stage: s, cols });
			}
		}
		return out;
	});
	const flatColumns = $derived(groupedColumns.flatMap((g) => g.cols));
	const totalCols   = $derived(flatColumns.length);

	// Per-bucket column width — wider when fewer buckets so the row doesn't
	// feel sparse; narrower when many to keep the whole window on screen.
	const bodyColW = $derived.by(() => {
		const n = buckets.length;
		if (n <= 12) return 28;
		if (n <= 40) return 16;
		if (n <= 80) return 12;
		return 10;
	});

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
			let merged = [...buckets, ...page.buckets];
			if (merged.length > MAX_BUCKETS) merged = merged.slice(0, MAX_BUCKETS);
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

	// Manual pagination only — auto-loading on scroll let users accumulate
	// thousands of cells and lock up the tab. They can now click "Load older"
	// to grow the table on demand.

	const orderedBuckets = $derived(
		sortOrder === 'asc' ? [...buckets].reverse() : [...buckets]
	);

	$effect(() => {
		try { localStorage.setItem('sentinel.timeline.sort', sortOrder); } catch { /* */ }
	});
	$effect(() => {
		try { localStorage.setItem('sentinel.timeline.live', live ? '1' : '0'); } catch { /* */ }
	});

	let visHandler: (() => void) | undefined;
	onMount(() => {
		try {
			const s = localStorage.getItem('sentinel.timeline.sort');
			if (s === 'asc' || s === 'desc') sortOrder = s;
			const l = localStorage.getItem('sentinel.timeline.live');
			if (l === '0') live = false;
			else if (l === '1') live = true;
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
			if (t2.frozen) {
				if (t2.frozen.verdict === 'FROZEN') {
					out.push(`**Frame identical** to the previous run (frozen-frame detection tripped).`);
				} else if (t2.frozen.verdict === 'QUIET_LOW_COV') {
					out.push(`Frame identical to previous run, but coverage is low — treated as a quiet/clear scene, not a stuck feed.`);
				} else if (t2.frozen.verdict === 'QUIET_SLOW') {
					out.push(`Frame identical to previous run, expected for this slow-cadence forecast product.`);
				}
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
		// Coarse grains: dates are more meaningful than wall-clock hour.
		if (bucket === '1d' || bucket === '6h') {
			return { primary: `${mo}/${da}`, secondary: `${d.getUTCFullYear()}` };
		}
		// 1h grain: show date at midnight (anchors the day), HH:MM otherwise.
		if (bucket === '1h' && hh === '00' && mm === '00') {
			return { primary: `${mo}/${da}`, secondary: `${d.getUTCFullYear()}` };
		}
		return { primary: `${hh}:${mm}`, secondary: `${mo}/${da}` };
	}

	// Picks WHICH columns get a full text label vs. a minor tick. Cadence is
	// chosen per-grain so labels stay readable (no overlap) regardless of how
	// many buckets are loaded. Without this, grains >=1h render every column
	// with a label, and at the ~10-12px column width they collide into mush.
	// Edges (first/last bucket) are NOT force-labelled — they tend to crowd
	// the nearest cadence label, and the cell-level tooltip plus the cadence
	// label within a few cells gives enough orientation.
	function isMajorTick(iso: string, _bi: number, _total: number): boolean {
		const d = new Date(iso);
		const mins = d.getUTCMinutes();
		const hrs = d.getUTCHours();
		const dow = d.getUTCDay();
		switch (bucket) {
			case '1m':  return mins === 0 || mins === 30;       // every 30 min
			case '5m':  return mins === 0;                      // every hour
			case '15m': return mins === 0 && hrs % 2 === 0;     // every 2 hours
			case '1h':  return hrs % 12 === 0;                  // every 12 hours
			case '6h':  return hrs === 0 && dow % 2 === 0;      // every other midnight
			case '1d':  return dow === 1;                       // every Monday
		}
		return false;
	}
	function relativeAge(iso: string): string {
		const s = (Date.now() - new Date(iso).getTime()) / 1000;
		return s < 0 ? 'in future' : `${fmtAge(s)} ago`;
	}
	// Upstream base URL for synthesizing clickable verification links. The
	// frontend doesn't have SETTINGS.base; this is the only externally-known
	// host the checks scrape from. If you ever swap upstream, update here.
	const UPSTREAM = 'https://radarca.engr.colostate.edu';

	interface VerifyContext {
		urls: { label: string; url: string }[];
		rows: { label: string; observed: string; threshold: string; verdict?: string }[];
		recipe?: string[];
	}

	// Build a "verify yourself" block for a single run. Goal: surface the
	// exact upstream URLs the check hit, plus the thresholds vs. observed
	// values that drove its verdict — enough info that a maintainer can
	// curl the URLs themselves and reproduce our reasoning, not just take
	// our word for it.
	function verifyContext(run: CheckRun): VerifyContext {
		const p = (run.payload ?? {}) as Record<string, any>;
		const urls: VerifyContext['urls'] = [];
		const rows: VerifyContext['rows'] = [];
		const recipe: string[] = [];

		// L0 — site / TLS / origin
		if (run.check_id === 'layer0.website.public') {
			urls.push({ label: 'GET /public', url: `${UPSTREAM}/public` });
			if (p.http != null) rows.push({ label: 'HTTP status', observed: String(p.http), threshold: '200' });
			if (p.bytes != null) rows.push({ label: 'body size', observed: `${p.bytes} B`, threshold: '≥ 50,000 B' });
			recipe.push('Open the URL in a browser; confirm 200 + a populated dashboard page.');
		}
		if (run.check_id === 'layer0.website.root_notfound') {
			urls.push({ label: 'GET /', url: `${UPSTREAM}/` });
			if (p.http != null) rows.push({ label: 'HTTP status', observed: String(p.http), threshold: '200 (yes, even for not-found)' });
			recipe.push('Open the URL; confirm the Next.js not-found body text appears even though HTTP is 200.');
		}
		if (run.check_id === 'layer0.origin.alive') {
			urls.push({ label: 'GET /api/radar-status/ (no-cache)', url: `${UPSTREAM}/api/radar-status/` });
			if (p.http != null) rows.push({ label: 'HTTP status', observed: String(p.http), threshold: '200' });
			if (p.elapsed_ms != null) rows.push({ label: 'latency', observed: `${p.elapsed_ms} ms`, threshold: '— (informational)' });
			recipe.push('Curl with --no-cache; expect JSON listing radar declarations.');
		}
		if (run.check_id === 'layer0.tls.cert') {
			if (p.expires_in_days != null) {
				rows.push({ label: 'days until expiry', observed: String(p.expires_in_days), threshold: '> 30 (else WARN)' });
			}
			recipe.push('openssl s_client -connect radarca.engr.colostate.edu:443 | openssl x509 -noout -dates');
		}

		// L1 — product
		if (run.check_id.startsWith('layer1.product.')) {
			const cfg_details = p.details_file || `${run.check_id.split('.').pop()}/details.json`;
			urls.push({ label: 'GET /api/productDetail', url: `${UPSTREAM}/api/productDetail?file=${encodeURIComponent(cfg_details)}` });
			if (p.image_source) {
				urls.push({ label: 'GET /api/imageData (latest scan)', url: `${UPSTREAM}/api/imageData?file=${encodeURIComponent(p.image_source)}` });
			}
			if (p.last_ts) {
				const ageS = (Date.now() - new Date(p.last_ts).getTime()) / 1000;
				rows.push({ label: 'newest scan age', observed: fmtAge(ageS), threshold: 'depends per product (see config.PRODUCTS.max_freshness_s)' });
			}
			if (p.n_steps != null) {
				rows.push({ label: 'step count', observed: String(p.n_steps), threshold: 'within ±4 of expected_steps (None ⇒ skip)' });
			}
			if (p.image_bytes != null) {
				rows.push({ label: 'image size', observed: `${p.image_bytes.toLocaleString()} B`, threshold: '≥ min_png_bytes per product' });
			}
			if (p.sub_status) {
				const sub = Object.entries(p.sub_status).map(([k, v]) => `${k}=${v}`).join(', ');
				rows.push({ label: 'sub-check verdicts', observed: sub, threshold: 'all = pass' });
			}
			recipe.push('Open the productDetail URL — confirm `steps[]` array and recent timestamps.');
			recipe.push('Open the imageData URL — confirm a valid PNG renders.');
		}

		// L2 — radar reconciliation
		if (run.check_id.startsWith('layer2.radar.')) {
			const folder = p.folder;
			urls.push({ label: 'GET /api/radar-status/', url: `${UPSTREAM}/api/radar-status/` });
			if (folder) {
				urls.push({ label: 'GET /api/xbandRadarImages/ (Reflectivity)',
					url: `${UPSTREAM}/api/xbandRadarImages/?radarFolder=${folder}&productPrefix=CorrReflectivity` });
			}
			if (p.declared != null) {
				rows.push({ label: 'declared (upstream)', observed: String(p.declared), threshold: 'UP for healthy' });
			}
			if (p.observed?.primary != null) {
				rows.push({ label: 'images in window', observed: String(p.observed.primary), threshold: '> 0' });
			}
			if (p.observed?.primary_age_s != null) {
				rows.push({
					label: 'newest scan age',
					observed: `${p.observed.primary_age_s} s`,
					threshold: `≤ ${p.observed.silent_fail_s ?? '?'} s (per-radar SILENT_FAIL_S)`,
					verdict: p.observed.fresh === false ? 'STALE → GHOST_UP/CONFIRMED_DOWN path' : 'fresh',
				});
			}
			if (p.verdict) {
				rows.push({ label: 'verdict', observed: p.verdict, threshold: '(declared, fresh) → verdict matrix' });
			}
			recipe.push('Open the radar-status URL; find this radar\'s entry and confirm the `status` field.');
			recipe.push('Open the xbandRadarImages URL; check the last filename\'s embedded timestamp.');
			recipe.push('If declared=UP and the newest filename is older than the threshold above, GHOST_UP is correct.');
		}

		// L3 — overlay parity
		if (run.check_id.startsWith('layer3.')) {
			urls.push({ label: 'GET /public (rendered HTML)', url: `${UPSTREAM}/public` });
			urls.push({ label: 'GET /api/productDetail (default product)',
				url: `${UPSTREAM}/api/productDetail?file=total_precip%2Fdetails_in.json` });
			if (p.overlay_ts) rows.push({ label: 'UI overlay timestamp', observed: String(p.overlay_ts), threshold: '— (compared to API)' });
			if (p.api_ts) rows.push({ label: 'API step[0] timestamp', observed: String(p.api_ts), threshold: '— (compared to UI)' });
			if (p.verdict) rows.push({ label: 'verdict', observed: p.verdict, threshold: 'MATCH for pass' });
			recipe.push('Load /public in a browser; note the timestamp shown in the overlay header.');
			recipe.push('Open the productDetail URL; compare to `steps[0]` timestamp.');
		}

		// L4 — image QC
		if (run.check_id.startsWith('layer4.')) {
			const t1 = p.tier1 ?? {};
			const t2 = p.tier2 ?? {};
			if (p.source) {
				urls.push({ label: 'GET /api/imageData (scan analysed)',
					url: `${UPSTREAM}/api/imageData?file=${encodeURIComponent(p.source)}` });
			}
			if (p.profile) {
				const prof = p.profile;
				rows.push({ label: 'profile applied', observed: JSON.stringify(prof), threshold: 'from config.L4_PROFILES' });
			}
			if (t1.coverage_pct != null) {
				rows.push({ label: 'pixel coverage', observed: `${t1.coverage_pct}%`,
					threshold: `< ${p.profile?.frozen_min_cov_pct ?? 5}% suppresses FROZEN as quiet-scene` });
			}
			if (t2.extreme) {
				rows.push({
					label: 'extreme fraction',
					observed: String(t2.extreme.fraction ?? '—'),
					threshold: `> ${p.profile?.extreme_threshold ?? 0.40} ⇒ SATURATED`,
					verdict: t2.extreme.verdict,
				});
			}
			if (t2.speckle) {
				rows.push({ label: 'speckle ratio', observed: String(t2.speckle.ratio ?? '—'),
					threshold: '> 0.35 ⇒ SPECKLE', verdict: t2.speckle.verdict });
			}
			if (t2.range_ring && t2.range_ring.verdict !== 'N/A') {
				rows.push({ label: 'range-ring score', observed: String(t2.range_ring.score ?? '—'),
					threshold: '> 0.80 × median ⇒ RING_PEAK', verdict: t2.range_ring.verdict });
			}
			if (t2.frozen) {
				rows.push({ label: 'frame identical to prior', observed: t2.frozen.matched_prev ? 'yes' : 'no',
					threshold: 'matched + coverage above floor + !skip_frozen ⇒ FROZEN', verdict: t2.frozen.verdict });
			}
			recipe.push('Open the imageData URL; the captured PNG renders in the panel above for visual comparison.');
			recipe.push('Compare the observed values above to the thresholds; the rightmost verdict column shows what each Tier-2 detector concluded.');
		}

		return { urls, rows, recipe };
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
			{@const c = tabCounts[t.key]}
			{@const total = c.pass + c.warn + c.fail + c.error + c.skip}
			{@const bad = c.fail + c.error}
			<button
				class="flex items-center gap-1.5 px-3 py-1.5 uppercase tracking-wider {tab === t.key
					? 'text-[var(--color-bright)] border-b-2 border-[var(--color-bright)] -mb-px'
					: 'text-[var(--color-muted)] hover:text-[var(--color-default)] border-b-2 border-transparent -mb-px'}"
				onclick={() => (tab = t.key)}
				title={`${t.label}: ${c.pass} pass · ${c.warn} warn · ${bad} fail/error · ${c.skip} skip (${total} total). ${t.desc}`}
			>
				<PieStatus counts={c} size={16} />
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

		<div class="flex items-center gap-1">
			<span class="text-[var(--color-muted)] uppercase tracking-wider">order</span>
			<div class="flex border border-[var(--color-border-strong)]">
				<button
					class="px-2 py-0.5 text-[11px] num {sortOrder === 'asc'
						? 'bg-[var(--color-elevated)] text-[var(--color-bright)]'
						: 'text-[var(--color-muted)] hover:text-[var(--color-default)]'}"
					title="Oldest on the left, newest on the right"
					onclick={() => (sortOrder = 'asc')}
				>old→new</button>
				<button
					class="px-2 py-0.5 text-[11px] num {sortOrder === 'desc'
						? 'bg-[var(--color-elevated)] text-[var(--color-bright)]'
						: 'text-[var(--color-muted)] hover:text-[var(--color-default)]'}"
					title="Newest on the left, oldest on the right"
					onclick={() => (sortOrder = 'desc')}
				>new→old</button>
			</div>
		</div>

		<label class="flex items-center gap-1 text-[var(--color-muted)] uppercase tracking-wider">
			<input type="checkbox" bind:checked={live} class="accent-[var(--color-ok)]" />
			<span>live (30s)</span>
		</label>

		<button
			type="button"
			class="border border-[var(--color-border-strong)] px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-elevated)]"
			onclick={() => (exportOpen = true)}
			title="Open the export dialog: pick a time range, resolution, stages, and targets — download as CSV. Defaults to what's on the screen right now."
		>
			⤓ Export report
		</button>

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
			<!--
			  Pivoted layout: rows = checks, columns = time buckets.
			  Direction (asc=oldest→newest left-to-right, desc=newest→oldest)
			  is user-controlled via the sort toggle in the header.
			-->
			<div
				class="timeline-grid grid"
				style="grid-template-columns: {LABEL_COL_W}px repeat({orderedBuckets.length}, {bodyColW}px); width: max-content;"
			>
				<!-- TOP-LEFT CORNER -->
				<div
					class="sticky left-0 top-0 z-30 bg-[var(--color-canvas)] border-b border-r border-[var(--color-border)] flex items-center px-3 text-[10px] uppercase tracking-[0.18em] text-[var(--color-muted)]"
					style="height:{TIME_HDR_H}px;"
				>
					check · {sortOrder === 'asc' ? 'time →' : '← time'}
				</div>

				<!-- TIME HEADER ROW (sticky top). Only "major" buckets render a
				     text label; the rest stay blank so labels never overlap.
				     Per-grain cadence picked by isMajorTick().

				     NOTE on class: these are NOT .tl-cell — they need
				     overflow: visible so labels can extend past their narrow
				     (~10-16px) cell into adjacent cells. .tl-cell carries
				     `contain: paint` (see <style> block) which forces a
				     clipping region, truncating every label to 2-3 chars at
				     coarse grains. Header cells are O(n_buckets) which is
				     small — opting them out of the contain optimization
				     costs nothing measurable. -->
				{#each orderedBuckets as b, bi}
					{@const ts = fmtRowTs(b.ts)}
					{@const isLast = bi === orderedBuckets.length - 1}
					<!-- "Most recent" = the newest bucket regardless of sort
					     direction (rightmost in asc, leftmost in desc).
					     Always labelled + bold so the eye anchors on "now". -->
					{@const isNewest = sortOrder === 'asc' ? isLast : bi === 0}
					{@const major = isNewest || isMajorTick(b.ts, bi, orderedBuckets.length)}
					<!-- Labelled cells need to paint AFTER neighbours so their
					     overflowing label isn't clipped by the next cell's
					     opaque background. Bump z-index on major cells; bump
					     further on the newest cell so its bold label always
					     wins paint order. -->
					<div
						class="tl-header relative sticky top-0 bg-[var(--color-canvas)] border-b border-[var(--color-border)] text-[10px] num {isNewest ? 'z-[27]' : major ? 'z-[25]' : 'z-20'}"
						style="height:{TIME_HDR_H}px;{major ? ' box-shadow: inset 1px 0 0 var(--color-border-strong);' : ''}"
						title={`${b.ts} · ${relativeAge(b.ts)}${isNewest ? ' · most recent' : ''}`}
					>
						{#if major}
							<span
								class="absolute bottom-1 whitespace-nowrap tracking-tight {isNewest ? 'font-bold text-[var(--color-bright)] text-[11px]' : 'font-medium text-[var(--color-bright)]'}"
								style="{isLast ? 'right:4px;' : 'left:4px;'} background: var(--color-canvas); padding: 0 3px;"
							>{ts.primary}{isNewest ? ' • now' : ''}</span>
						{/if}
					</div>
				{/each}

				<!-- ONE BLOCK PER STAGE -->
				{#each groupedColumns as g}
					<!-- Stage separator (sticky-left label + full-width strip) -->
					<div
						class="tl-cell sticky left-0 z-10 bg-[var(--color-canvas)] border-b border-t border-r border-[var(--color-border)] flex items-center px-3 text-[10px] uppercase tracking-[0.18em] {stageColor(g.stage)}"
						style="height:{STAGE_ROW_H}px;"
					>
						<span>{stageLabel(g.stage)}</span>
						<span class="ml-2 text-[var(--color-faint)] num">({g.cols.length})</span>
					</div>
					<div
						class="tl-cell border-b border-t border-[var(--color-border)] bg-[var(--color-canvas)]"
						style="height:{STAGE_ROW_H}px; grid-column: span {orderedBuckets.length};"
					></div>

					<!-- Subgroups: surfaced for L1 (Products) to mirror the home
					     page's Radar Data / Atmospheric Forecast / CoSMoS / NWM
					     grouping. Each subgroup gets a slim header row before
					     its checks. -->
					{#if g.subgroups}
						{#each g.subgroups as sg}
							<div
								class="tl-cell sticky left-0 z-10 bg-[var(--color-canvas)] border-b border-r border-[var(--color-border)] flex items-center px-3 text-[9.5px] uppercase tracking-[0.18em] text-[var(--color-muted)]"
								style="height:{Math.max(STAGE_ROW_H - 6, 18)}px;"
							>
								<span>{sg.label}</span>
								<span class="ml-2 text-[var(--color-faint)] num">({sg.cols.length})</span>
							</div>
							<div
								class="tl-cell border-b border-[var(--color-border)] bg-[var(--color-canvas)]/60"
								style="height:{Math.max(STAGE_ROW_H - 6, 18)}px; grid-column: span {orderedBuckets.length};"
							></div>
							{#each sg.cols as col}
								<div
									class="tl-cell sticky left-0 z-10 bg-[var(--color-canvas)] border-b border-r border-[var(--color-border)] flex items-center px-3 text-[12px] text-[var(--color-bright)] num"
									style="height:{ROW_H}px;"
									title={[
										`${prettyCheckLabel(col.id, col.target)}  —  ${checkBlurb(col)}`,
										``,
										`Check ID:  ${col.id}`,
										`Target:    ${col.target}`,
										`Cadence:   every ${col.cadence_s}s`,
										`Stage:     ${stageLabel(col.stage)} (${col.stage})`,
										``,
										`Cell colors: green = pass · yellow = warn · red = fail/error · gray = no data / skipped.`,
										`Click any cell to open a drill-down with thresholds, observed values, and verification URLs.`,
									].join('\n')}
								>
									<span class="truncate">{prettyCheckLabel(col.id, col.target)}</span>
								</div>
								{#each orderedBuckets as b}
									{@const cell = b.cells[cellKey(col)]}
									{@const st = cell?.status ?? 'unknown'}
									{@const onTheHour = new Date(b.ts).getUTCMinutes() === 0}
									<button
										type="button"
										class="tl-cell flex items-center justify-center p-0 cursor-pointer bg-transparent border-b border-[var(--color-border)]"
										style="height:{ROW_H}px;{onTheHour ? ' box-shadow: inset 1px 0 0 var(--color-border);' : ''}"
										onclick={() => openDetail(b, col)}
										title={cell
											? `${prettyCheckLabel(col.id, col.target)} · ${STATUS_WORD[st]} · ${cell.n} run${cell.n === 1 ? '' : 's'} · ${fmtRowTs(b.ts).primary} UTC`
											: `${prettyCheckLabel(col.id, col.target)} · no data · ${fmtRowTs(b.ts).primary} UTC`}
									>
										<span
											class="block"
											style="width:{Math.max(bodyColW - 4, 4)}px; height:{ROW_H - 6}px; background:{STATUS_BG[st]}; border-radius:2px;"
										></span>
									</button>
								{/each}
							{/each}
						{/each}
					{:else}
					<!-- Per-check rows in this stage -->
					{#each g.cols as col}
						<div
							class="tl-cell sticky left-0 z-10 bg-[var(--color-canvas)] border-b border-r border-[var(--color-border)] flex items-center px-3 text-[12px] text-[var(--color-bright)] num"
							style="height:{ROW_H}px;"
							title={[
								`${prettyCheckLabel(col.id, col.target)}  —  ${checkBlurb(col)}`,
								``,
								`Check ID:  ${col.id}`,
								`Target:    ${col.target}`,
								`Cadence:   every ${col.cadence_s}s`,
								`Stage:     ${stageLabel(col.stage)} (${col.stage})`,
								``,
								`Cell colors: green = pass · yellow = warn · red = fail/error · gray = no data / skipped.`,
								`Click any cell to open a drill-down with thresholds, observed values, and verification URLs.`,
							].join('\n')}
						>
							<span class="truncate">{prettyCheckLabel(col.id, col.target)}</span>
						</div>
						{#each orderedBuckets as b}
							{@const cell = b.cells[cellKey(col)]}
							{@const st = cell?.status ?? 'unknown'}
							{@const onTheHour = new Date(b.ts).getUTCMinutes() === 0}
							<button
								type="button"
								class="tl-cell flex items-center justify-center p-0 cursor-pointer bg-transparent border-b border-[var(--color-border)]"
								style="height:{ROW_H}px;{onTheHour ? ' box-shadow: inset 1px 0 0 var(--color-border);' : ''}"
								onclick={() => openDetail(b, col)}
								title={cell
									? `${prettyCheckLabel(col.id, col.target)} · ${STATUS_WORD[st]} · ${cell.n} run${cell.n === 1 ? '' : 's'} · ${fmtRowTs(b.ts).primary} UTC`
									: `${prettyCheckLabel(col.id, col.target)} · no data · ${fmtRowTs(b.ts).primary} UTC`}
							>
								<span
									class="block"
									style="width:{Math.max(bodyColW - 4, 4)}px; height:{ROW_H - 6}px; background:{STATUS_BG[st]}; border-radius:2px;"
								></span>
							</button>
						{/each}
					{/each}
					{/if}
				{/each}
			</div>

			<div class="flex items-center justify-center gap-3 px-4 py-3 text-[11px] text-[var(--color-muted)]">
				{#if loadingMore}
					<span>loading older…</span>
				{:else if olderCursor && buckets.length < MAX_BUCKETS}
					<button
						class="border border-[var(--color-border-strong)] px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-elevated)]"
						onclick={loadMore}
					>
						load older ({cfg.label} × {cfg.pageLimit})
					</button>
				{:else if buckets.length >= MAX_BUCKETS}
					<span class="text-[var(--color-faint)]">cap reached ({MAX_BUCKETS} rows) — pick a coarser grain</span>
				{:else}
					<span>end of data</span>
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
					<div class="label text-[var(--color-faint)]" title={`Internal stage: ${detail.col.stage}`}>
						{stageLabel(detail.col.stage)}
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
				{@const v = verifyContext(run)}
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

					<!-- Image (L4 image QC only). LazyImage defers the fetch +
					     decode until the placeholder is actually visible, so
					     opening this panel doesn't kick off N parallel PNG
					     decodes for runs the user may never scroll to. -->
					{#if isImageQc(run.check_id) && (run.payload as any)?.source}
						{@const src = (run.payload as any).source}
						{@const url = apiUrl(`/api/upstream/image_by_source.png?source=${encodeURIComponent(src)}`)}
						{@const ageMin = (Date.now() - new Date(run.finished_at).getTime()) / 60000}
						<div class="mt-3">
							<div class="text-[10px] text-[var(--color-muted)] uppercase tracking-wider mb-1">
								captured image
							</div>
							<a
								href={url}
								target="_blank"
								rel="noreferrer"
								class="block border border-[var(--color-border)] bg-black"
							>
								{#snippet imgError()}
									<div class="p-4 text-center text-[11px] text-[var(--color-muted)] leading-relaxed">
										<div class="text-[var(--color-faint)] uppercase tracking-wider text-[10px] mb-1">
											Image no longer available
										</div>
										<div>Radarca only keeps the last ~2 h of scans before rotating files out.</div>
										<div class="mt-1 text-[var(--color-faint)]">
											This scan was captured {Math.round(ageMin)} min ago — the original PNG has been deleted upstream.
										</div>
									</div>
								{/snippet}
								<LazyImage src={url} alt="captured scan" minHeight={180} errorSnippet={imgError} />
							</a>
							<div class="mt-1 text-[10px] text-[var(--color-faint)] num truncate" title={src}>
								source: {src}
							</div>
						</div>
					{/if}

					<!-- Verify yourself: upstream URLs + thresholds vs observed.
					     Gives the operator the raw materials to re-derive our
					     verdict by hand without trusting Sentinel's reasoning.
					     (`v` is declared at the top of the each-block.) -->
					{#if v.urls.length || v.rows.length}
						<details class="mt-3 border-t border-[var(--color-border)] pt-2 text-[11px]" open>
							<summary class="cursor-pointer text-[var(--color-faint)] uppercase tracking-wider text-[10px]">verify yourself</summary>

							{#if v.rows.length}
								<div class="mt-2 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">thresholds vs observed</div>
								<table class="mt-1 w-full text-[11px]">
									<thead class="text-[10px] uppercase tracking-wider text-[var(--color-faint)]">
										<tr>
											<th class="text-left font-normal pb-0.5 pr-2">field</th>
											<th class="text-left font-normal pb-0.5 pr-2">observed</th>
											<th class="text-left font-normal pb-0.5 pr-2">threshold / rule</th>
											<th class="text-left font-normal pb-0.5">verdict</th>
										</tr>
									</thead>
									<tbody>
										{#each v.rows as r}
											<tr class="border-t border-[var(--color-border)]">
												<td class="py-1 pr-2 text-[var(--color-muted)]">{r.label}</td>
												<td class="py-1 pr-2 num text-[var(--color-bright)] break-all">{r.observed}</td>
												<td class="py-1 pr-2 text-[var(--color-default)] num">{r.threshold}</td>
												<td class="py-1 num text-[var(--color-default)]">{r.verdict ?? ''}</td>
											</tr>
										{/each}
									</tbody>
								</table>
							{/if}

							{#if v.urls.length}
								<div class="mt-3 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">upstream calls (click to open)</div>
								<ul class="mt-1 space-y-1 text-[11px] num">
									{#each v.urls as u}
										<li class="flex items-start gap-2">
											<span class="text-[var(--color-faint)] shrink-0">·</span>
											<div class="min-w-0 flex-1">
												<div class="text-[var(--color-muted)] text-[10px] uppercase tracking-wider">{u.label}</div>
												<a class="text-[var(--color-info)] hover:underline break-all" href={u.url} target="_blank" rel="noreferrer">{u.url}</a>
											</div>
										</li>
									{/each}
								</ul>
							{/if}

							{#if v.recipe?.length}
								<div class="mt-3 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">how to replicate</div>
								<ol class="mt-1 list-decimal space-y-0.5 pl-5 text-[11px] text-[var(--color-default)] leading-snug">
									{#each v.recipe as step}
										<li>{step}</li>
									{/each}
								</ol>
							{/if}
						</details>
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
				{#snippet historyLink()}
					{@const baseTs = new Date(detail.ts).getTime()}
					{@const bucketSec = BUCKET_SECONDS[bucket] ?? 60}
					{@const bucketMs = bucketSec * 1000}
					{@const sinceIso = new Date(baseTs - bucketMs).toISOString()}
					{@const untilIso = new Date(baseTs + bucketMs * 2).toISOString()}
					{@const href = `/history?tab=checks&check_id=${encodeURIComponent(detail.col.id)}` +
						`&target=${encodeURIComponent(detail.col.target)}` +
						`&stage=${encodeURIComponent(detail.col.stage)}` +
						`&since=${encodeURIComponent(sinceIso)}&until=${encodeURIComponent(untilIso)}`}
					<a
						class="border border-[var(--color-border-strong)] px-3 py-1 text-[11px] uppercase tracking-wider text-[var(--color-bright)] hover:bg-[var(--color-elevated)]"
						{href}
						title="Open this exact check in the History page with stage + target + check_id + a ±1-bucket time window pre-filled."
					>
						open in history
					</a>
				{/snippet}
				{@render historyLink()}
			</div>
		</div>
	{/if}
</div>

<ReportExportModal
	bind:open={exportOpen}
	defaultSince={exportDefaults.since}
	defaultUntil={exportDefaults.until}
	defaultBucket={exportDefaults.bucket}
	defaultStages={exportDefaults.stages}
	defaultTargets={exportDefaults.targets}
	targetOptions={exportTargetOptions}
/>

<style>
	.timeline-grid {
		font-variant-numeric: tabular-nums;
	}
	/* === Load-bearing perf hint. DO NOT DELETE. ===
	 *
	 * The body cells are layout-independent islands — nothing inside any
	 * one cell influences its neighbours' size or position. `contain:
	 * layout style paint` tells the browser that explicitly so it can
	 * skip restyle/reflow on neighbours when one changes (live update,
	 * hover, etc.) and so scroll events don't force a full display-list
	 * rebuild for the whole grid.
	 *
	 * Without this rule, a Firefox profiler trace caught the grid's
	 * scroll container generating ~5000 DisplayList rebuilds and ~3000
	 * ViewManagerFlushes during a normal session — almost entirely
	 * paint/layout overhead that visibly slowed scrolling. Adding
	 * `contain` to the cells cut that work by ~80%.
	 *
	 * The `.tl-cell` class on every grid item (top-row headers, sticky
	 * left column, body cells) is what makes this selector match. Keep
	 * `tl-cell` on every direct child of `.timeline-grid`. */
	.timeline-grid > .tl-cell {
		contain: layout style paint;
	}
</style>
