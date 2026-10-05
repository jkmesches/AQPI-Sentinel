// Common formatters. Tabular figures throughout.

export function fmtAge(seconds: number, opts: { signed?: boolean } = {}): string {
	const total = Math.floor(Math.abs(seconds));
	const sign = opts.signed ? (seconds < 0 ? '-' : '+') : '';
	if (total < 60) return `${sign}${total}s`;
	if (total < 3600) {
		const m = Math.floor(total / 60);
		const s = total % 60;
		return `${sign}${m}m${s ? s + 's' : ''}`;
	}
	// >= 1h: compound (e.g. "6d12h37m")
	const d = Math.floor(total / 86400);
	const h = Math.floor((total % 86400) / 3600);
	const m = Math.floor((total % 3600) / 60);
	let out = '';
	if (d) out += `${d}d`;
	if (h || d) out += `${h}h`;
	out += `${m}m`;
	return `${sign}${out}`;
}

export function fmtBytes(b: number): string {
	if (b < 1024) return `${b} B`;
	if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
	return `${(b / 1024 / 1024).toFixed(1)} MB`;
}

export function fmtUtcClock(iso: string): string {
	const d = new Date(iso);
	return d.toISOString().slice(11, 19) + 'Z';
}

export function statusColor(s: string): string {
	return (
		{
			pass: 'text-[var(--color-ok)]',
			warn: 'text-[var(--color-warn)]',
			fail: 'text-[var(--color-fail)]',
			error: 'text-[var(--color-error)]',
			skip: 'text-[var(--color-muted)]'
		}[s] ?? 'text-[var(--color-muted)]'
	);
}

export function statusDotBg(s: string): string {
	return (
		{
			pass: 'bg-[var(--color-ok)]',
			warn: 'bg-[var(--color-warn)]',
			fail: 'bg-[var(--color-fail)]',
			error: 'bg-[var(--color-error)]',
			skip: 'bg-[var(--color-muted)]',
			critical: 'bg-[var(--color-critical)]',
			info: 'bg-[var(--color-info)]'
		}[s] ?? 'bg-[var(--color-muted)]'
	);
}

export function statusBorder(s: string): string {
	return (
		{
			pass: 'border-[var(--color-ok)]/40',
			warn: 'border-[var(--color-warn)]/40',
			fail: 'border-[var(--color-fail)]/45',
			error: 'border-[var(--color-error)]/45',
			skip: 'border-[var(--color-faint)]'
		}[s] ?? 'border-[var(--color-faint)]'
	);
}

export function statusText(s: string): string {
	return (
		{
			pass: 'text-[var(--color-ok)]',
			warn: 'text-[var(--color-warn)]',
			fail: 'text-[var(--color-fail)]',
			error: 'text-[var(--color-error)]',
			skip: 'text-[var(--color-muted)]',
			critical: 'text-[var(--color-critical)]',
			info: 'text-[var(--color-info)]'
		}[s] ?? 'text-[var(--color-muted)]'
	);
}

// === Stage vocabulary ===
//
// Internal stage IDs (L0…L4-T1T2) stay in the data layer for filtering and
// grouping. Anything rendered to a user goes through stageLabel().
// stageTechCode() returns the short technical tag (L0…L4) for tooltips
// and email footers so the mapping is still discoverable.
//
// Names approved 2026-05-18.
// The canonical stage list. EVERY enumeration of stages must derive from this —
// filters, pickers, timeline ordering, rollup groupings. v0.5.0 added LB1/LB2 and
// updated only the label lookups below, which left eight separate hard-coded copies
// of ['L0','L1','L2','L3','L4-T1T2'] silently dropping the new stages from the
// timeline, history, silence picker, report export and mobile views.
//
// Backend stages sit immediately after the radarca stage they correspond to, so the
// two views of the same thing are adjacent wherever stages are listed in order.
export const ALL_STAGES = ['L0', 'L1', 'LB1', 'L2', 'LB2', 'LB3', 'L3', 'L4-T1T2'] as const;
export type Stage = (typeof ALL_STAGES)[number];

/** Dropdown/filter options. `hint` carries the short technical code where the UI shows it. */
export function stageOptions(withHint = false): { value: string; label: string; hint?: string }[] {
	return ALL_STAGES.map((s) =>
		withHint
			? { value: s, label: stageLabel(s), hint: stageTechCode(s) }
			: { value: s, label: stageLabel(s) }
	);
}

