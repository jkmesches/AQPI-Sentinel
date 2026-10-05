#!/usr/bin/env python3
"""LB3 — composite participation and the DROPS producer.

The parser is the risky part of this stage, so it is tested against the
receipt EXACTLY as it exists on cira-aqpi, double slash and all:

    Radar files for composite:
    /trinity/.../recentfiles//XSCR/XSCR_volume_20261005-163823_drops.nc
    /trinity/.../recentfiles//SSCB/AQPI.SSCB_20261005_163719_drops.nc

Three things in that one file would each break a plausible parser:

  * the radar is spelled three ways across the naming conventions
    (``XSCR_volume``, ``AQPI.SSCB``, ``cfrad_KSOX``) while the DIRECTORY is
    uniform — so identity comes from the directory, not the filename;
  * ``recentfiles//XSCR`` carries a real double slash, because the writer
    concatenates a path that already ends in "/";
  * the timestamp separator differs by convention (``-`` for X-band, ``_`` for
    C-band and NEXRAD).

And the file is truncate-then-append with no rename, so a torn read is
reachable in normal operation and biased: the appends run X-band, then NEXRAD,
then SSCB, so a torn read systematically drops CBAND. A check that trusted one
would invent "CBAND absent from the composite" forever.

Run:  python3 validation_tests/test_backend_processing.py
"""
from __future__ import annotations
import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.mkdtemp(prefix="sentinel-lb3-")
os.environ.setdefault("SENTINEL_DB_URL", "postgresql://unused/unused")
os.environ["SENTINEL_BACKEND_ROOT"] = _TMP
os.environ.setdefault("SENTINEL_SSCB_ROOT", "/sscb")

from backend.checks import layer3_backend_processing as lb3   # noqa: E402
from backend.config import (COMPOSITE_EXPECTED_RADARS,         # noqa: E402
                            COMPOSITE_RECEIPT, RADAR_FOLDER,
                            RADAR_SILENT_FAIL_S)

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


PREFIX = "/trinity/projects/aqpiqpedev/PRODUCTS/Radar_processed/recentfiles/"


def line(rdir: str, name: str) -> str:
    # The double slash is deliberate and real: PREFIX already ends in "/".
    return f"{PREFIX}/{rdir}/{name}"


def stamp(dt: datetime, sep: str = "-") -> str:
    return dt.strftime(f"%Y%m%d{sep}%H%M%S")


def write_receipt(lines: list[str], mtime_age_s: float = 60.0) -> str:
    p = os.path.join(_TMP, COMPOSITE_RECEIPT)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write("Radar files for composite:\n")
        for ln in lines:
            fh.write(ln + "\n")
    # Back-date so the settle window does not make every test sleep 5s.
    t = time.time() - mtime_age_s
    os.utime(p, (t, t))
    lb3._reset_receipt_cache()
    return p


def receipt_now(ages_min: dict[str, float]) -> list[str]:
    """One line per radar, each aged by the given number of minutes."""
    out = []
    now = datetime.now(timezone.utc)
    for rdir, age in ages_min.items():
        dt = datetime.fromtimestamp(now.timestamp() - age * 60, timezone.utc)
        if rdir == "SSCB":
            out.append(line(rdir, f"AQPI.SSCB_{stamp(dt, '_')}_drops.nc"))
        else:
            out.append(line(rdir, f"{rdir}_volume_{stamp(dt, '-')}_drops.nc"))
    return out


def run(rid: str):
    return asyncio.run(
        lb3.Layer3CompositeParticipationCheck(radar_id=rid).run(None))


