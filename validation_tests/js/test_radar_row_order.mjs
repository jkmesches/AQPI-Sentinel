// Pipeline order of the rows inside a radar's expander.
//
// The group is read top to bottom as the data moves: it ARRIVES on disk (LB2),
// is STAGED where the composite will look (layer3.input), the composite USES
// it (layer3.composite), and radarca reports on it last (L2). Diagnosing a gap
// means working down that chain, so the order is the point of the column, not
// decoration.
//
// Sorting by stage alone left the two LB3 families in API order, which is
// alphabetical — "composite" above "staged", reversing the only two rows whose
// relative order carries meaning, and reversing them precisely in the case the
// staging check was added to make visible: staged empty, composite missing.

export function runTests(mod) {
	const { radarRowRank } = mod;
	const fails = [];
	const check = (label, cond, detail = '') => {
		console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
		if (!cond) fails.push(label);
	};

	const row = (stage, check_id) => ({ stage, check_id });
	const order = (rows) =>
		rows.slice().sort((a, b) => radarRowRank(a) - radarRowRank(b)).map((r) => r.check_id);

	const all = [
		row('L2', 'layer2.radar.XEBY'),
		row('LB3', 'layer3.composite.XEBY'),
		row('LB2', 'layer2.backend.XEBY'),
		row('LB3', 'layer3.input.XEBY')
	];

	console.log('\nthe expander reads down the pipeline');
	check(
		'arrival, staged, used, then what radarca says',
		JSON.stringify(order(all)) ===
			JSON.stringify([
				'layer2.backend.XEBY',
				'layer3.input.XEBY',
				'layer3.composite.XEBY',
				'layer2.radar.XEBY'
			]),
		order(all).join(' → ')
	);
	check(
		'staged sorts above composite, not alphabetically',
		radarRowRank(row('LB3', 'layer3.input.XEBY')) <
			radarRowRank(row('LB3', 'layer3.composite.XEBY'))
	);
	check(
		'both LB3 rows still sit between LB2 and L2',
		radarRowRank(row('LB2', 'layer2.backend.XEBY')) <
			radarRowRank(row('LB3', 'layer3.input.XEBY')) &&
			radarRowRank(row('LB3', 'layer3.composite.XEBY')) <
				radarRowRank(row('L2', 'layer2.radar.XEBY'))
	);

	console.log('\nan unplaced row is visible, not promoted');
	// A stage nobody ranked must not land at the top, where it would displace
	// the arrival reading — the row an operator looks at first.
	check(
		'an unknown stage sorts last',
		radarRowRank(row('LB9', 'layer9.new.XEBY')) >
			radarRowRank(row('L2', 'layer2.radar.XEBY')),
		String(radarRowRank(row('LB9', 'layer9.new.XEBY')))
	);
	check(
		'an unknown LB3 family sorts after the two that are placed',
		radarRowRank(row('LB3', 'layer3.whatever.XEBY')) >
			radarRowRank(row('LB3', 'layer3.composite.XEBY'))
	);
	check(
		'a partial order never reports two rows as equal',
		new Set(all.map(radarRowRank)).size === all.length,
		all.map(radarRowRank).join(',')
	);

	console.log(
		fails.length ? `\n${fails.length} FAILED: ${fails.join(', ')}` : '\nall radar-row-order assertions passed'
	);
	return fails.length ? 1 : 0;
}