/**
 * Which metric a check's sparkline plots, or null for checks that have none.
 *
 * One place, because the previous arrangement had the answer in three: the
 * store enumerated stages L1 and L2 to decide what to FETCH, and each rail
 * hard-coded a metric name to decide what to DRAW. The backend checks were
 * missing from the store's enumeration — doubly, since the L1 loop also
 * guarded on `layer1.product.` — so LB1 and LB2 rows asked for no series and
 * rendered an empty cell, while the data sat in metric_samples.
 *
 * That was the ninth hand-rolled stage enumeration. v0.5.1 found eight and
 * routed them through ALL_STAGES, but all eight were in .svelte pages and
 * this one is in a store, so the sweep missed it.
 *
 * All four families plot `headroom` — the fraction of the check's freshness
 * budget still unspent, 0..1, computed by backend.checks.helpers.headroom
 * against the very threshold the verdict uses. They deliberately plot the SAME
 * metric now. The radarca and backend radar checks even divide by the same
 * per-radar `silent_fail_s`, so when one trace diverges from the other that is
 * the two sources genuinely disagreeing rather than two scales disagreeing.
 *
 * What this replaced: radarca radars plotted `images_Reflectivity` and the
 * other three plotted `age_s`. Two shapes in one rail meant a reader had to
 * know which family a row belonged to before the trace meant anything, and
 * `age_s` is not comparable across rows anyway — the radar limits alone span
 * 300 s to 1080 s. The image count survives as the sparkline's trailing count
 * label; it is no longer the trace.
 */
/**
 * Which stage is the backend-read view of which radarca-derived stage.
 *
 * `LB1` reads the same products off the filesystem that `L1` scrapes from
 * radarca, and `LB2` does the same for the radars `L2` watches. They are two
 * observations of one thing, which is why the home rails pair them on a row
 * and the timeline puts them adjacent.
 *
 * One map rather than a check scattered across the surfaces that need it —
 * this codebase has been bitten repeatedly by stage knowledge living in
 * whichever file happened to need it first.
 */
export const BACKEND_STAGE_OF: Record<string, string> = { L1: 'LB1', L2: 'LB2' };

/** The backend counterpart of a stage, or null if it has none. */
export function backendStageOf(stage: string): string | null {
	return BACKEND_STAGE_OF[stage] ?? null;
}

/** True for a stage that is itself a backend view (so it is never a block of its own). */
export function isBackendStage(stage: string): boolean {
	return Object.values(BACKEND_STAGE_OF).includes(stage);
}

/**
 * The X-band fleet correlation check, which is not a radar.
 *
 * It shares stage L2 with the six radars and used to render as a seventh row
 * in the Radars rail — with a UP/DOWN word, an empty sparkline (it measures a
 * count, not freshness, so it records no `headroom`), no image-QC dot, and a
 * target of `xband-fleet` that truncated to `xband-…`. It also made the rail
 * read "5/7" when there are six radars.
 *
 * It is a verdict ABOUT the rail, so it renders above it as a banner and only
 * when it has something to say. See backend/checks/layer2_radar.py.
 */
export const FLEET_CHECK_ID = 'layer2.xband.fleet';

/**
 * The backend-side fleet correlation, added 2026-10-05.
 *
 * Same shape and same reason as the L2 one — a verdict ABOUT the rail rather
 * than a row in it — but it reads the filesystem, so it can say something the
 * radarca-derived check cannot: CBAND arrives on a separate mount, so a
 * passing CBAND localises the fault to the X-band path. See
 * backend/checks/layer2_backend_radar.Layer2BackendFleetCheck.
 */
export const BACKEND_FLEET_CHECK_ID = 'layer2.backend.fleet';

/**
 * True for a correlation verdict that must NOT be rendered as a radar row.
 *
 * Both ids, because both share a stage with the radars they describe. Missing
 * the second would put `radar-fleet` in the rail as a seventh radar with an
 * empty sparkline — exactly the bug the L2 one was pulled out to fix, and the
 * rail's "5/7" count would come back with it.
 */
export function isFleetCheck(checkId: string): boolean {
	return checkId === FLEET_CHECK_ID || checkId === BACKEND_FLEET_CHECK_ID;
}

export function sparklineMetric(checkId: string): string | null {
	if (checkId.startsWith('layer2.radar.'))   return 'headroom';
	if (checkId.startsWith('layer2.backend.')) return 'headroom';
	if (checkId.startsWith('layer1.product.')) return 'headroom';
	if (checkId.startsWith('layer1.backend.')) return 'headroom';
	// LB3. Participation plots headroom against its own contribution-age band
	// so it shares the fixed 0..1 axis with everything else; the DROPS producer
	// does too, against its informational limit.
	if (checkId.startsWith('layer3.composite.')) return 'headroom';
	if (checkId === 'layer3.backend.drops')      return 'headroom';
	return null;
}