def main() -> int:
    print("the receipt as it actually exists — three naming conventions, one parser:")
    real = [
        line("XSCR", "XSCR_volume_20261005-163823_drops.nc"),
        line("XSCV", "XSCV_volume_20261005-163941_drops.nc"),
        line("XSWR", "XSWR_volume_20261005-163928_drops.nc"),
        line("SSCB", "AQPI.SSCB_20261005_163719_drops.nc"),
    ]
    p = write_receipt(real)
    parsed = lb3._read_receipt_blocking(p)
    got = parsed["contributions"]
    check("all four radars parsed", sorted(got) == ["SSCB", "XSCR", "XSCV", "XSWR"],
          str(sorted(got)))
    check("nothing was left unparsed", not parsed["unparsed"], str(parsed["unparsed"]))
    check("the double slash in recentfiles// did not break identity",
          "XSCR" in got, str(sorted(got)))
    check("the C-band underscore separator parsed as a timestamp",
          "SSCB" in got and got["SSCB"]["declared_ts"] > 0)
    # 16:38:23Z on 2026-10-05
    want = datetime(2026, 10, 5, 16, 38, 23, tzinfo=timezone.utc).timestamp()
    check("...and the X-band timestamp is read as UTC, to the second",
          abs(got["XSCR"]["declared_ts"] - want) < 1,
          f"{got['XSCR']['declared_ts']} vs {want}")
    want_c = datetime(2026, 10, 5, 16, 37, 19, tzinfo=timezone.utc).timestamp()
    check("...and so is the C-band one",
          abs(got["SSCB"]["declared_ts"] - want_c) < 1)
    check("identity comes from the DIRECTORY, not the filename",
          got["SSCB"]["path"].endswith("AQPI.SSCB_20261005_163719_drops.nc"),
          "SSCB's filename says AQPI.SSCB; its directory says SSCB")

    print("\nthe NEXRAD convention parses too, though it is not in the expected set:")
    p = write_receipt([line("KSOX", "cfrad_KSOX_20260917_155622_drops.nc")])
    got2 = lb3._read_receipt_blocking(p)["contributions"]
    check("cfrad_<RID>_<date>_<time> parses", "KSOX" in got2, str(sorted(got2)))
    check("...and is NOT in COMPOSITE_EXPECTED_RADARS",
          "KSOX" not in COMPOSITE_EXPECTED_RADARS,
          "the three NEXRAD trees do not exist; see config")

    print("\nCBAND's receipt directory is SSCB — not derivable from either name:")
    check("config maps CBAND -> SSCB",
          COMPOSITE_EXPECTED_RADARS.get("CBAND") == "SSCB",
          str(COMPOSITE_EXPECTED_RADARS.get("CBAND")))
    check("...and RADAR_FOLDER disagrees with both",
          RADAR_FOLDER["CBAND"] == "sscb",
          "three spellings of one radar: CBAND / SSCB / sscb")

    print("\na radar absent from the receipt fails — this is XEBY on 2026-10-05:")
    write_receipt(receipt_now({"XSCR": 3, "XSCV": 3, "XSWR": 3, "SSCB": 5}))
    r = run("XEBY")
    check("XEBY reports fail", r.status == "fail", r.status)
    check("...with A_included = fail", r.payload["sub_status"]["A_included"] == "fail")
    check("...and says NOT IN COMPOSITE in words", "NOT IN COMPOSITE" in r.summary,
          r.summary)
    check("...and names who IS present, so the row is actionable",
          "XSCR" in r.summary and "SSCB" in r.summary, r.summary)
    check("...and records included=0 as a metric",
          r.metrics.get("included") == 0.0)

    print("\na radar present and fresh passes:")
    r = run("XSCR")
    check("XSCR passes", r.status == "pass", f"{r.status}: {r.summary}")
    check("...A_included and B_fresh both pass",
          r.payload["sub_status"] == {"A_included": "pass", "B_fresh": "pass"},
          str(r.payload["sub_status"]))
    check("...and carries age_s in payload for the reprocess engine",
          isinstance(r.payload.get("age_s"), float), str(r.payload.get("age_s")))
    check("...and headroom for the sparkline",
          0.0 <= r.metrics.get("headroom", -1) <= 1.0,
          str(r.metrics.get("headroom")))

    print("\nbands are derived PER RADAR from that radar's own silence limit:")
    # CBAND's cadence is ~3x an X-band's. One global band would call a healthy
    # C-band contribution late on every single cycle.
    w_x, f_x = lb3._contrib_bands("XEBY")
    w_c, f_c = lb3._contrib_bands("CBAND")
    check("CBAND's fail band is looser than XEBY's", f_c > f_x,
          f"XEBY {f_x:.0f}s vs CBAND {f_c:.0f}s")
    check("...by exactly the difference in their silence limits",
          abs((f_c - f_x) - (RADAR_SILENT_FAIL_S["CBAND"]
                             - RADAR_SILENT_FAIL_S["XEBY"])) < 1,
          f"{f_c - f_x:.0f}s")
    check("warn sits at 0.8x fail, the same shape LB1/LB2 use",
          abs(w_x - f_x * 0.8) < 1, f"{w_x:.0f} vs {f_x * 0.8:.0f}")

    print("\na stale contribution warns, then fails:")
    _, fail_s = lb3._contrib_bands("XSCR")
    write_receipt(receipt_now({"XSCR": (fail_s * 0.9) / 60}))
    r = run("XSCR")
    check("inside the fail band but past warn -> warn",
          r.payload["sub_status"]["B_fresh"] == "warn", str(r.payload["sub_status"]))
    check("...and the overall status is warn, not fail", r.status == "warn", r.status)
    write_receipt(receipt_now({"XSCR": (fail_s + 120) / 60}))
    r = run("XSCR")
    check("past the fail band -> fail", r.payload["sub_status"]["B_fresh"] == "fail")
    check("...and says STALE CONTRIBUTION, not just a number",
          "STALE CONTRIBUTION" in r.summary, r.summary)
    check("...while A_included still passes — present-but-stale is not absent",
          r.payload["sub_status"]["A_included"] == "pass",
          "two distinct faults must not collapse into one")

    print("\nan absent receipt is a fail, and says so plainly:")
    pth = os.path.join(_TMP, COMPOSITE_RECEIPT)
    os.rename(pth, pth + ".gone")
    lb3._reset_receipt_cache()
    r = run("XSCR")
    check("receipt absent -> fail", r.status == "fail", r.status)
    check("...flagged in payload", r.payload.get("receipt_absent") is True)
    check("...and not reported as an error", r.status != "error",
          "the composite not running is a fault, not a failure to observe")
    os.rename(pth + ".gone", pth)
    lb3._reset_receipt_cache()

    print("\nthe torn read — reachable in normal operation, and biased:")
    # Truncate-then-append means a read can see the header plus the X-bands but
    # not yet SSCB. The whole point: that must NOT read as "CBAND absent".
    torn = receipt_now({"XSCR": 3, "XSCV": 3, "XSWR": 3})   # SSCB not yet appended
    write_receipt(torn)
    r = run("CBAND")
    check("a torn receipt still reports CBAND absent if we trust it",
          r.payload["sub_status"]["A_included"] == "fail",
          "this is the FAILURE MODE the mtime bracket exists to prevent")

    # Now prove the bracket catches a real tear: rewrite the file mid-read by
    # hooking open() so the content changes between the two stat() calls.
    full = receipt_now({"XSCR": 3, "XSCV": 3, "XSWR": 3, "SSCB": 5})
    p = write_receipt(full)
    real_open = open
    state = {"n": 0}

    def tearing_open(*a, **k):
        fh = real_open(*a, **k)
        state["n"] += 1
        if state["n"] == 1:
            # Append after the read has taken its first stat, and bump mtime.
            with real_open(p, "a") as w:
                w.write(line("SSCB", "AQPI.SSCB_20261005_170000_drops.nc") + "\n")
            os.utime(p, None)
        return fh

    lb3._reset_receipt_cache()
    import builtins
    builtins.open = tearing_open
    try:
        caught = False
        try:
            lb3._read_receipt_blocking(p)
        except lb3.TornReceipt:
            caught = True
    finally:
        builtins.open = real_open
    check("a receipt rewritten mid-read raises TornReceipt rather than parsing",
          caught, "detected via stat() bracket, not guessed from content")

    print("\nthe settle window covers the tear the stat bracket cannot:")
    # Between two appends the mtime is momentarily STABLE, so a read that
    # starts and finishes in that gap sees a short file with nothing changing
    # under it — the bracket is blind to this one. The settle window is what
    # catches it, by refusing to read a receipt that was just written.
    short = receipt_now({"XSCR": 3, "XSCV": 3, "XSWR": 3})   # SSCB not yet appended
    p2 = write_receipt(short, mtime_age_s=0.2)               # as if mid-write
    t_start = time.time()
    lb3._read_receipt_blocking(p2)
    waited = time.time() - t_start
    check("a receipt written 0.2 s ago is waited out, not read immediately",
          waited >= 4.0, f"waited {waited:.1f}s for a {4.8:.1f}s remaining window")
    p3 = write_receipt(short, mtime_age_s=60.0)
    t_start = time.time()
    lb3._read_receipt_blocking(p3)
    check("...while a settled receipt is read without waiting",
          time.time() - t_start < 1.0, f"{time.time() - t_start:.2f}s")

    print("\nevery radar in a cycle sees ONE receipt version:")
    # Otherwise XEBY could be judged against one composite and CBAND the next,
    # and the reported set would be stitched from two composites.
    write_receipt(receipt_now({"XSCR": 3, "SSCB": 5}))
    seen = [tuple(run(r).payload.get("receipt_radars") or [])
            for r in ("XSCR", "CBAND", "XSCV")]
    check("all three radars report the same receipt contents",
          len(set(seen)) == 1, str(seen))
    check("...and the cache key is the receipt's own (mtime, size)",
          lb3._receipt_cache is not None, "parsed once, read by all")

    print("\nthe DROPS producer check is informational by construction:")
    root = os.path.join(_TMP, "PRODUCTS", "DROPS")
    for rid, folder in RADAR_FOLDER.items():
        if rid != "CBAND":
            os.makedirs(os.path.join(root, folder), exist_ok=True)
    d = lb3.Layer3DropsProducerCheck()
    r = asyncio.run(d.run(None))
    check("a fresh DROPS tree passes", r.status == "pass", f"{r.status}: {r.summary}")
    check("...and CBAND is not among the folders it reads",
          "CBAND" not in r.payload["sub_status"],
          "DROPS is Gen_X-band_QPE.py's output; CBAND does not pass through it")
    check("...one check for the producer, not one per folder",
          len(r.payload["sub_status"]) == 5 and r.target == "drops-qpe",
          f"{len(r.payload['sub_status'])} folders in one row")

    # Age every folder past the limit — 12 h, the real 2026-10-05 duration.
    stale = time.time() - 12 * 3600
    for rid, folder in RADAR_FOLDER.items():
        if rid != "CBAND":
            os.utime(os.path.join(root, folder), (stale, stale))
    r = asyncio.run(d.run(None))
    check("a producer dead 12 h reports WARN, never fail",
          r.status == "warn", r.status)
    check("...because fail would auto-promote to critical at 30 min and page",
          r.status != "fail", "severity is a routing decision, not an observation")
    check("...but the summary states the real duration in words",
          "STALLED" in r.summary and "12.0 h" in r.summary, r.summary)
    check("...and says no live product reads the tree",
          "no live product" in r.summary, r.summary)
    check("...while payload.age_s carries the true age for reprocess",
          r.payload.get("age_s", 0) > 11 * 3600, str(r.payload.get("age_s")))
    check("...and payload is flagged informational",
          r.payload.get("informational") is True)

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall LB3 assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
