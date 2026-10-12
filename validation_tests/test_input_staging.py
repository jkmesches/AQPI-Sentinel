"""Composite input staging — the gap between two checks that both say "fine".

Run directly (no DB, no network):

    python validation_tests/test_input_staging.py

Three stages sit between a radar and a composite, and until 2026-10-12 only
the ends were watched:

    DROPS dated tree    <- layer2.backend.<radar>    (LB2, arrival)
    recentfiles/<DIR>   <- nothing
    composite output    <- layer3.composite.<radar>  (LB3, participation)

XSCW had ZERO files staged in the middle one while `layer2.backend.XSCW` read
pass with "newest volume 0.5 min ago", because LB2 reads the DROPS tree and
these are different paths. The composite was building a third of its output
without that radar. The same shape hid the three NEXRAD feeds for eighteen
days after a K2 reboot: their staging directories held zero files and nothing
in Sentinel looked at the one directory where that was visible.

What this check must not do, each a mistake already made elsewhere in this
codebase and each pinned below:

  1. REPORT OUR BLINDNESS AS THE SUBJECT'S OUTAGE. A directory holding files
     that our pattern does not match is OUR problem. Reporting it as "nothing
     staged" is what rendered a CBAND directory of 295 current files as "no
     data directory for the current UTC day".

  2. PAGE FOR A RADAR THAT IS DOWN. XSCV's staging directory is correctly
     empty because the radar is off. An ungated check turns that into a
     permanent alarm that restates layer2.radar.XSCV and buries the one case
     worth seeing — the conjunction LB2 PASS plus staging EMPTY.

  3. DISCARD THE OBSERVATION WHILE SUPPRESSING THE PAGE. `depends_on` demotes
     the result to skip, which throws away a reading we actually took.
     `alarm_only_depends_on` keeps the row true and collapses the page, and
     the difference is exactly why the base class carries both.

  4. IMPLY AN AGE IT CANNOT KNOW. This tree retains about an hour, so once
     staging stops the evidence ages out and "stopped 70 minutes ago" is
     indistinguishable from "stopped three days ago".
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")
# Registration is gated on these being set, not on them existing — the checks
# are built at import time and the paths are only read when one runs, which
# every case below stubs. Without them this file asserts against an empty
# registry and passes by measuring nothing.
os.environ.setdefault("SENTINEL_BACKEND_ROOT", "/nonexistent-for-tests")
os.environ.setdefault("SENTINEL_SSCB_ROOT", "/nonexistent-for-tests/sscb")

import backend.checks as _all                                   # noqa: E402,F401
from backend.checks import layer3_backend_processing as lb3     # noqa: E402
from backend.config import (COMPOSITE_INPUT_DIR,                # noqa: E402
                            COMPOSITE_INPUT_RETENTION_S,
                            COMPOSITE_INPUT_STALE_WARN_S,
                            COMPOSITE_INPUT_TS_RE, RADAR_FOLDER)
from backend.registry import CHECKS                             # noqa: E402
from backend.stages import stage_tier                           # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


_NOW = datetime(2026, 10, 12, 17, 20, tzinfo=timezone.utc)


def stage_dir(tmp: Path, name: str, files: dict[str, float] | None,
              mtimes: dict[str, float] | None = None) -> Path:
    """Build one staging directory. `files` maps filename -> declared age (s)."""
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    for fname, age in (files or {}).items():
        p = d / fname
        p.write_text("x")
        if mtimes and fname in mtimes:
            ts = _NOW.timestamp() - mtimes[fname]
            os.utime(p, (ts, ts))
    return d


def declared(age_s: float, pattern: str = "{rid}_volume_%Y%m%d-%H%M%S_drops.nc",
             rid: str = "XSCW") -> str:
    t = _NOW - timedelta(seconds=age_s)
    return t.strftime(pattern.replace("{rid}", rid))


def run(radar_id: str, path: Path):
    """Drive the REAL run() against a real temporary directory.

    A real directory rather than a stubbed scandir: the thing most likely to be
    wrong is how entries are filtered and parsed, and a stub would be my
    reading of os.scandir rather than os.scandir.
    """
    chk = lb3.Layer3InputStagingCheck(radar_id=radar_id)
    orig_now, orig_path = lb3.utcnow, chk._path
    lb3.utcnow = lambda: _NOW
    chk._path = lambda: str(path)
    try:
        return asyncio.run(chk.run(None))
    finally:
        lb3.utcnow, chk._path = orig_now, orig_path


def main() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="staging-"))

    print("the check covers the composite's input set, not the radar table")
    ids = sorted(i for i in CHECKS if i.startswith("layer3.input."))
    check("one check per staging directory",
          len(ids) == len(COMPOSITE_INPUT_DIR), f"{len(ids)} vs {len(COMPOSITE_INPUT_DIR)}")
    check("the three NEXRAD sites are covered",
          all(f"layer3.input.{r}" in CHECKS for r in ("KBBX", "KDAX", "KMUX")),
          ",".join(ids))
    check("...and are NOT in the arrival table, so this is their only coverage",
          not any(r in RADAR_FOLDER for r in ("KBBX", "KDAX", "KMUX")))
    check("it is a backend-tier stage", all(CHECKS[i].stage == "LB3" for i in ids))
    check("...so a radarca episode cannot silence it",
          all(stage_tier(CHECKS[i].stage) == "backend" for i in ids)
          and not any("layer0.origin.episode" in (CHECKS[i].alarm_only_depends_on or [])
                      for i in ids),
          str(CHECKS[ids[0]].alarm_only_depends_on))

    print("\nthe alarm is gated on LB2, the observation is not")
    xeby = CHECKS.get("layer3.input.XEBY")
    if xeby is not None:
        check("a radar with an LB2 check gates its alarm on it",
              xeby.alarm_only_depends_on == ["layer2.backend.XEBY"],
              str(xeby.alarm_only_depends_on))
        check("...via alarm_only_depends_on, which keeps the reading",
              xeby.depends_on == [], str(xeby.depends_on))
    check("a radar with no LB2 check gates on nothing, rather than on a ghost",
          CHECKS["layer3.input.KBBX"].alarm_only_depends_on == [],
          str(CHECKS["layer3.input.KBBX"].alarm_only_depends_on))

    print("\nhealthy staging")
    d = stage_dir(tmp, "XSWR_ok", {
        declared(110, rid="XSWR"): 110,
        declared(230, rid="XSWR"): 230,
        declared(350, rid="XSWR"): 350,
    })
    r = run("XSWR", d)
    check("passes", r.status == "pass", r.summary)
    check("counts what is staged", r.payload["staged_files"] == 3,
          str(r.payload.get("staged_files")))
    check("and reports the newest declared age",
          105 <= r.payload["age_s"] <= 115, str(r.payload.get("age_s")))

    print("\nall three naming conventions parse")
    for rid, name in (
        ("XSWR", "XSWR_volume_20261012-171800_drops.nc"),
        ("KBBX", "cfrad_KBBX_20261012_171800_drops.nc"),
        ("CBAND", "AQPI.SSCB_20261012_171800_drops.nc"),
    ):
        d = stage_dir(tmp, f"{rid}_shape", {name: 0})
        r = run(rid, d)
        check(f"{rid}: {name.split('_')[0]}-style name is read",
              r.payload.get("staged_files") == 1, f"{r.status}: {r.summary}")

    print("\nnothing staged — the finding this check exists for")
    d = stage_dir(tmp, "XSCW_empty", {})
    r = run("XSCW", d)
    check("fails", r.status == "fail", r.summary)
    check("says nothing is staged, in those words",
          "NOTHING STAGED" in r.summary, r.summary)
    # See refusal 4: this tree keeps about an hour, so a duration here would be
    # invented. The summary must not carry one.
    check("...and claims no duration it cannot know",
          not any(w in r.summary for w in ("min", "hour", "ago", "h old")),
          r.summary)
    check("the retention bound is recorded for whoever reads the payload",
          r.payload.get("retention_s") == COMPOSITE_INPUT_RETENTION_S,
          str(r.payload.get("retention_s")))

    print("\nour pattern failing is OUR fault, and must not read as the radar's")
    d = stage_dir(tmp, "XSCW_weird", {
        "volume-aaaa.nc": 0, "volume-bbbb.nc": 0, "readme.txt": 0,
    })
    r = run("XSCW", d)
    check("is an error, not a fail", r.status == "error", f"{r.status}: {r.summary}")
    check("...and does NOT say nothing is staged",
          "NOTHING STAGED" not in r.summary, r.summary)
    check("...it names the pattern as the thing that is stale",
          "pattern" in r.summary and "not the radar" in r.summary, r.summary)
    check("...and records the pattern so it can be compared to reality",
          r.payload.get("pattern") == COMPOSITE_INPUT_TS_RE,
          str(r.payload.get("pattern")))
    check("...and counts the entries it could not read",
          r.payload.get("staged_entries") == 3,
          str(r.payload.get("staged_entries")))

    print("\nstale staging warns, and cannot fail")
    d = stage_dir(tmp, "XSCW_stale",
                  {declared(COMPOSITE_INPUT_STALE_WARN_S + 600): 0})
    r = run("XSCW", d)
    check("warns", r.status == "warn", f"{r.status}: {r.summary}")
    check("...never fails on an age, because no band has been fitted",
          r.payload["sub_status"]["B_fresh"] == "warn",
          str(r.payload.get("sub_status")))
    check("...and says it is the staging that is stale",
          "STAGING STALE" in r.summary, r.summary)

    print("\ndotfiles are skipped before matching, not after")
    # A writer's temporaries carry a real parseable timestamp and are the
    # newest entries; only the leading dot distinguishes them. Counting one
    # would report staging as fresher than it is.
    d = stage_dir(tmp, "XSCW_dot", {
        declared(3000): 0,
        "." + declared(5): 0,
    })
    r = run("XSCW", d)
    check("the temporary file is not counted", r.payload["staged_files"] == 1,
          str(r.payload.get("staged_files")))
    check("...and does not make staging look fresh",
          r.payload["age_s"] > 2000, str(r.payload.get("age_s")))

    print("\ndeclared-versus-written skew is recorded, never judged")
    # The only quantity that can tell a stalled producer from one replaying old
    # scans into a fresh file. Retention bounds a file's MTIME while this tree
    # is read by its DECLARED time, and conflating those two is what made a
    # 22.8 h contribution age look impossible when it was merely unexplained.
    name = declared(900)
    d = stage_dir(tmp, "XSCW_skew", {name: 0}, mtimes={name: 300})
    r = run("XSCW", d)
    check("skew is the gap between declaring and writing",
          580 <= r.payload.get("staged_skew_s", 0) <= 620,
          str(r.payload.get("staged_skew_s")))
    check("...and does not change the verdict",
          r.payload["sub_status"]["B_fresh"] in ("pass", "warn"),
          str(r.payload.get("sub_status")))

    print("\na missing directory is not an empty one")
    r = run("XSCW", tmp / "does_not_exist")
    check("is an error about the mount, not a fail about the radar",
          r.status == "error", f"{r.status}: {r.summary}")
    check("...and says which", "directory does not exist" in r.summary, r.summary)
    check("...and is marked absent for the reprocess engine",
          r.payload.get("absent") is True)

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall input-staging assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
