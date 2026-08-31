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
export function stageColor(s: string): string {
	return (
		{
			L0:        'text-[var(--color-info)]',
			L1:        'text-[var(--color-default)]',
			L2:        'text-[var(--color-bright)]',
			L3:        'text-[var(--color-warn)]',
			'L4-T1T2': 'text-[var(--color-critical)]'
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
			'L4-T1T2': 'Image Quality'
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
			'L4-T1T2': 'L4'
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
	if (checkId.startsWith('layer0.origin.'))        return 'Origin reachable';
	if (checkId.startsWith('layer0.website.public')) return 'Public dashboard page';
	if (checkId.startsWith('layer0.website.root'))   return 'Root URL (404 check)';
	if (checkId.startsWith('layer0.net.internet'))   return 'Sentinel Internet';
	if (checkId.startsWith('layer0.net.dns'))        return 'Sentinel DNS';
	if (checkId.startsWith('layer0.'))               return L0_TARGET_LABELS[target] ?? titleCase(tDash);
	if (checkId.startsWith('layer1.product.'))       return productLabel(target);
	if (checkId.startsWith('layer1.stream.'))        return STREAM_TARGET_LABELS[target] ?? `Stream · ${titleCase(tDash)}`;
	if (checkId.startsWith('layer1.vector.'))        return VECTOR_TARGET_LABELS[target] ?? `Overlay · ${titleCase(tDash)}`;
	if (checkId.startsWith('layer2.radar.'))         return target;            // XSCV / CBAND — keep radar IDs as-is
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
