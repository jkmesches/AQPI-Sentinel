"""Composite input staging — the gap between two checks that both say "fine".

Run directly (no DB, no network):

    python validation_tests/test_input_staging.py

Three stages sit between a radar and a composite, and until 2026-10-12 only
the ends were watched:

    DROPS dated tree    <- layer2.backend.<radar>    (LB2, arrival)
    recentfiles/<DIR>   <- nothing
    composite output    <- layer3.composite.<radar>  (LB3, participation)

The three NEXRAD feeds hid in the middle one for eighteen days after a K2
reboot left `ldm` stopped: KBBX/KDAX/KMUX staging directories held zero files
from 2026-09-17 to ~2026-10-06 while the composite went on publishing from six
radars instead of nine, and nothing in Sentinel looked at the one directory
where that was visible.

The XSCW version of this story stood here until 2026-10-12 and was wrong. It
paired a staging reading from 10-12 with a `layer2.backend.XSCW` pass from
10-08 and called that a simultaneous conjunction; re-measured, XSCW was simply
down and every stage said so. The tests below still use XSCW as a fixture
name, which is harmless — they build their own directories and assert on
behaviour, not on that radar's history.

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
# Its OWN root, and these checks do not exist without it. Deliberately not
# derived from SENTINEL_BACKEND_ROOT here either: a test that quietly accepts
# the backend root would pass against exactly the misconfiguration that shipped.
os.environ.setdefault("SENTINEL_RECENTFILES_DIR", "/nonexistent-for-tests/trinity")

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

    print("\nolder than the tree retains is the wrong tree, not a late radar")
    # v0.8.0 resolved this tree under backend_root, found a copy abandoned in
    # March 2026 with the right eight directory names, and reported
    # "20 staged, newest 298254.8 min old" as a PASS on every one of them.
    # Needs no fitted band: 202 days against a 1 h retention window is a
    # contradiction, not a close call.
    d = stage_dir(tmp, "XEBY_decoy", {declared(202 * 86400, rid="XEBY"): 0})
    r = run("XEBY", d)
    check("an age past the whole retention window is an ERROR",
          r.status == "error", f"{r.status}: {r.summary}")
    check("...blaming the tree, not the radar",
          "not a live staging tree" in r.summary, r.summary)
    check("...and naming the resolved path, which is the thing to check",
          "XEBY_decoy" in r.summary, r.summary)
    check("...flagged in the payload for the reprocess engine",
          r.payload.get("beyond_retention") is True)
    # And it must not fire on anything inside the window, band or no band.
    d = stage_dir(tmp, "XEBY_live", {declared(COMPOSITE_INPUT_RETENTION_S - 60,
                                               rid="XEBY"): 0})
    r = run("XEBY", d)
    check("a file inside the retention window is untouched by it",
          r.status == "pass", f"{r.status}: {r.summary}")

    print("\nwith no band, the age is recorded and not judged")
    # Shipped as a uniform 900 s for about an hour. Measured the same
    # afternoon: NEXRAD newest-declared ages reached 855 s, so 900 left 45 s of
    # headroom over the worst observation in a five-minute window — and
    # alerts.yaml routes `stage: LB3` to the standard policy with hourly
    # repeats, making that three recurring PAGES rather than dashboard noise.
    check("there is no band to be wrong about yet",
          COMPOSITE_INPUT_STALE_WARN_S is None,
          str(COMPOSITE_INPUT_STALE_WARN_S))
    d = stage_dir(tmp, "XSCW_old", {declared(3400): 0})
    r = run("XSCW", d)
    check("a very old staged volume does not warn", r.status == "pass",
          f"{r.status}: {r.summary}")
    # "not judged" and "judged healthy" are different claims. Recording the
    # second would make the staged-age series look assessed for the whole
    # period it was not, which is the same conflation as counting a demoted
    # skip as a pass.
    check("...and B_fresh is ABSENT, not set to pass",
          "B_fresh" not in (r.payload.get("sub_status") or {}),
          str(r.payload.get("sub_status")))
    check("...while the age is still recorded, which is what a band needs",
          3390 <= r.payload["age_s"] <= 3410, str(r.payload.get("age_s")))
    check("...and the summary claims no verdict about it",
          "STALE" not in r.summary, r.summary)

    print("\nsetting a band switches the sub-check on")
    # The mechanism has to be live and waiting for a number, not absent — or
    # the operator who finally measures one has nowhere to put it.
    import backend.thresholds as _th
    orig = _th.get_global
    _th.get_global = lambda k, default=None: (
        600 if k == "composite_input_stale_warn_s" else orig(k, default))
    try:
        r = run("XSCW", d)
    finally:
        _th.get_global = orig
    check("a stored threshold is picked up by the live check",
          r.status == "warn", f"{r.status}: {r.summary}")
    check("...and then it says the staging is stale",
          "STAGING STALE" in r.summary and r.payload["sub_status"]["B_fresh"] == "warn",
          r.summary)
    check("...and still never fails, because the band is warn-capped",
          r.status != "fail", r.status)

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
          r.payload["sub_status"] == {"A_staged": "pass"},
          str(r.payload.get("sub_status")))

    print("\nthe OLD variable name is not read, so a stale config fails safe")
    # Renamed in v0.8.3 because the meaning inverted: v0.8.2 appended
    # PRODUCTS/Radar_processed/recentfiles, so the same value is correct on one
    # version and wrong on the next. Reading the old name would silently
    # REINTERPRET a correct v0.8.2 config into nine plausible "staging
    # directory does not exist" rows whose real cause is the image tag.
    import subprocess as _sp
    env = {**os.environ, "SENTINEL_RECENTFILES_ROOT": "/some/project/root"}
    env.pop("SENTINEL_RECENTFILES_DIR", None)
    out = _sp.run(
        [sys.executable, "-c",
         "import backend.config as c, backend.checks, backend.registry as r;"
         "print('DIR=', c.SETTINGS.recentfiles_dir);"
         "print('N=', len([i for i in r.CHECKS if i.startswith('layer3.input.')]))"],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True, text=True, env=env)
    check("the old name does not populate the setting",
          "DIR= None" in out.stdout, out.stdout.strip()[:80])
    check("...so the nine checks do not register, which is the safe outcome",
          "N= 0" in out.stdout, out.stdout.strip()[:80])
    check("...and startup says so, naming the rename and the new meaning",
          "renamed to SENTINEL_RECENTFILES_DIR" in out.stderr
          and "recentfiles` directory" in out.stderr,
          out.stderr.strip()[-120:])

    print("\nthe dir pointing one level too shallow says so by name")
    # The misconfiguration the old variable name invited: in v0.8.2 the code
    # appended PRODUCTS/Radar_processed/recentfiles, so RECENTFILES_ROOT had to
    # point two levels ABOVE recentfiles. It was set from the name instead and
    # every row read
    # /recentfiles/PRODUCTS/Radar_processed/recentfiles/KBBX — a doubled path
    # reported as "the mount or the bind-mount boundary", which is true and
    # useless.
    project = tmp / "project_root"
    deep = project / "PRODUCTS" / "Radar_processed" / "recentfiles" / "KBBX"
    deep.mkdir(parents=True, exist_ok=True)
    # SETTINGS is a frozen dataclass, so swap the whole object rather than a
    # field — the same way a deployment changes it, by restarting with a
    # different env rather than by mutation.
    import dataclasses as _dc
    orig_settings = lb3.SETTINGS
    try:
        lb3.SETTINGS = _dc.replace(orig_settings, recentfiles_dir=str(project))
        chk = lb3.Layer3InputStagingCheck(radar_id="KBBX")
        orig_now = lb3.utcnow
        lb3.utcnow = lambda: _NOW
        try:
            r = asyncio.run(chk.run(None))
        finally:
            lb3.utcnow = orig_now
    finally:
        lb3.SETTINGS = orig_settings
    check("it names the variable rather than reporting a broken mount",
          "SENTINEL_RECENTFILES_DIR" in r.summary, r.summary)
    check("...and says which way it is wrong",
          "too shallow" in r.summary and "not the project root" in r.summary,
          r.summary)
    check("...and shows the path that DOES exist, which is the fix",
          r.payload.get("misconfigured_root", "").endswith("recentfiles/KBBX"),
          str(r.payload.get("misconfigured_root")))

    print("\na missing directory is not an empty one")
    r = run("XSCW", tmp / "does_not_exist")
    check("is an error about the mount, not a fail about the radar",
          r.status == "error", f"{r.status}: {r.summary}")
    check("...and says which", "directory does not exist" in r.summary, r.summary)
    check("...and is marked absent for the reprocess engine",
          r.payload.get("absent") is True)

    print("\nan absent directory carries the evidence to explain itself")
    # "staging directory does not exist" on its own sent two sessions guessing
    # on 2026-10-12 — at a wrong directory NAME, at the bind mount, and at a
    # dangling symlink — for a check whose own parent listing separates all
    # three. The guesswork is the bug this closes.
    peers = tmp / "tree_present"
    for name in ("XEBY", "XSCR", "XSCV", "XSCW", "XSWR", "KBBX"):
        (peers / name).mkdir(parents=True, exist_ok=True)
    r = run("CBAND", peers / "SSCB")
    check("a mounted tree missing ONE name says exactly that",
          "THIS name is not in it" in r.summary, r.summary)
    check("...and names the siblings, so the right name is readable off the row",
          "XEBY" in r.summary and r.payload.get("siblings") == sorted(
              ["XEBY", "XSCR", "XSCV", "XSCW", "XSWR", "KBBX"]),
          str(r.payload.get("siblings")))

    empty_parent = tmp / "tree_empty" / "recentfiles"
    empty_parent.mkdir(parents=True, exist_ok=True)
    r = run("CBAND", empty_parent / "SSCB")
    check("an empty parent is distinguished from a missing one",
          "EMPTY" in r.summary, r.summary)
    check("...and records the empty listing rather than None",
          r.payload.get("siblings") == [], str(r.payload.get("siblings")))

    r = run("CBAND", tmp / "no_such_tree" / "recentfiles" / "SSCB")
    check("an unreadable parent points at the mount, not at the radar",
          "parent is unreadable" in r.summary and "not this radar" in r.summary,
          r.summary)
    check("...and records siblings as None, which is not the same as empty",
          r.payload.get("siblings") is None, str(r.payload.get("siblings")))
    # A dangling symlink is the shape a multi-mount tree produces: present to
    # the host, absent inside the container. It must read as the sibling case,
    # because the parent IS mounted.
    link_parent = tmp / "tree_link"
    link_parent.mkdir(parents=True, exist_ok=True)
    (link_parent / "XEBY").mkdir(exist_ok=True)
    (link_parent / "SSCB").symlink_to(tmp / "nowhere_at_all")
    r = run("CBAND", link_parent / "SSCB")
    check("a dangling symlink reads as 'the name is here but does not resolve'",
          r.status == "error" and "THIS name is not in it" in r.summary,
          r.summary)
    check("...and the listing SHOWS it, which is what identifies a broken link",
          "SSCB" in (r.payload.get("siblings") or []),
          str(r.payload.get("siblings")))

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall input-staging assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