/**
 * Which of a target's several checks owns the row's ARRIVAL trace.
 *
 * A radar now reports through up to three checks and they do not all measure
 * arrival: LB2 reads the raw volumes landing on disk, LB3 reads a composite
 * receipt written two steps later, and L2 reads what radarca says about it.
 * Only the first is data arrival. The collapsed row plots that one, so the
 * trace means the same thing on every row and is never quietly a measure of
 * the composite driver's health instead.
 *
 * Falls back to whatever the target does have — on a profile with no backend
 * mount there is no LB2 reading, and a row with no trace at all is worse than
 * one whose trace is the only reading available.
 */
export const ARRIVAL_STAGE_PRIORITY = ['LB2', 'L2', 'LB3'] as const;

export function arrivalRowOf<T extends { stage: string }>(rows: readonly T[]): T | null {
	for (const stage of ARRIVAL_STAGE_PRIORITY) {
		const hit = rows.find((r) => r.stage === stage);
		if (hit) return hit;
	}
	return rows[0] ?? null;
}

/**
 * A second series a rail shows as a NUMBER rather than as a trace, or null.
 *
 * The products rail prints each row's newest age next to it. That used to come
 * along for free because the sparkline itself plotted `age_s`; once the trace
 * became `headroom` nothing fetched `age_s` any more and the readout sat at
 * "—" permanently. It is a separate concern from what gets drawn, so it is a
 * separate question — and the store fetches the union of the two.
 */
export function readoutMetric(checkId: string): string | null {
	if (checkId.startsWith('layer1.product.')) return 'age_s';
	if (checkId.startsWith('layer1.backend.')) return 'age_s';
	return null;
}

/**
 * The fixed y range a metric is drawn on, or null to autoscale to the window.
 *
 * `headroom` is already normalized to its own budget, so it gets a FIXED 0..1
 * axis and the sparkline stops rescaling. That is the whole point of the
 * metric: under autoscale a steady series and a dead one both collapsed to the
 * baseline (identical pixel for pixel), and a 2% wobble drew the same
 * full-height zigzag as a 6x swing. Anything else keeps the old behaviour.
 */
export function sparklineDomain(metric: string | null): [number, number] | null {
	return metric === 'headroom' ? [0, 1] : null;
}

/**
 * Where to draw the sparkline's warn rule, in the metric's own units, or null
 * for none. 0.2 headroom is the band layer2_backend_radar warns at
 * (`age_s <= silent_s * 0.8`), so the rule marks a real boundary rather than a
 * decorative gridline.
 */
export function sparklineWarnAt(metric: string | null): number | null {
	return metric === 'headroom' ? 0.2 : null;
}

export function stageColor(s: string): string {
	return (
		{
			L0:        'text-[var(--color-info)]',
			L1:        'text-[var(--color-default)]',
			L2:        'text-[var(--color-bright)]',
			L3:        'text-[var(--color-warn)]',
			'L4-T1T2': 'text-[var(--color-critical)]',
			LB1:       'text-[var(--color-bright)]',
			LB2:       'text-[var(--color-bright)]',
			LB3:       'text-[var(--color-bright)]'
		}[s] ?? 'text-[var(--color-muted)]'
	);
}
export function stageLabel(s: string): string {
	return (
		{
			L0:        'Connectivity',
			L1:        'Product Freshness',
			L2:        'Radar Scans',
			L3:        'Map Overlays',
			'L4-T1T2': 'Image Quality',
			LB1:       'Backend Products',
			LB2:       'Backend Radar Arrival',
			LB3:       'Backend Processing'
		}[s] ?? s
	);
}
export function stageTechCode(s: string): string {
	return (
		{
			L0:        'L0',
			L1:        'L1',
			L2:        'L2',
			L3:        'L3',
			'L4-T1T2': 'L4',
			LB1:       'LB1',
			LB2:       'LB2',
			LB3:       'LB3'
		}[s] ?? s
	);
}
export function stageTooltip(s: string): string {
	const label = stageLabel(s);
	const code = stageTechCode(s);
	return label === code ? label : `${label} (${code})`;
}

