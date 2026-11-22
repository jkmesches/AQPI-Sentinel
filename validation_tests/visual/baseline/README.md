# Map console baseline

<!-- prose-check: mode=tech-informal -->

The map console's structure, as 28 states captured from
`the test deployment` on 2026-11-20. Slice 3 of
The map console plan is moving `MapView.svelte` into modules, and this
is what the moves have to preserve.

It has been replaced once since it was first taken. The vector overlays were
all added with `beforeId: 'radar-halo'`, so their order among themselves came
down to which fetch returned first, and the first baseline recorded one of
the two arrangements. `$lib/map/layers/stack.ts` declares the order now, and
the recorded layer order changed on purpose.

Compare the current code against it:

```bash
python validation_tests/visual/capture.py /tmp/after
python validation_tests/visual/compare.py validation_tests/visual/baseline /tmp/after
```

Capture from the same deployment. The records name AQPI's radars and
products, so comparing them against the XQPI deployment reports two profiles differing
rather than two versions of the code.

The JSON is committed, and the screenshots are not. A record is about 9 KB,
and the whole set is 244 KB. The PNGs are 15 MB and nothing compares them.

Two consecutive captures of unchanged code agreed on all 28 states, with no
order-only warnings and no retries, which is this baseline's noise floor.
Anything a refactor moves shows against it.

A state that fails to load what it asked for is retried once and the error
is kept on its record. One state in 56 loads has done that, the forecast
composite failing to appear inside 30 s, and a record carrying `retried`
means the console did not show something a reader would have expected.

Replace these files when the console's structure changes on purpose, in the
same commit that changes it, so the reason sits in the commit message
alongside the new numbers.
