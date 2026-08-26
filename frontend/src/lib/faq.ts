/**
 * FAQ content for the in-app page at /faq.
 *
 * Kept as data rather than markup so the page stays presentational and so
 * `validation_tests/test_faq_parity.py` can check that this question set
 * matches `docs/04-faq.md`. The two versions are deliberately allowed to word
 * their answers differently — the docs site suits longer prose, this one is
 * read on a dashboard — but they must not drift on WHICH questions exist, or
 * the lab team gets different answers depending on where they look.
 */

export type FaqBlock =
	| { kind: 'p'; text: string }
	| { kind: 'table'; head: string[]; rows: string[][] }
	| { kind: 'note'; text: string };

export interface FaqItem {
	id: string;
	q: string;
	blocks: FaqBlock[];
}

export const FAQ: FaqItem[] = [
	{
		id: 'what-we-watch',
		q: 'What is Sentinel actually watching?',
		blocks: [
			{ kind: 'p', text: 'The **public-facing radarca service**, not the radars directly. Sentinel has no privileged access to CSU-CHILL infrastructure and no feed from the radar processors — it reads exactly what a browser visiting radarca would read, and reasons about it.' },
			{ kind: 'note', text: 'That limit shapes every answer here. When Sentinel says "XSWR is not reporting", the precise claim is *its data is not reaching radarca\'s public API* — which could be the radar, the ingest pipeline, the publishing step, or the API itself.' }
		]
	},
	{
		id: 'data-sources',
		q: 'Where does the data come from?',
		blocks: [
			{ kind: 'p', text: 'Five public endpoints:' },
			{
				kind: 'table',
				head: ['Endpoint', 'What we take from it'],
				rows: [
					['/api/radar-status/', 'Each radar\'s **declared** status (UP/DOWN)'],
					['/api/xbandRadarImages/', 'Per-radar, per-moment list of published scan filenames'],
					['/api/productDetail', 'Per-product step lists and timestamps'],
					['/api/imageData', 'The product and scan PNGs themselves'],
					['the rendered site', 'Whether overlays actually draw, via a real browser']
				]
			},
			{ kind: 'p', text: 'Scan times come from the **filenames** (`..._20260826-0049.png`), which is how we can say "the newest scan is 6 minutes old" rather than merely "some images exist".' }
		]
	},
	{
		id: 'cadence',
		q: 'How often does it check, and does that load your servers?',
		blocks: [
			{ kind: 'p', text: 'Cadences run from 30 s (is the internet up) to 1 h (slow forecast products); most radar and product checks run every **120 s**. Total outbound traffic is roughly **30 requests/minute**.' },
			{ kind: 'p', text: 'We actively work to keep that low. All six radar checks used to fetch `/api/radar-status/` independently — 4,320 calls/day where 720 suffice — and now share one memoised fetch per cycle. If our traffic is ever a problem, tell us: the cadences are configuration, not architecture.' }
		]
	},
	{
		id: 'stages',
		q: 'What do the five stages mean?',
		blocks: [
			{
				kind: 'table',
				head: ['Stage', 'Name', 'Question it answers'],
				rows: [
					['L0', 'Connectivity', 'Is the internet up, DNS resolving, radarca reachable?'],
					['L1', 'Product Freshness', 'Is each product publishing recent, correctly-sized data?'],
					['L2', 'Radar Scans', 'Is each radar actually emitting scans?'],
					['L3', 'Map Overlays', 'Do overlays render in a real browser?'],
					['L4', 'Image Quality', 'Does the imagery itself look plausible?']
				]
			},
			{ kind: 'p', text: 'The stages are also a dependency chain. If L0 says the origin is unreachable, downstream failures are collateral and Sentinel marks them as such, rather than painting forty red cells for one root cause.' }
		]
	},
	{
		id: 'statuses',
		q: 'What is the difference between fail, error, warn and skip?',
		blocks: [
			{
				kind: 'table',
				head: ['Status', 'Means', 'Example'],
				rows: [
					['pass', 'Checked, healthy', 'Scans arriving on time'],
					['warn', 'Degraded, not broken', 'Declared DOWN but data still flowing'],
					['fail', '**The monitored thing is broken**', 'Radar declared UP, no scans'],
					['error', '**We could not determine its state**', 'Upstream API timed out'],
					['skip', 'Deliberately not evaluated', 'Suppressed by an upstream failure or a silence']
				]
			},
			{ kind: 'note', text: '`fail` is a statement about radarca. `error` is a statement about *our visibility*. Treat a screen full of `error` as "Sentinel is flying blind", not "everything is down". These were rendered in the same red until August 2026, which made ~1,500 upstream API timeouts a day read as radar outages.' }
		]
	},
	{
		id: 'radar-down',
		q: 'How do you decide a radar is down?',
		blocks: [
			{ kind: 'p', text: 'Sentinel reconciles two independent facts: what radarca **declares** the radar\'s status to be, and what it **observes** in the published scans.' },
			{
				kind: 'table',
				head: ['Declared', 'Scans arriving?', 'Verdict'],
				rows: [
					['UP', 'yes', 'HEALTHY'],
					['UP', 'no', '**GHOST_UP** — claims up, silent'],
					['DOWN', 'no', 'CONFIRMED_DOWN — known outage, correctly reported'],
					['DOWN', 'yes', 'STUCK_DOWN_FLAG — status flag stale, data is fine'],
					['—', 'can\'t tell', 'OBSERVED_API_ERROR']
				]
			},
			{ kind: 'p', text: '`GHOST_UP` is the one worth paging on: it is the failure mode the status feed itself will not tell you about. `CONFIRMED_DOWN` is not alarming in the same way — if a radar is down and radarca says it is down, the outage is real but nobody is misinformed.' }
		]
	},
	{
		id: 'thresholds',
		q: 'Why is one radar\'s threshold different from another\'s?',
		blocks: [
			{ kind: 'p', text: 'The check asks: *how old is the newest published scan?* That age contains two things — the radar\'s scan interval, **and upstream\'s publication lag** between a scan happening and appearing in the API. On this fleet the lag runs roughly 300–500 s.' },
			{ kind: 'p', text: 'Thresholds calibrated in May were derived from scan cadence alone (~120 s). XSWR scans every 120 s and delivers ~27 images per poll — a perfectly healthy radar — but its 240 s threshold sat below the publication lag, so it reported `GHOST_UP` on **96%** of checks. Recalibrated in August against measured age, with 6,406 historical verdicts corrected.' },
			{
				kind: 'table',
				head: ['XEBY', 'XSCV', 'XSCW', 'XSCR', 'XSWR', 'CBAND'],
				rows: [['300', '660', '720', '780', '660', '1080']]
			},
			{ kind: 'note', text: '**Raising a threshold cannot hide an outage.** A radar publishing *no* images is marked not-fresh regardless of any threshold. Thresholds only govern the "images present but not advancing" case — a frozen feed — where the cost is detection latency, not detection.' }
		]
	},
	{
		id: 'systemic',
		q: 'Five radars went red at once. Is that five outages?',
		blocks: [
			{ kind: 'p', text: 'Almost certainly one. Radars at Santa Cruz, Sonoma, East Bay, Santa Clara and Sierra do not fail in the same second.' },
			{ kind: 'p', text: 'Measured over 14 days, **80%** of all `GHOST_UP` readings happened while four or five radars were silent simultaneously; only **3%** were isolated to one radar. The clearest case: four sites entered `GHOST_UP` at `2026-08-13 12:29:52` and left it at `2026-08-16 19:29:54` — sub-second alignment, 79 hours apart.' },
			{ kind: 'p', text: 'Sentinel now runs a fleet-correlation check that recognises this shape and raises **one** systemic alarm instead of five, while still showing each radar\'s individual verdict. An isolated single-radar failure is unaffected and still pages normally.' },
			{ kind: 'note', text: 'That 79-hour episode was a **real** data outage. The correlation logic makes the count accurate, not the problem smaller.' }
		]
	},
	{
		id: 'history',
		q: 'Can a past verdict change? Is the history trustworthy?',
		blocks: [
			{ kind: 'p', text: 'Yes, verdicts can be re-derived — and the history is trustworthy *because* of how that works. When a threshold is corrected, Sentinel can re-evaluate historical runs under the new value, so the record reflects our best current understanding rather than a setting we have since learned was wrong. Three rules make that safe:' },
			{ kind: 'p', text: '**Raw observations are never modified.** The scan count and newest-scan timestamp are what we measured; only the interpretation is recomputed.' },
			{ kind: 'p', text: '**The original verdict is preserved** in the row, including the threshold that produced it. You can always see what we said at the time, and why.' },
			{ kind: 'p', text: '**Runs where no data existed are never reclassified.** A genuine stoppage cannot be tuned away.' },
			{ kind: 'note', text: 'Worked example — the August recalibration: 231,430 rows re-evaluated, 6,406 corrected, zero rows deleted, and the 79-hour outage still recorded in full across all five radars.' }
		]
	},
	{
		id: 'alerts',
		q: 'Who gets alerted, and how do I stop being paged for something I know about?',
		blocks: [
			{
				kind: 'table',
				head: ['Tier', 'Meaning'],
				rows: [
					['Action required', 'Broken (`fail`/`error`). Auto-escalates after 30 min.'],
					['Attention required', 'Degraded (`warn`). Visible, does not page.'],
					['Informational', 'Healthy or out of scope.']
				]
			},
			{ kind: 'p', text: 'Recipients, escalation steps and on-call schedules live under Admin → Alerts and Admin → Groups, including recurring quiet hours and per-device routing on mobile.' },
			{ kind: 'p', text: 'For planned work use a **silence** rather than muting a check permanently — silenced alarms still appear on the dashboard and in the record, they just stop paging.' },
			{ kind: 'note', text: 'If you are seeing noise you believe is wrong, that is worth reporting rather than silencing. Two of the largest sources of false alarms found so far were a stale threshold and a colour that made "couldn\'t measure" look identical to "broken".' }
		]
	}
];

/** Things Sentinel deliberately does not do — shown after the questions. */
export const FAQ_LIMITS: string[] = [
	'It does not assess **scientific quality** of radar products. L4 checks catch gross image anomalies — saturated, frozen, empty, ring artifacts — not calibration error or subtle bias.',
	'It does not see **inside** radarca. Every conclusion is inferred from public outputs.',
	'It cannot distinguish "the radar stopped" from "the pipeline stopped publishing it" for a single radar. Correlation across radars is what separates those, and it needs more than one radar to work.',
	'Its history is only as good as its thresholds, which is why they are re-tuned monthly and why past verdicts can be corrected.'
];