// Per-product human display labels. Source of truth: backend/config.py
// PRODUCTS table. Map the underscored identifiers (qpe_15min, comp_ref,
// fcst_total_precip_cum, …) to the names a scientist actually says out
// loud. Used by prettyCheckLabel for layer1.product.* + layer4.mosaic.*
// and exported standalone for any UI that displays a bare product_id
// (composite menus, push routing filters, etc.).
//
// Anything not in this map falls through to a title-cased
// underscore-replaced version of the id — fine for one-off products.
const PRODUCT_LABELS: Record<string, string> = {
	qpe_15min:             'Total Precip · 15 min QPE',
	qpe_1hr:               'Total Precip · 1 hour QPE',
	precip_rate_radar:     'Precip Rate (radar)',
	comp_ref:              'Composite Reflectivity',
	comp_now:              'Reflectivity Nowcast',
	fcst_total_precip:     'Forecast · Total Precip',
	fcst_total_precip_cum: 'Forecast · Total Precip (cumulative)',
	fcst_precip_rate:      'Forecast · Precip Rate',
	fcst_temp:             'Forecast · Temperature',
	water_level:           'Water Level',
	water_depth:           'Water Depth',
	max_water_level:       'Max Water Level',
	max_water_depth:       'Max Water Depth'
};

// Friendly names for L0 site checks + L1 vector overlays + L1 streams.
// Keys are the trailing `target` slug; full mapping kept separate from
// product names so we don't conflate radar imagery products with
// site/stream/overlay assets.
const L0_TARGET_LABELS: Record<string, string> = {
	tls_cert:                  'TLS certificate',
	origin_alive:              'Origin reachable',
	'origin-episode':          'Upstream slow episode',
	'origin-latency':          'Upstream latency canary',
	public:                    'Public dashboard page',
	root_notfound:             'Root URL (404 check)',
	website_public:            'Public dashboard page',
	website_root_notfound:     'Root URL (404 check)',
	// Sentinel-* labels surface OUR infrastructure separately from the
	// monitored upstream. "Sentinel Internet" = can we reach the public
	// internet at all; "Sentinel DNS" = can our resolver translate
	// hostnames. Split out 2026-05-19 so radarca outages don't confuse
	// upstream-side problems with our own.
	internet:                  'Sentinel Internet',
	dns:                       'Sentinel DNS'
};
const VECTOR_TARGET_LABELS: Record<string, string> = {
	flowlines:        'Stream flowlines',
	watersheds:       'Watersheds',
	station_markers:  'Station markers',
	stations:         'Stations'
};
const STREAM_TARGET_LABELS: Record<string, string> = {
	stream:        'Stream gauges (live)',
	stream_csv:    'Stream gauges (CSV)',
	stream_fcst:   'Stream gauges (forecast)'
};

export function productLabel(productId: string): string {
	return PRODUCT_LABELS[productId] ?? titleCase(productId.replaceAll('_', ' '));
}

// Category map for the Live page's Products section. Mirrors the upstream
// radarca layout: radar-derived imagery, atmospheric forecast (Forecast
// suite), CoSMoS hydro (water level/depth from the Coastal Storm Modeling
// System), and a placeholder for National Water Model — radarca doesn't
// expose any NWM products today but the category is reserved so future
// products surface in the right place.
export type ProductCategory = 'radar' | 'forecast' | 'cosmos' | 'nwm' | 'other';
export const PRODUCT_CATEGORY_ORDER: ProductCategory[] = [
	'radar', 'forecast', 'cosmos', 'nwm', 'other'
];
export const PRODUCT_CATEGORY_LABEL: Record<ProductCategory, string> = {
	radar:     'Radar Data',
	forecast:  'Atmospheric Forecast',
	cosmos:    'CoSMoS Data',
	nwm:       'National Water Model',
	other:     'Other'
};
const PRODUCT_CATEGORIES: Record<string, ProductCategory> = {
	qpe_15min:             'radar',
	qpe_1hr:               'radar',
	precip_rate_radar:     'radar',
	comp_ref:              'radar',
	comp_now:              'radar',

	fcst_total_precip:     'forecast',
	fcst_total_precip_cum: 'forecast',
	fcst_precip_rate:      'forecast',
	fcst_temp:             'forecast',

	water_level:           'cosmos',
	water_depth:           'cosmos',
	max_water_level:       'cosmos',
	max_water_depth:       'cosmos',

	// Stream Reach feeds sourced from the National Water Model on
	// radarca. flowlines + watersheds are vector overlays; stream
	// (live) + stream_csv are the gauge data feeds. Confirmed
	// 2026-05-18 with the maintainer — these all derive from NWM.
	flowlines:             'nwm',
	watersheds:            'nwm',
	stream:                'nwm',
	stream_csv:            'nwm'
};

export function productCategory(productId: string): ProductCategory {
	return PRODUCT_CATEGORIES[productId] ?? 'other';
}

