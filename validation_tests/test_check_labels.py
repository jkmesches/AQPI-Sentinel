#!/usr/bin/env python3
"""Every registered check must get a label from a branch meant for IT.

Both label functions — `backend/check_labels.py:pretty_check_label` and its
mirror `frontend/src/lib/format.ts:prettyCheckLabel` — are ordered
prefix-match chains, first match wins. That shape fails silently in one
specific way: a new check family whose ids sit under an existing prefix gets
labelled by the OLDER, more generic branch, and the label is confidently wrong
rather than missing.

That is what happened to LB3 on 2026-10-05. `layer3.composite.<radar>` and
`layer3.backend.drops` both fell into the `layer3.` branch written for the
overlay-parity check and rendered as "Overlay reconcile — Cband". Nothing
errored; the row just described a check that does not exist. It was found by
a person reading the dashboard and asking what it meant.

=== Why a stage-shaped audit missed it ===

The known enumeration sites were audited by searching for stage-keyed code
(`ALL_STAGES`, `stage ==`, the label/colour maps). These two functions are not
stage-keyed — they match on CHECK ID PREFIX — so no amount of grepping for
stages would ever have found them. There is a whole second family of
enumeration sites with that shape, including `sparklineMetric`,
`readoutMetric`, `scheduler.py`'s `layer0.net.` special cases and `push.py`'s
L0 routing. Both greps are needed:

    grep -rnE "ALL_STAGES|stage ==|'L0'"              # stage-keyed
    grep -rnE "startsWith\\('layer|\\.startswith\\(\\"layer"   # id-prefix-keyed

Run:  python3 validation_tests/test_check_labels.py
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def registered(profile: str) -> dict[str, str]:
    """check_id -> target, from the real import graph on `profile`."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import json, backend.checks, backend.prewarm\n"
         "from backend.registry import CHECKS\n"
         "print('@@' + json.dumps({c.id: c.target for c in CHECKS.values()}))"],
        cwd=ROOT, capture_output=True, text=True,
        env=dict(os.environ, SENTINEL_PROFILE=profile,
                 SENTINEL_DB_URL="postgresql://unused/unused",
                 SENTINEL_BACKEND_ROOT="/backend/root",
                 SENTINEL_SSCB_ROOT="/sscb"))
    line = [l for l in out.stdout.splitlines() if l.startswith("@@")]
    assert line, out.stderr[-2000:]
    import json
    return json.loads(line[0][2:])


# --- extract each file's ordered branch list -------------------------------
#
# Parsed rather than executed: the point is the ORDER and SPECIFICITY of the
# branches, which is a property of the source text, and parsing keeps the TS
# function testable from Python without a node round-trip.

def ts_branches() -> list[tuple[str, bool]]:
    """(matcher, is_exact) in source order, for prettyCheckLabel only."""
    src = (ROOT / "frontend/src/lib/format.ts").read_text()
    body = src[src.index("export function prettyCheckLabel"):]
    body = body[:body.index("\n}")]
    out: list[tuple[str, bool]] = []
    for m in re.finditer(
            r"checkId\.startsWith\('([^']+)'\)|checkId === '([^']+)'"
            r"|checkId === (BACKEND_FLEET_CHECK_ID|FLEET_CHECK_ID)", body):
        if m.group(1):
            out.append((m.group(1), False))
        elif m.group(2):
            out.append((m.group(2), True))
        else:
            const = {"BACKEND_FLEET_CHECK_ID": "layer2.backend.fleet",
                     "FLEET_CHECK_ID": "layer2.xband.fleet"}[m.group(3)]
            out.append((const, True))
    return out


def py_branches() -> list[tuple[str, bool]]:
    src = (ROOT / "backend/check_labels.py").read_text()
    body = src[src.index("def pretty_check_label"):]
    out: list[tuple[str, bool]] = []
    for m in re.finditer(r'cid\.startswith\("([^"]+)"\)|cid == "([^"]+)"', body):
        if m.group(1):
            out.append((m.group(1), False))
        else:
            out.append((m.group(2), True))
    return out


def first_match(cid: str, branches: list[tuple[str, bool]]) -> tuple[str, bool] | None:
    for matcher, exact in branches:
        if (cid == matcher) if exact else cid.startswith(matcher):
            return matcher, exact
    return None


