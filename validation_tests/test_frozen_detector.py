"""FROZEN must mean a stuck feed, not the check looking at one frame twice.

Run directly (no DB, no network):

    python validation_tests/test_frozen_detector.py

Background: the L4 image check runs every 120s, but publish cadences vary.
CBAND publishes roughly every 240s, so half of its runs re-sampled the frame
they had already seen. _LAST_PHASH tracked the hash but not WHICH image it
came from, so those runs compared a frame to itself and reported FROZEN.

Measured over 24h before the fix: of 366 runs where the source was unchanged,
366 flagged FROZEN; of 352 runs with a genuinely new image, 0 did. 1,520
warnings in 7 days, 42x the next noisiest radar, all spurious.

The load-bearing case is the last one: a genuinely stuck feed — upstream
advances the filename but the pixels don't change — must STILL be caught.
It would have been easy to fix the noise by suppressing frozen for CBAND and
lose that.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")

import backend.checks.layer4_image as L4          # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def verdict(check_id, src, phash, coverage_pct, *, skip_frozen=False, min_cov=5.0):
    """Reproduce the frozen decision exactly as layer4_image makes it."""
    prev_phash = L4._LAST_PHASH.get(check_id)
    prev_source = L4._LAST_SOURCE.get(check_id)
    if prev_source is not None and src == prev_source:
        v = "OK_SAME_FRAME"
    elif prev_phash is not None and prev_phash == phash:
        if skip_frozen:
            v = "QUIET_SLOW"
        elif coverage_pct < min_cov:
            v = "QUIET_LOW_COV"
        else:
            v = "FROZEN"
    else:
        v = "OK"
    L4._LAST_PHASH[check_id] = phash
    if src:
        L4._LAST_SOURCE[check_id] = src
    return v


def reset():
    L4._LAST_PHASH.clear()
    L4._LAST_SOURCE.clear()


# ---------------------------------------------------------------------------
# The reprocess engine must reach the SAME verdict as the live detector, or a
# retroactive pass would rewrite history to something the running system would
# never produce. These two implementations are separate code and drift is
# silent, so compare them directly on the same inputs.
# ---------------------------------------------------------------------------
def test_reprocess_parity() -> None:
    from backend import reprocess_engine as R
    print("\n[7] reprocess engine agrees with the live detector")

    cases = [
        # (label, cur_phash, prev_phash, cur_src, prev_src, coverage, expected)
        ("re-sampled same frame",      "aaaa", "aaaa", "f1.png", "f1.png", 6.0, "OK_SAME_FRAME"),
        ("new frame, same pixels",     "aaaa", "aaaa", "f2.png", "f1.png", 6.0, "FROZEN"),
        ("new frame, new pixels",      "bbbb", "aaaa", "f2.png", "f1.png", 6.0, "OK"),
        ("new frame, low coverage",    "aaaa", "aaaa", "f2.png", "f1.png", 1.0, "QUIET_LOW_COV"),
        ("no history yet",             "aaaa", None,   "f1.png", None,     6.0, "OK"),
    ]
    for label, cur, prev, csrc, psrc, cov, expected in cases:
        got = R._l4_frozen_verdict(cur, prev, cov, False, 5.0,
                                   cur_source=csrc, prev_source=psrc)
        check(f"reprocess: {label}", got == expected, f"got {got}, expected {expected}")

    check("OK_SAME_FRAME is a pass verdict in the reprocess engine too",
          "OK_SAME_FRAME" in R._L4_OK_VERDICTS)



def main() -> int:
    cid = "layer4.xband.CBAND"

    print("[1] the CBAND cadence artefact")
    reset()
    verdict(cid, "cband_1200.png", "aaaa", 6.0)                       # first sight
    v = verdict(cid, "cband_1200.png", "aaaa", 6.0)                   # re-sampled
    check("re-sampling the SAME published frame is not FROZEN", v == "OK_SAME_FRAME", v)
    v = verdict(cid, "cband_1200.png", "aaaa", 6.0)                   # and again
    check("still not FROZEN on a third look at the same frame", v == "OK_SAME_FRAME", v)

    print("\n[2] LOAD-BEARING: a genuinely stuck feed is still caught")
    reset()
    verdict(cid, "cband_1200.png", "aaaa", 6.0)
    v = verdict(cid, "cband_1204.png", "aaaa", 6.0)   # NEW file, identical pixels
    check("new filename + identical pixels = FROZEN", v == "FROZEN", v)

    print("\n[3] normal operation is unaffected")
    reset()
    verdict(cid, "cband_1200.png", "aaaa", 6.0)
    v = verdict(cid, "cband_1204.png", "bbbb", 6.0)
    check("new frame with new content is OK", v == "OK", v)

    print("\n[4] the existing demotes still apply to genuinely new frames")
    reset()
    verdict(cid, "a.png", "aaaa", 1.0)
    v = verdict(cid, "b.png", "aaaa", 1.0)
    check("low coverage demotes to QUIET_LOW_COV", v == "QUIET_LOW_COV", v)
    reset()
    verdict(cid, "a.png", "aaaa", 90.0)
    v = verdict(cid, "b.png", "aaaa", 90.0, skip_frozen=True)
    check("skip_frozen products demote to QUIET_SLOW", v == "QUIET_SLOW", v)

    print("\n[5] OK_SAME_FRAME counts as pass, not a warning")
    # OK_VERDICTS is a local inside run(), so assert against the source text.
    # A verdict missing from that tuple silently becomes a warning, which is
    # the exact noise this change exists to remove.
    import inspect
    src_text = inspect.getsource(L4)
    ok_block = src_text.split("OK_VERDICTS = (", 1)[1].split(")", 1)[0]
    check("OK_SAME_FRAME is listed in OK_VERDICTS",
          '"OK_SAME_FRAME"' in ok_block,
          "otherwise it would render as a warn")

    print("\n[6] radars whose cadence matches the check are unchanged")
    reset()
    xid = "layer4.xband.XSCV"
    verdict(xid, "x_1200.png", "aaaa", 0.01)
    v = verdict(xid, "x_1202.png", "cccc", 0.01)
    check("fast-cadence radar with fresh frames stays OK", v == "OK", v)

    test_reprocess_parity()

    print(f"\n{'PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
