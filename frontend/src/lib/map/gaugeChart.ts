/**
 * The stream-gauge hydrograph, as an SVG string.
 *
 * Pure, so a runner can reach it. The chart is built as markup rather than
 * with a charting library because it lives inside a MapLibre popup, which
 * MapLibre owns and may tear down at any moment; a component mounted in
 * there would have to survive that.
 *
 * Geometry only. The colors are CSS in `app.css`, where both themes are
 * defined against their own surface, so this module never names one.
 */
import type { GaugeFetch, GaugePoint } from '$lib/gaugeSeries';

/** Fits a 320px popup with its padding. */
export const GC_W = 288;
export const GC_H = 92;
export const GC_PAD_L = 34;
export const GC_PAD_R = 4;
export const GC_PAD_T = 6;
export const GC_PAD_B = 14;

/** Below this, the two series are treated as meeting. */
const GAP_HOURS = 1.5;
const HOUR_MS = 3_600_000;

/** Headroom above the tallest point, so the peak is not on the frame. */
const TOP_HEADROOM = 1.08;

/**
 * Escape text bound for an SVG attribute or text node.
 *
 * @param s Raw text.
 * @returns The text with the five markup characters replaced.
 */
export function svgEsc(s: string): string {
	return s.replace(/[&<>"]/g, (c) =>
		({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] as string);
}

/**
 * Render a value with enough significant figures to be readable.
 *
 * Flow spans several orders of magnitude between a dry creek and a storm, so
 * a fixed precision either loses the small values or pads the large ones.
 *
 * @param v The value.
 * @returns A short decimal, or exponential below 0.01.
 */
export function gaugeNum(v: number): string {
	if (v === 0) return '0';
	const a = Math.abs(v);
	if (a >= 100) return v.toFixed(0);
	if (a >= 1) return v.toFixed(2);
	if (a >= 0.01) return v.toFixed(3);
	return v.toExponential(1);
}

/**
 * A UTC day-and-time label.
 *
 * The day of the month as well as the hour, since the window spans two to
 * three days and a bare hour would not say which one.
 *
 * @param t Milliseconds since the epoch.
 * @returns A label like "20 1430Z".
 */
export function hhmm(t: number): string {
	const d = new Date(t);
	return `${String(d.getUTCDate()).padStart(2, '0')} `
		+ `${String(d.getUTCHours()).padStart(2, '0')}`
		+ `${String(d.getUTCMinutes()).padStart(2, '0')}Z`;
}

/**
 * The upstream's own columns, as a compact key and value list.
 *
 * @param j One fetch result.
 * @returns Markup for the popup body, or a muted "no data".
 */
export function gaugeSummaryHtml(j: GaugeFetch | null): string {
	if (!j?.headers || j.headers.length <= 1) {
		return '<span class="gauge-muted">no data</span>';
	}
	// The first column echoes the COMID, so it is skipped. Numbers go to 2dp
	// for readability.
	const pairs: string[] = [];
	const hdrs = j.headers.slice(1);
	const vals = (j.values ?? []).slice(1);
	for (let i = 0; i < hdrs.length && i < vals.length; i++) {
		const v = vals[i];
		const rendered = typeof v === 'number' ? v.toFixed(2) : String(v);
		pairs.push(`<span class="gauge-muted">${hdrs[i]}:</span> ${rendered}`);
	}
	return pairs.join('  ·  ');
}

/**
 * Say how long the observed trace has been missing, when it has.
 *
 * The two series normally overlap by an hour or two, because observed runs
 * to the current hour and the forecast starts a little before it. A hole
 * therefore means the upstream is behind on observations for this site
 * rather than that the chart is drawn wrong, and a reader watching a rising
 * creek needs to be told which.
 *
 * @param obs Observed points, oldest first.
 * @param fcst Forecast points, oldest first.
 * @returns A muted note, or an empty string when the series meet.
 */
export function gapNote(obs: GaugePoint[], fcst: GaugePoint[]): string {
	if (obs.length === 0 || fcst.length === 0) return '';
	const gapH = (fcst[0].t - obs[obs.length - 1].t) / HOUR_MS;
	if (gapH < GAP_HOURS) return '';
	return '<div class="gauge-muted" style="margin-top:3px">'
		+ `no observations for ${Math.round(gapH)} h before the forecast`
		+ ' starts</div>';
}

/**
 * Draw observed and forecast flow as one hydrograph.
 *
 * One y axis, because both series are the same measure, and it starts at
 * zero because flow has a true zero. Forecast is dashed as well as
 * differently colored, so the two are told apart without relying on hue. A
 * dotted rule marks now, which is the only thing separating the measured
 * part of the line from the predicted part.
 *
 * No unit is printed. The upstream publishes bare numbers and documents no
 * unit for them, and a guessed unit on a chart about flooding is worse than
 * none.
 *
 * @param obs Observed points, oldest first.
 * @param fcst Forecast points, oldest first.
 * @param now Milliseconds since the epoch, for the rule. Injectable.
 * @returns SVG markup, or an empty string when there is nothing to draw.
 */
export function gaugeChartSvg(
	obs: GaugePoint[],
	fcst: GaugePoint[],
	now: number = Date.now()
): string {
	const all = [...obs, ...fcst];
	if (all.length < 2) return '';
	const t0 = Math.min(...all.map((p) => p.t));
	const t1 = Math.max(...all.map((p) => p.t));
	const vmax = Math.max(...all.map((p) => p.v));
	if (!(t1 > t0)) return '';
	// A flat-zero series still deserves a readable axis rather than a
	// divide-by-zero, so give it a nominal top.
	const top = vmax > 0 ? vmax * TOP_HEADROOM : 1;
	const x = (t: number) =>
		GC_PAD_L + ((t - t0) / (t1 - t0)) * (GC_W - GC_PAD_L - GC_PAD_R);
	const y = (v: number) =>
		GC_H - GC_PAD_B - (v / top) * (GC_H - GC_PAD_T - GC_PAD_B);
	const path = (pts: GaugePoint[]) =>
		pts.map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)} ${y(p.v).toFixed(1)}`)
			.join(' ');

	const nowRule = now > t0 && now < t1
		? `<line class="gc-now" x1="${x(now).toFixed(1)}" y1="${GC_PAD_T}" `
			+ `x2="${x(now).toFixed(1)}" y2="${GC_H - GC_PAD_B}"></line>`
		: '';

	// Mark a hole between the two series rather than leaving blank plot
	// area, which reads as a broken chart. Normally they overlap and nothing
	// is drawn here. See gapNote.
	const gapStart = obs.length ? obs[obs.length - 1].t : 0;
	const gapEnd = fcst.length ? fcst[0].t : 0;
	const gapBand = gapStart && gapEnd && gapEnd - gapStart > GAP_HOURS * HOUR_MS
		? `<rect class="gc-gap" x="${x(gapStart).toFixed(1)}" y="${GC_PAD_T}"`
			+ ` width="${(x(gapEnd) - x(gapStart)).toFixed(1)}"`
			+ ` height="${GC_H - GC_PAD_T - GC_PAD_B}"></rect>`
			+ `<text class="gc-gaplabel" x="${((x(gapStart) + x(gapEnd)) / 2).toFixed(1)}"`
			+ ` y="${(GC_H - GC_PAD_B) / 2 + 3}" text-anchor="middle">no data</text>`
		: '';

	// Native tooltips over invisible targets: a hover layer that needs no
	// script inside a popup MapLibre owns and may tear down at any moment.
	const hit = (p: GaugePoint, kind: string) =>
		`<rect class="gc-hit" x="${(x(p.t) - 4).toFixed(1)}" y="${GC_PAD_T}"`
		+ ` width="8" height="${GC_H - GC_PAD_T - GC_PAD_B}">`
		+ `<title>${svgEsc(hhmm(p.t))} ${kind} ${svgEsc(gaugeNum(p.v))}</title></rect>`;
	const hits = [
		...obs.map((p) => hit(p, 'observed')),
		...fcst.map((p) => hit(p, 'forecast'))
	].join('');

	return `
			<svg class="gauge-chart" viewBox="0 0 ${GC_W} ${GC_H}" role="img"
			     aria-label="observed and forecast flow, ${svgEsc(hhmm(t0))} to ${svgEsc(hhmm(t1))}">
				<line class="gc-axis" x1="${GC_PAD_L}" y1="${GC_H - GC_PAD_B}"
				      x2="${GC_W - GC_PAD_R}" y2="${GC_H - GC_PAD_B}"></line>
				<text class="gc-tick" x="${GC_PAD_L - 4}" y="${GC_PAD_T + 7}"
				      text-anchor="end">${svgEsc(gaugeNum(top))}</text>
				<text class="gc-tick" x="${GC_PAD_L - 4}" y="${GC_H - GC_PAD_B}"
				      text-anchor="end">0</text>
				<text class="gc-tick" x="${GC_PAD_L}" y="${GC_H - 3}">${svgEsc(hhmm(t0))}</text>
				<text class="gc-tick" x="${GC_W - GC_PAD_R}" y="${GC_H - 3}"
				      text-anchor="end">${svgEsc(hhmm(t1))}</text>
				${gapBand}
				${nowRule}
				${obs.length > 1 ? `<path class="gc-obs" d="${path(obs)}"></path>` : ''}
				${fcst.length > 1 ? `<path class="gc-fcst" d="${path(fcst)}"></path>` : ''}
				${hits}
			</svg>
			<div class="gauge-legend">
				<span><i class="gl-obs"></i>observed</span>
				<span><i class="gl-fcst"></i>forecast</span>
			</div>
			${gapNote(obs, fcst)}`;
}