def main() -> int:
    ts, py = ts_branches(), py_branches()
    print(f"branch chains parsed:  format.ts {len(ts)}   check_labels.py {len(py)}")
    check("both label chains were found", len(ts) > 5 and len(py) > 5,
          f"{len(ts)} / {len(py)}")

    ids: dict[str, str] = {}
    for prof in ("aqpi", "xqpi"):
        ids.update(registered(prof))
    print(f"\n{len(ids)} distinct check ids across both profiles")

    # ---- THE STRUCTURAL RULE ------------------------------------------
    #
    # A check's FAMILY is the first two id segments: layer3.composite,
    # layer2.backend, layer0.self. The rule is not "every family needs its own
    # branch" -- some are deliberately served by a generic one with a lookup
    # table behind it, and `layer0.` is like that on purpose. The rule is that
    # relying on a generic branch must be a DECLARED choice, so that a new
    # family cannot start relying on one silently. That is the whole bug: LB3
    # arrived, nobody added a branch, and `layer3.` absorbed it without a
    # murmur.
    #
    # Adding a family here is the acknowledgement. If a new check family shows
    # up in this set without being listed, the test fails and the author has
    # to decide whether a generic label is really what they want.
    GENERIC_OK = {
        # Served by `layer0.` plus the L0_TARGET_LABELS / _L0_TARGETS table.
        "layer0.net", "layer0.self", "layer0.origin", "layer0.website",
        # `layer3.` was written FOR the overlay check, back when it was the
        # only layer3 family. It keeps the generic branch; LB3 does not.
        "layer3.overlay",
        # `layer4.` plus the product-label table. (`layer4.xband` has its own
        # branch; only the mosaic family rides the generic one.)
        "layer4.mosaic",
    }

    def family(cid: str) -> str:
        return ".".join(cid.split(".")[:2])

    print("\nno check relies on a generic branch without saying so:")
    for name, branches in (("format.ts", ts), ("check_labels.py", py)):
        undeclared: list[str] = []
        unmatched: list[str] = []
        for cid in sorted(ids):
            hit = first_match(cid, branches)
            if hit is None:
                unmatched.append(cid)
                continue
            matcher, exact = hit
            if exact or matcher.startswith(family(cid)):
                continue          # a branch written for this family
            if family(cid) in GENERIC_OK:
                continue          # declared as deliberately generic
            undeclared.append(f"{cid} -> {matcher!r}")
        check(f"{name}: no undeclared reliance on a generic branch",
              not undeclared, "; ".join(undeclared[:4]))
        check(f"{name}: no check falls through to the generic default",
              not unmatched, "; ".join(unmatched[:4]))

    # Families that no longer exist should not linger in GENERIC_OK either --
    # a stale allowlist entry is how an allowlist stops meaning anything.
    live_families = {family(c) for c in ids}
    stale = sorted(f for f in GENERIC_OK if f not in live_families)
    check("GENERIC_OK lists no family that no longer registers",
          not stale, "; ".join(stale))

    # ---- THE TWO FILES MUST NOT DRIFT ---------------------------------
    #
    # They are two implementations of one mapping, and step 4 of "adding a
    # check" says to update both. LB3 got a branch in NEITHER, but the
    # likelier future mistake is one file and not the other.
    #
    # Compared at FAMILY granularity, not branch identity: the two files
    # legitimately differ in how finely they slice layer0.origin (one matches
    # the exact ids, the other the prefix) while producing the same labels.
    # Asserting branch equality there would fail on a difference that does not
    # matter, and a test that cries wolf gets muted.
    print("\nboth files have a dedicated branch for the same families:")
    def dedicated(branches: list[tuple[str, bool]]) -> set[str]:
        out = set()
        for cid in ids:
            hit = first_match(cid, branches)
            if hit and (hit[1] or hit[0].startswith(family(cid))):
                out.add(family(cid))
        return out
    only_ts = sorted(dedicated(ts) - dedicated(py))
    only_py = sorted(dedicated(py) - dedicated(ts))
    check("no family has a dedicated branch in only one file",
          not only_ts and not only_py,
          f"ts-only={only_ts} py-only={only_py}")

    # ---- SEMANTIC: another family's vocabulary must not leak ----------
    # The structural rule above would not have caught the fleet check, whose
    # id legitimately sits under layer2.backend. but which is a correlation
    # verdict and was labelled "backend arrival".
    print("\nno label borrows another family's vocabulary:")
    from backend.check_labels import pretty_check_label as L
    FORBIDDEN = [
        ("reconcile", lambda c: not c.startswith("layer3.overlay."),
         "only the overlay-parity check reconciles anything"),
        ("arrival", lambda c: not (c.startswith("layer2.backend.")
                                   and c != "layer2.backend.fleet"),
         "arrival belongs to the per-radar LB2 checks, not to a correlation"),
        ("Overlay", lambda c: not c.startswith("layer3.overlay."),
         "LB3 is not an overlay check"),
    ]
    for word, allowed, why in FORBIDDEN:
        hits = sorted(f"{cid} -> {L(cid, ids[cid])!r}" for cid in ids
                      if word.lower() in (L(cid, ids[cid]) or "").lower()
                      and allowed(cid))
        check(f"no misuse of {word!r} — {why}", not hits, "; ".join(hits[:3]))

    # ---- the specific labels the incident was about -------------------
    print("\nthe labels a person actually read and could not interpret:")
    for cid, want in (("layer3.composite.XSWR", "XSWR — in composite"),
                      ("layer3.backend.drops", "DROPS producer"),
                      ("layer2.backend.fleet", "Radar fleet — correlation")):
        if cid in ids:
            got = L(cid, ids[cid])
            check(f"{cid} -> {want!r}", got == want, repr(got))
    # And the branch they were stealing from still works.
    ov = [c for c in ids if c.startswith("layer3.overlay.")]
    if ov:
        check("the overlay check still says 'Overlay reconcile'",
              "Overlay reconcile" in L(ov[0], ids[ov[0]]), L(ov[0], ids[ov[0]]))

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall check-label assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
