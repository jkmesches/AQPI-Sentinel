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
			error: 'text-[var(--color-fail)]',
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
			error: 'bg-[var(--color-fail)]',
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
			error: 'border-[var(--color-fail)]/45',
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
			error: 'text-[var(--color-fail)]',
			skip: 'text-[var(--color-muted)]',
			critical: 'text-[var(--color-critical)]',
			info: 'text-[var(--color-info)]'
		}[s] ?? 'text-[var(--color-muted)]'
	);
}

// Stage colour + display label. Keeps the timeline header readable for someone
// who's never seen our L0/L1/L2 layering before.
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
			L0:        'Site',
			L1:        'Products',
			L2:        'Radars',
			L3:        'Cross-check',
			'L4-T1T2': 'Image QC'
		}[s] ?? s
	);
}

// Turn a check_id + target into something a non-engineer can read at a glance.
// We special-case the major check families; everything else falls back to a
// cleaned-up version of the target.
export function prettyCheckLabel(checkId: string, target: string): string {
	const t = target.replaceAll('_', ' ');
	if (checkId.startsWith('layer0.tls.'))           return 'TLS cert';
	if (checkId.startsWith('layer0.origin.'))        return 'Origin live';
	if (checkId.startsWith('layer0.website.public')) return 'Public page';
	if (checkId.startsWith('layer0.website.root'))   return 'Root 404';
	if (checkId.startsWith('layer0.'))               return titleCase(t);
	if (checkId.startsWith('layer1.product.'))       return titleCase(t);
	if (checkId.startsWith('layer1.stream.'))        return 'Stream API';
	if (checkId.startsWith('layer1.vector.'))        return `Overlay · ${titleCase(t)}`;
	if (checkId.startsWith('layer2.radar.'))         return target;            // XSCV / CBAND
	if (checkId.startsWith('layer3.'))               return `Reconcile · ${titleCase(t)}`;
	if (checkId.startsWith('layer4.xband.'))         return target;
	if (checkId.startsWith('layer4.'))               return titleCase(t);
	return titleCase(t);
}
function titleCase(s: string): string {
	return s.replace(/\b\w/g, (c) => c.toUpperCase());
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