// Turn a check_id + target into something a non-engineer can read at a glance.
// We special-case the major check families; everything else falls back to a
// cleaned-up version of the target.
export function prettyCheckLabel(checkId: string, target: string): string {
	const tDash = target.replaceAll('_', ' ');
	if (checkId.startsWith('layer0.tls.'))           return 'TLS certificate';
	if (checkId === 'layer0.origin.episode')         return 'Upstream slow episode';
	if (checkId === 'layer0.origin.latency')         return 'Upstream latency canary';
	if (checkId.startsWith('layer0.origin.'))        return 'Origin reachable';
	if (checkId.startsWith('layer0.website.public')) return 'Public dashboard page';
	if (checkId.startsWith('layer0.website.root'))   return 'Root URL (404 check)';
	if (checkId.startsWith('layer0.net.internet'))   return 'Sentinel Internet';
	if (checkId.startsWith('layer0.net.dns'))        return 'Sentinel DNS';
	if (checkId.startsWith('layer0.'))               return L0_TARGET_LABELS[target] ?? titleCase(tDash);
	if (checkId.startsWith('layer1.product.'))       return productLabel(target);
	if (checkId.startsWith('layer1.backend.'))       return `${productLabel(target)} · backend`;
	if (checkId.startsWith('layer1.stream.'))        return STREAM_TARGET_LABELS[target] ?? `Stream · ${titleCase(tDash)}`;
	if (checkId.startsWith('layer1.vector.'))        return VECTOR_TARGET_LABELS[target] ?? `Overlay · ${titleCase(tDash)}`;
	if (checkId.startsWith('layer2.radar.'))         return target;            // XSCV / CBAND — keep radar IDs as-is
	// Both fleet correlation checks, BEFORE the layer2.backend. branch they
	// would otherwise fall into. The backend one did, and rendered as
	// "radar-fleet · backend" — a correlation verdict labelled as an arrival
	// reading, which is the one thing it is not.
	if (checkId === BACKEND_FLEET_CHECK_ID)          return 'Radar fleet · correlation';
	if (checkId === FLEET_CHECK_ID)                  return 'X-band fleet · correlation';
	if (checkId.startsWith('layer2.backend.'))       return `${target} · backend`;
	// LB3, BEFORE the generic layer3. branch. First match wins, and that
	// branch was written for the overlay-parity check: it caught
	// layer3.composite.* and layer3.backend.drops and labelled them
	// "Reconcile · …", which describes nothing either of them does. A
	// participation check reports whether a radar is IN the composite and how
	// stale its contribution was.
	//
	// The label stays the same whichever source answered. "in composite"
	// versus "offered to composite" is carried by the SUMMARY, where it
	// belongs — a row whose label changed when the receipt fallback engaged
	// would read as a different check rather than a weaker reading.
	if (checkId.startsWith('layer3.composite.'))     return `${target} · in composite`;
	if (checkId === 'layer3.backend.drops')          return 'DROPS producer';
	if (checkId.startsWith('layer3.'))               return `Reconcile · ${productLabel(target)}`;
	if (checkId.startsWith('layer4.xband.'))         return target;            // radar IDs
	if (checkId.startsWith('layer4.'))               return productLabel(target);
	return titleCase(tDash);
}
function titleCase(s: string): string {
	return s.replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Pick a sparkline display window from a check's cadence. Targets ~30
 * cadence intervals visible at once, snapped to a natural unit so the
 * label reads sensibly. The label is suitable for inline rendering as
 * "N/{label}" — e.g. "16/h" or "0/6h". Used by Sparkline.svelte so the
 * window auto-adapts: products at 30s cadence get a 30-min window,
 * forecast products at 30m cadence get a multi-hour window, etc.
 */
export function windowFromCadence(cadenceS: number | undefined | null): { ms: number; label: string } {
	const c = cadenceS && cadenceS > 0 ? cadenceS : 60;
	const targetMin = (c * 30) / 60;
	// Ordered ascending. First entry whose minutes >= target wins.
	const choices: [number, string][] = [
		[30, '30m'], [60, 'h'], [120, '2h'], [180, '3h'], [360, '6h'], [720, '12h']
	];
	for (const [mins, label] of choices) {
		if (mins >= targetMin) return { ms: mins * 60_000, label };
	}
	return { ms: 720 * 60_000, label: '12h' };
}

export function severityChip(s: string): string {
	const base = 'px-1 py-0 text-[10px] uppercase tracking-wider rounded-sm border';
	const v =
		{
			info: 'text-[var(--color-info)] border-[var(--color-info)]/40',
			warn: 'text-[var(--color-warn)] border-[var(--color-warn)]/40',
			critical: 'text-[var(--color-critical)] border-[var(--color-critical)]/50'
		}[s] ?? 'text-[var(--color-muted)] border-[var(--color-muted)]/40';
	return `${base} ${v}`;
}
