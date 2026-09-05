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
					['skip', 'Deliberately not evaluated', 'Suppressed by an upstream failure, or nothing to assess']
				]
			},
			{ kind: 'note', text: '`fail` is a statement about radarca. `error` is a statement about *our visibility*. Treat a screen full of `error` as "Sentinel is flying blind", not "everything is down". These were rendered in the same red until August 2026, which made ~1,500 upstream API timeouts a day read as radar outages.' },
			{ kind: 'p', text: '`skip` comes in two flavours that look identical on the timeline. Most are **cascade skips**: when one upstream thing breaks, Sentinel stops reporting its own opinion about everything downstream, so a single origin fault does not paint thirty cells red. That is a refusal to guess, *not* a clean bill of health — a skip never resolves an open alarm, and the previous verdict stands until a real result replaces it. The other flavour is an **intrinsic skip**, where the check ran and genuinely had nothing to assess; that one does count as "nothing wrong". The drilldown tells you which.' }
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
			{ kind: 'p', text: '**To stop being paged for something you already know about, acknowledge it.** An ack stops every channel for that alarm — email, console, webhook, and any repeat — and it silences it for the whole team, not just you, because whoever acks is taking ownership of it. Un-acking resumes paging.' },
			{ kind: 'p', text: 'For planned work use a **silence** rather than muting a check permanently — silenced alarms still appear on the dashboard and in the record, they just stop paging. Prefer a silence over an ack when you know the window in advance, because a silence covers the check regardless of how the underlying alarm comes and goes.' },
			{ kind: 'note', text: 'If you are seeing noise you believe is wrong, that is worth reporting rather than silencing. The three largest sources of false alarms found so far were a stale threshold, a colour that made "couldn\'t measure" look identical to "broken", and — until v0.2.1 — a bug that let a permanently-down radar re-announce itself every few hours because an upstream blip had been mistaken for a recovery.' }
		]
	},
	{
		id: 'image-pipeline',
		q: 'What happens to an image between radarca publishing it and a verdict appearing?',
		blocks: [
			{ kind: 'p', text: 'Seven steps, every cycle, per radar and per mosaic product:' },
			{
				kind: 'table',
				head: ['Step', 'What happens'],
				rows: [
					['1. Resolve', 'Ask radarca which scan is *latest* — `xbandRadarImages` for a radar, `productDetail` for a mosaic product.'],
					['2. Fetch', 'Pull the PNG bytes. This is the only upstream request the image checks make.'],
					['3. Archive', 'Hash the bytes with SHA-256 and store them content-addressed. Identical bytes are stored once.'],
					['4. Tier 1', 'Measure the frame — coverage, intensity, structure, perceptual hash. No judgement yet.'],
					['5. Tier 2', 'Run the pathology detectors against those pixels.'],
					['6. Compare', 'Check the perceptual hash against the previous run to catch a stuck feed.'],
					['7. Verdict', 'Take the worst sub-verdict. Anything not on the OK list becomes a `warn`.']
				]
			},
			{ kind: 'p', text: 'Separating step 4 from step 5 is deliberate. Tier 1 records **what the frame is**, and is threshold-free; Tier 2 decides **whether that is a problem**, and every threshold lives there. When a threshold turns out to be wrong we can re-run the judgement over stored measurements without re-fetching a single image from radarca.' },
			{ kind: 'note', text: 'Image checks emit `warn`, never `fail`. A strange-looking frame is a reason to go and look, not a claim that the product is broken — Sentinel cannot tell an artifact from genuinely unusual weather.' }
		]
	},
	{
		id: 'image-tests',
		q: 'What tests do you actually run on the image itself?',
		blocks: [
			{ kind: 'p', text: '**Tier 1 measures.** A pixel counts as *active* where alpha > 10, and these are recorded for every frame whether or not anything is wrong:' },
			{
				kind: 'table',
				head: ['Measurement', 'What it captures'],
				rows: [
					['Coverage %', 'Share of the canvas carrying data. Drives most of the suppression logic below.'],
					['Mean / std intensity', 'Brightest channel per active pixel — overall level and spread.'],
					['Top-3 intensity bins', 'Where the intensity histogram piles up, in 16 buckets.'],
					['Horizontal autocorrelation', 'Whether neighbouring pixels agree. Structured weather correlates; noise does not.'],
					['Perceptual hash', '16×16 pHash. Two frames with the same hash are visually identical.'],
					['SHA-256 + byte size', 'Exact identity of the file, for the archive and for dedup.']
				]
			},
			{ kind: 'p', text: '**Tier 2 judges.** Four detectors, each with a tunable threshold:' },
			{
				kind: 'table',
				head: ['Detector', 'Trips when', 'Catches'],
				rows: [
					['Saturation', 'One quantised colour holds >40% of active pixels', 'A frame collapsed to a single value — a stuck colour map or an encoder fault.'],
					['Speckle', '>35% of active pixels have no active 4-neighbour', 'Noise dressed as data: isolated pixels with no structure.'],
					['Range ring', 'Peak ring deviation >0.80× the median across 50 polar bins', 'Concentric artifacts on an X-band disc — a calibration or clutter-filter signature. X-band only; mosaics have no radar-centred geometry.'],
					['Frozen frame', 'pHash identical to the previous run', 'A feed that is publishing but no longer changing.']
				]
			},
			{ kind: 'p', text: 'Product checks also test the image at Layer 1, separately and more cheaply: that it exists and is really a PNG, that it is not implausibly small for that product, and that its bytes hash to a recorded value.' },
			{ kind: 'note', text: 'The saturation threshold is per product, not global. Forecast fields such as water depth encode a scalar with a thresholded colour ramp and legitimately sit above 40% in normal operation; radar reflectivity does not.' }
		]
	},
	{
		id: 'image-quiet-verdicts',
		q: 'Why do image checks so often say "quiet" or "low coverage" instead of pass or fail?',
		blocks: [
			{ kind: 'p', text: 'Because the honest answer is frequently *"that detector cannot say anything useful about this frame"*, and saying so is better than guessing. Each of these means the test ran and declined to draw a conclusion:' },
			{
				kind: 'table',
				head: ['Verdict', 'Means'],
				rows: [
					['`OK_SAME_FRAME`', 'Upstream has not published anything new since the last check, so there is nothing to compare against.'],
					['`QUIET_LOW_COV`', 'Coverage is below 5%. A radar watching a clear sky produces near-identical frames; that is calm weather, not a stuck feed.'],
					['`QUIET_SLOW`', 'This product updates more slowly than we check it, so repeats are expected.'],
					['`OK_LOW_COV`', 'Too few pixels for a saturation reading to mean anything.'],
					['`TOO_SPARSE` / `EMPTY`', 'Not enough active pixels for the detector to run at all.']
				]
			},
			{ kind: 'p', text: 'These exist because the first version did not have them, and it was badly wrong. The frozen-frame detector compared each run against the previous one without asking whether upstream had published anything in between — so re-sampling one published image reported it as frozen against itself, measuring our own polling rate rather than the feed. **15,326** captures are marked `OK_SAME_FRAME` (upstream had published nothing new); every one would previously have been a candidate for a false FROZEN. Reprocessing with the source comparison took the count from **13,837 to 263** in August 2026, and it stands at 265 today.' },
			{ kind: 'note', text: 'A suppressed verdict is still recorded in full. Nothing is deleted — you can always see which detector declined and why.' }
		]
	},
	{
		id: 'image-archive',
		q: 'Do you keep the images? Can I see what Sentinel actually saw?',
		blocks: [
			{ kind: 'p', text: 'Yes. Every frame a Layer 4 check evaluates is stored, currently about **298,000 captures / 22 GB**. Clicking a cell in the timeline shows the frame that produced that verdict, not a fresh fetch of whatever is current now.' },
			{ kind: 'p', text: 'Storage is **content-addressed**: a file is named by the SHA-256 of its own bytes. That has three consequences worth knowing:' },
			{
				kind: 'table',
				head: ['Property', 'Why it matters'],
				rows: [
					['Self-verifying', 'Re-hash a file and compare to its name. Silent corruption cannot hide.'],
					['Automatic dedup', 'A forecast product idle for an hour, or a radar on a steady clutter pattern, stores one copy however many times we fetch it.'],
					['Stable reference', 'A verdict points at exact bytes, so evidence for a past call cannot drift.']
				]
			},
			{ kind: 'p', text: 'Two indexes sit over it: one from hash to metadata (size, dimensions, first and last seen), and one from the upstream source string to the hash, which is what lets the map replay history from our own copies instead of asking radarca again.' },
			{ kind: 'note', text: 'This is also why map scrubbing is polite. Frames already captured are served from the archive, so moving through a time window costs radarca nothing.' }
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
