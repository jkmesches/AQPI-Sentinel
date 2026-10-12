"""UTC day rollover — "the day just started" is not "nothing is arriving".

Run directly (no DB, no network):

    python validation_tests/test_utc_rollover.py

LB2 reads a dated tree (`XSCR/%Y/%m/%d`). For the first minutes of each UTC
day there is no directory for the new date — the day's first volume has not
landed — and the check read that one path and failed. Measured over the 25
days to 2026-10-12 on cira-aqpi:

    layer2.backend.XSCR    3/121 non-pass at 00Z vs 0/2879 the rest of the day
    layer2.backend.fleet  17/120              vs 77/2880

and confined to the first TEN minutes:

                           00-09   10-19   20-59
    layer2.backend.fleet   15/21    2/19    0/80
    layer2.backend.XSCR     3/20    0/20    0/80   <- all three of its failures

The fleet check was the loud one because the absence path published SILENT for
each affected radar in the same tick, and several radars silent at once is
what the fleet check is built to notice. One per-radar artifact became a
fleet-wide alarm.

The fix looks back one day. The four things that would make it wrong, each
pinned below:

  1. MASKING A REAL OUTAGE. If the fallback could ever produce an age younger
     than reality, it would silence exactly the radars it was meant to leave
     audible. Today must be tried FIRST and must only yield when it declares
     nothing at all — so the fallback is always a real observation time from
     yesterday and the threshold governs at every instant. This is the
     property that makes it a look-back and not a grace window, and a grace
     window is what this must never become: "don't alarm for the first N
     minutes" blinds the check to a radar that dies at 23:55.

  2. COSTING A READ IN THE STEADY STATE. The standing constraint on these
     hosts is to add no load. When today resolves, nothing else may be read,
     and a layout whose path ignores the date must not be read twice.

  3. SWALLOWING THE >24 h CASE. When NEITHER day resolves the radar really
     has been absent for over a day. SILENT is then true and the fleet should
     correlate on it; publishing something softer there would remove the
     signal rather than the artifact.

  4. RETRYING THE WRONG ERRORS. A permission error or a hung mount is not
     "the day just started". Only FileNotFoundError falls back.
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")
os.environ.setdefault("SENTINEL_BACKEND_ROOT", "/nonexistent-for-tests")

import backend.checks as _all                                  # noqa: E402,F401
from backend.checks import layer2_backend_radar as lb2         # noqa: E402
from backend.checks.helpers import first_resolvable            # noqa: E402
from backend.config import RADAR_SILENT_FAIL_S                 # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


RID = "XSCR"
LIMIT = RADAR_SILENT_FAIL_S.get(RID, 900)


class FakeTree:
    """A filesystem of {path: age_of_newest_file_in_seconds}.

    Anything not listed raises FileNotFoundError, which is what BOTH readers
    do for a missing directory — and what `_newest_declared` also does for a
    directory that exists but declares nothing parseable. The two are one
    condition here on purpose: that is exactly what makes the look-back cover
    the 00:00:xx case where the directory has been created but the first
    volume has not yet been renamed into place.
    """
    def __init__(self, ages: dict[str, float], error: Exception | None = None):
        self.ages = ages
        self.error = error
        self.reads: list[str] = []

    def __call__(self, path, *a, **k):
        self.reads.append(path)
        if self.error is not None:
            raise self.error
        if path not in self.ages:
            raise FileNotFoundError(f"no such directory: {path}")
        return _NOW.timestamp() - self.ages[path]


_NOW = datetime(2026, 10, 12, 0, 3, 17, tzinfo=timezone.utc)


def run_with(tree: FakeTree, rid: str = RID):
    """Drive the REAL run() against a stubbed filesystem and a frozen clock.

    Both readers are stubbed, not one. The check picks between them from
    config.LB2_FRESHNESS, and stubbing only the unused one exercises nothing
    — the mistake that let an earlier version of test_backend_checks pass
    while asserting against a real, absent path.
    """
    orig_dir, orig_decl, orig_now = lb2._dir_mtime, lb2._newest_declared, lb2.utcnow
    lb2._dir_mtime = tree
    lb2._newest_declared = tree
    lb2.utcnow = lambda: _NOW
    lb2._lb_last_verdict.clear()
    try:
        return asyncio.run(lb2.Layer2BackendRadarCheck(radar_id=rid).run(None))
    finally:
        lb2._dir_mtime, lb2._newest_declared, lb2.utcnow = orig_dir, orig_decl, orig_now


TODAY = lb2._radar_path(RID, _NOW)
YESTERDAY = lb2._radar_path(RID, _NOW - timedelta(days=1))


def main() -> int:
    print("the helper's contract")
    seen: list[str] = []

    def reader(p):
        seen.append(p)
        if p == "b":
            return 42.0
        raise FileNotFoundError(f"missing: {p}")

    check("returns the first candidate that resolves",
          first_resolvable(["a", "b", "c"], reader) == (42.0, "b", 1))
    check("and stops there — no later candidate is read", seen == ["a", "b"],
          str(seen))
    seen.clear()
    try:
        first_resolvable(["a", "a", "z"], reader)
    except FileNotFoundError as e:
        check("a repeated path is read once", seen == ["a", "z"], str(seen))
        check("the error names the OLDEST candidate tried, not the newest",
              "z" in str(e), str(e))
    seen.clear()
    try:
        first_resolvable([], reader)
        check("no candidates is a programming error, not a silent pass", False)
    except ValueError:
        check("no candidates is a programming error, not a silent pass", True)

    print("\nsteady state — today resolves")
    t = FakeTree({TODAY: 60.0, YESTERDAY: 7200.0})
    r = run_with(t)
    check("passes on a fresh volume", r.status == "pass", r.summary)
    check("reads today only", t.reads == [TODAY], str(t.reads))
    check("records days_back = 0", r.payload.get("days_back") == 0,
          str(r.payload.get("days_back")))
    check("and reports today's path", r.payload.get("path") == TODAY,
          str(r.payload.get("path")))

    print("\nrollover — today's directory does not exist yet")
    t = FakeTree({YESTERDAY: 74.0})
    r = run_with(t)
    check("passes, because the newest observation is 74 s old", r.status == "pass",
          r.summary)
    check("falls back only after trying today first",
          t.reads == [TODAY, YESTERDAY], str(t.reads))
    check("records days_back = 1, as an integer for the reprocess engine",
          r.payload.get("days_back") == 1, repr(r.payload.get("days_back")))
    check("and reports the path it actually read",
          r.payload.get("path") == YESTERDAY, str(r.payload.get("path")))
    check("the age is real, not a placeholder",
          70 <= r.payload.get("age_s", 0) <= 80, str(r.payload.get("age_s")))
    check("no SILENT verdict reaches the fleet",
          lb2._lb_last_verdict.get(RID, (None, None))[1] == "ARRIVING",
          str(lb2._lb_last_verdict.get(RID)))

    print("\nrollover — today's directory exists but declares nothing yet")
    # `_newest_declared` raises FileNotFoundError for this too, and just after
    # 00Z it is the more common shape: the directory is created when the
    # writer opens it, before the first volume is renamed into place.
    t = FakeTree({YESTERDAY: 95.0})
    r = run_with(t)
    check("is handled identically to an absent directory", r.status == "pass",
          r.summary)

    print("\nthe look-back must not mask a real outage")
    # The discriminating case, and the one the deployment verification turns
    # on: XSCV is genuinely down, and its failure count must not move.
    t = FakeTree({YESTERDAY: 10 * 3600.0})
    r = run_with(t)
    check("yesterday's newest being 10 h old still fails", r.status == "fail",
          r.summary)
    check("...and is labelled BACKEND SILENT", "BACKEND SILENT" in r.summary,
          r.summary)
    check("...and publishes SILENT to the fleet",
          lb2._lb_last_verdict.get(RID, (None, None))[1] == "SILENT",
          str(lb2._lb_last_verdict.get(RID)))

    # Physically impossible — yesterday's directory cannot hold a file newer
    # than today's — and asserted precisely because of that. It tests the
    # ORDER, which is the entire safety argument: an implementation that read
    # yesterday first, or took the max of the two, would pass every other
    # assertion in this file and silently report an age younger than reality.
    t = FakeTree({TODAY: LIMIT + 600.0, YESTERDAY: 30.0})
    r = run_with(t)
    check("today's reading wins even when yesterday's looks fresher",
          r.status == "fail" and r.payload.get("days_back") == 0,
          f"{r.status} days_back={r.payload.get('days_back')}")
    check("...and yesterday is never read at all", t.reads == [TODAY],
          str(t.reads))

    print("\nneither day — the radar really has been absent over 24 h")
    t = FakeTree({})
    r = run_with(t)
    check("fails", r.status == "fail", r.summary)
    check("says so in words, not as a missing directory",
          "today or yesterday" in r.summary and "24 h" in r.summary, r.summary)
    check("marks the payload absent", r.payload.get("absent") is True)
    check("records how many distinct days were tried",
          r.payload.get("days_tried") == 2, str(r.payload.get("days_tried")))
    check("publishes SILENT — true here, and the fleet should correlate on it",
          lb2._lb_last_verdict.get(RID, (None, None))[1] == "SILENT",
          str(lb2._lb_last_verdict.get(RID)))

    print("\nonly a missing directory falls back")
    t = FakeTree({}, error=PermissionError(13, "Permission denied"))
    r = run_with(t)
    check("a permission error is an error, not a silence", r.status == "error",
          f"{r.status}: {r.summary}")
    check("...and is not retried against yesterday", len(t.reads) == 1,
          str(t.reads))
    check("...and publishes UNREADABLE, not SILENT",
          lb2._lb_last_verdict.get(RID, (None, None))[1] == "UNREADABLE",
          str(lb2._lb_last_verdict.get(RID)))

    print("\nthe fleet amplification, which is what made this loud")
    # Every fleet radar at rollover: today absent, yesterday fresh. Before the
    # look-back each published SILENT in the same tick and the fleet check saw
    # fleet-wide silence.
    lb2._lb_last_verdict.clear()
    silent = []
    for rid in sorted(lb2.LB_FLEET):
        y = lb2._radar_path(rid, _NOW - timedelta(days=1))
        tree = FakeTree({y: 80.0})
        orig_dir, orig_decl, orig_now = (lb2._dir_mtime, lb2._newest_declared,
                                         lb2.utcnow)
        lb2._dir_mtime, lb2._newest_declared = tree, tree
        lb2.utcnow = lambda: _NOW
        try:
            asyncio.run(lb2.Layer2BackendRadarCheck(radar_id=rid).run(None))
        finally:
            lb2._dir_mtime, lb2._newest_declared, lb2.utcnow = (orig_dir, orig_decl,
                                                                orig_now)
        if lb2._lb_last_verdict.get(rid, (None, None))[1] == "SILENT":
            silent.append(rid)
    check("no fleet radar reports SILENT at 00:03Z", silent == [],
          ",".join(silent))
    check("every one of them reports ARRIVING",
          lb2._lb_not_arriving(_NOW) == set(),
          str(sorted(lb2._lb_not_arriving(_NOW))))
    lb2._lb_last_verdict.clear()

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall rollover assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
