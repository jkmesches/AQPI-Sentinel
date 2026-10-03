// Sparkline geometry, pulled out of Sparkline.svelte so it can be tested.
//
// It used to live inside the component's $derived block, which meant the one
// piece of arithmetic that decides whether an operator can tell a healthy feed
// from a dead one had no test at all. It had been wrong for a while:
// autoscaling every trace to its own observed min/max put a perfectly steady
// series and a series with no samples at the SAME baseline, pixel for pixel,
// and gave a 2% wobble the same full-height zigzag as a 6x swing.
//
// See backend.checks.helpers.headroom for the metric that made a fixed domain
// possible in the first place.

export interface Sample {
	ts: number;
	value: number;
}

export interface GeometryOpts {
	data: Sample[];
	/** Right edge of the visible window, ms since epoch. */
	now: number;
	windowMs: number;
	cadenceS?: number | null;
	width: number;
	height: number;
	/** Fixed [min, max] y range. Null autoscales to the window's own extremes. */
	domain?: [number, number] | null;
	/** Draw a rule at this value, in the metric's own units. Fixed domain only. */
	warnAt?: number | null;
}

export interface Geometry {
	strokeD: string;
	fillD: string;
	/** Sample count in the window — the component's trailing "N/h" label. */
	total: number;
	/** y of the warn rule, already rounded, or null when there is none. */
	warnY: string | null;
	/** Per-bucket y values. Exposed for tests; the component draws the paths. */
	ys: number[];
}

export function sparklineGeometry(opts: GeometryOpts): Geometry {
	const { data, now, windowMs, cadenceS, width, height } = opts;
	const domain = opts.domain ?? null;
	const warnAt = opts.warnAt ?? null;

	// One bucket per cadence interval — when healthy, that's roughly one
	// sample per bucket. Floor at 15 s so a wonky 0-cadence input doesn't make
	// the bucket count blow up.
	const baseBucket = Math.max(15_000, (cadenceS ?? 60) * 1000);
	const nBuckets = Math.max(8, Math.min(48, Math.floor(windowMs / baseBucket)));
	const bucketMs = windowMs / nBuckets;

	// Sums + counts so we can derive per-bucket means. Buckets that received
	// no samples stay at count=0 and render at baseline.
	const sums = new Array(nBuckets).fill(0);
	const counts = new Array(nBuckets).fill(0);
	const left = now - windowMs;
	for (const d of data) {
		if (d.ts < left || d.ts > now) continue;
		const i = Math.min(nBuckets - 1, Math.floor((d.ts - left) / bucketMs));
		if (i >= 0) {
			sums[i] += d.value;
			counts[i]++;
		}
	}
	const total = counts.reduce((a: number, b: number) => a + b, 0);
	const means: (number | null)[] = sums.map((s, i) => (counts[i] > 0 ? s / counts[i] : null));
	const nonEmpty = means.filter((v): v is number => v !== null);

	// A fixed domain means "this metric already knows its own scale" —
	// headroom is 0..1 by construction, so 0 is always the floor and 1 always
	// the ceiling no matter what this particular window contains. That is what
	// makes a flat healthy trace sit HIGH with a full fill instead of
	// collapsing onto the baseline where a dead one sits.
	//
	// Without one, fall back to local autoscale: each sparkline's variation
	// gets the full y range, so a radar producing 2-vs-12 scans/min looks
	// different from one producing 50-vs-60. Floors prevent div-by-zero when
	// the metric is constant or the window is entirely empty.
	const minV = domain ? domain[0] : nonEmpty.length > 0 ? Math.min(...nonEmpty) : 0;
	const maxV = domain ? domain[1] : nonEmpty.length > 0 ? Math.max(...nonEmpty) : 1;
	const span = Math.max(1e-9, maxV - minV);

	// Clamp only in fixed mode. Autoscale cannot produce an out-of-range value
	// by construction, and clamping there would be a silent behaviour change.
	const yOf = (v: number) => {
		const t = domain ? Math.max(0, Math.min(1, (v - minV) / span)) : (v - minV) / span;
		return height - t * (height - 2) - 1;
	};

	// The warn rule is only meaningful against a fixed axis — on an autoscaled
	// trace its position would wander with the data, which is worse than absent.
	const warnY = domain && warnAt !== null ? yOf(warnAt).toFixed(1) : null;

	const dx = nBuckets > 1 ? width / (nBuckets - 1) : 0;
	// Empty bucket → baseline. An empty bucket and a zero-headroom bucket land
	// in the same place on purpose: "nothing arrived" and "no budget left" are
	// the same news.
	const ys = means.map((v) => (v === null ? height - 1 : yOf(v)));
	const pts: [number, number][] = ys.map((y, i) => [i * dx, y]);

	const strokeD = pts
		.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`)
		.join(' ');
	// Area fill spans the full width with baseline at height — so the "trace at
	// zero" state still draws as a thin sliver, visibly distinct from an
	// empty/unrendered sparkline.
	const fillD = pts.length > 1 ? `${strokeD} L${width.toFixed(1)},${height} L0,${height} Z` : '';

	return { strokeD, fillD, total, warnY, ys };
}
