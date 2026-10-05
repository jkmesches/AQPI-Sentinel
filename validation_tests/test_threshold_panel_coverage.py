#!/usr/bin/env python3
"""Every threshold a check honours must be reachable in the admin panel.

A tunable can be in three states, and only the third is useful:

  1. a constant in config.py            — not tunable at all
  2. in the threshold blob, honoured by the check, accepted by the API, and
     INVISIBLE in /admin/thresholds   — tunable only by someone who knows to
                                        PATCH the API by hand
  3. in the blob and rendered

State 2 is the trap, because nothing fails. The check reads its threshold,
the API would accept an edit, the retroactive reprocess engine would honour
one — there is simply no way to make the edit through the UI, and no error
anywhere to say so.

It has now happened twice:

  v0.5.0  LB1/LB2 keys were editable through the API immediately but absent
          from the page, because the column set was written out by hand in
          four places. A comment above RADAR_KEYS records it.
  v0.7.0  composite_contrib_warn_s, composite_contrib_fail_s and
          drops_silent_info_s were added to the seed and honoured by the LB3
          checks, and the globals section rendered none of them — it had a
          FIFTH hand-written key list inline in the markup, one section below
          that comment.

The page now derives its global key list from the seed, so the drift is
structurally impossible rather than merely discouraged. This test guards the
derivation itself: it fails if someone reintroduces a literal list.

Run:  python3 validation_tests/test_threshold_panel_coverage.py
"""
from __future__ import annotations
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

from backend import thresholds as _t                    # noqa: E402

PANEL = ROOT / "frontend/src/routes/admin/thresholds/+page.svelte"

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def main() -> int:
    seed = _t.seed_defaults()
    panel = PANEL.read_text()

    print("the globals section derives its keys instead of listing them:")
    # The failure mode is a literal array in the {#each}. Catch the shape, not
    # one instance of it: any {#each ['a','b'] as k} in this file is a
    # hand-written tunable list.
    literals = re.findall(r"\{#each \[\s*'[^\]]*\]\s+as\s+\w+\}", panel)
    check("no hand-written key list drives an {#each} on this page",
          not literals, f"{len(literals)} found: {literals[:2]}")
    check("the globals grid iterates a derived list",
          "{#each globalKeys as k}" in panel
          and "const globalKeys = $derived(" in panel)
    check("...built from the SEED, so a new backend tunable appears here",
          "Object.keys(defaults?.globals ?? {})" in panel)
    check("...unioned with STORED keys, so nothing already set is stranded",
          "Object.keys(draft?.globals ?? {})" in panel)

    print("\nevery seeded threshold is reachable in its section:")
    # Globals: reachable by construction now, so assert the construction holds
    # for the keys that actually exist rather than re-deriving the render.
    g = sorted(seed.get("globals") or {})
    check("the seed has globals to render", bool(g), str(g))
    print(f"        globals: {', '.join(g)}")

    # Per-radar and per-product sections DO still use declared lists, which is
    # fine — but the declaration must cover every key the seed writes.
    for section, const in (("radars", "RADAR_KEYS"), ("products", "PRODUCT_KEYS")):
        m = re.search(const + r" = \[([^\]]+)\]", panel)
        check(f"{const} is declared", bool(m))
        if not m:
            continue
        declared = set(re.findall(r"'([^']+)'", m.group(1)))
        seeded: set[str] = set()
        for entry in (seed.get(section) or {}).values():
            seeded |= set(entry)
        missing = sorted(seeded - declared)
        check(f"{section}: every seeded key is in {const}",
              not missing, "; ".join(missing))

    print("\nthe thresholds the new checks read are all seeded:")
    # Read from the checks' own call sites rather than a list kept here, so
    # this cannot drift from the code either.
    srcs = {
        "layer3_backend_processing.py":
            (ROOT / "backend/checks/layer3_backend_processing.py").read_text(),
        "layer2_backend_radar.py":
            (ROOT / "backend/checks/layer2_backend_radar.py").read_text(),
    }
    wanted_global: set[str] = set()
    wanted_radar: set[str] = set()
    for src in srcs.values():
        wanted_global |= set(re.findall(r'get_global\(\s*"([^"]+)"', src))
        wanted_radar |= set(re.findall(r'get_radar\(\s*[^,]+,\s*"([^"]+)"', src))
    print(f"        get_global: {sorted(wanted_global)}")
    print(f"        get_radar:  {sorted(wanted_radar)}")
    check("every get_global key the new checks read is seeded",
          not (wanted_global - set(seed.get("globals") or {})),
          str(sorted(wanted_global - set(seed.get("globals") or {}))))
    radar_seeded: set[str] = set()
    for entry in (seed.get("radars") or {}).values():
        radar_seeded |= set(entry)
    check("every get_radar key the new checks read is seeded",
          not (wanted_radar - radar_seeded),
          str(sorted(wanted_radar - radar_seeded)))

    print("\nlive checks and the reprocess engine read the same source:")
    # The sharpest version of the trap. If a check reads a config constant
    # directly while its reverdict handler reads the STORE, then editing the
    # threshold changes retroactive verdicts and leaves live ones alone -- a
    # split brain between a check and its own reprocess handler, with nothing
    # reporting the disagreement. drops_silent_info_s was in exactly that
    # state from v0.7.0 until this test was written.
    reproc = (ROOT / "backend/reprocess_engine.py").read_text()
    lb3_handler = reproc[reproc.index("def _reverdict_lb3"):]
    lb3_handler = lb3_handler[:lb3_handler.index("\n_REVERDICT")]
    handler_globals = set(re.findall(r'get_global\(\s*"([^"]+)"', lb3_handler))
    live = srcs["layer3_backend_processing.py"]
    live_globals = set(re.findall(r'get_global\(\s*"([^"]+)"', live))
    only_handler = sorted(handler_globals - live_globals)
    check("every threshold the LB3 reverdict reads is also read live",
          not only_handler,
          f"read on reprocess but not live: {only_handler}")
    print(f"        live: {sorted(live_globals)}")
    print(f"        reprocess: {sorted(handler_globals)}")

    print("\nwhat is deliberately NOT tunable, stated so it is a choice:")
    # The fleet correlation constants are module-level and not in the blob at
    # all. That is intentional: ENTER/EXIT/DWELL were fitted to a count
    # distribution over 9,800 ticks and a change to them alters what
    # "systemic" MEANS, which is a code review rather than an operator knob.
    # Asserted so that if someone does route them through the store, they have
    # to come here and say why.
    lb2 = srcs["layer2_backend_radar.py"]
    for const in ("LB_FLEET_SYSTEMIC_ENTER", "LB_FLEET_SYSTEMIC_EXIT",
                  "LB_FLEET_MIN_DWELL_S"):
        check(f"{const} is a module constant, not a stored threshold",
              f"\n{const} = " in lb2 and f'"{const.lower()}"' not in lb2)

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall threshold-panel assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
