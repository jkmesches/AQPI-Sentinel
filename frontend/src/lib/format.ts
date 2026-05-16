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
