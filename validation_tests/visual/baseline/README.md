# Map console baseline

<!-- prose-check: mode=tech-informal -->

The map console's structure as it stood before slice 3 of
`docs/map-console-plan.md` moved 3,277 lines of `MapView.svelte` into
modules. Twenty-eight states, captured from `the test deployment` on
2026-11-20 against commit `b63c7e5`.

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

Two consecutive captures of unchanged code agreed on all 28 states, which is
this baseline's noise floor. Anything a refactor moves shows against it.

Replace these files when the console's structure changes on purpose, in the
same commit that changes it, so the reason sits in the commit message
alongside the new numbers.
