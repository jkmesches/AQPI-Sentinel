#!/usr/bin/env python3
"""mtime is not a sound freshness basis on every published tree.

Why this exists
---------------
XQPI's volumes land on trinity, where a root gzip sweep walks the archive
daily around 07:25 UTC and rewrites files. Verified on csu-aqpi 2026-10-03:
the last file of 2026/09/25 and of 2026/09/30 both carry mtime
2026-10-03 01:25 local -- the same instant, five days apart -- against an
extension mix of 2,880 .gz to 4-9 .netcdf per day. The sweep touches the
current day's directory too.

So a check reading newest-mtime reports `pass` for up to its whole threshold
window after each sweep, whether or not the radar is producing. That is a
false negative in the one direction monitoring must never fail, and it recurs
daily. It is also how the survey's first pass got eight phantom multi-hour
outages whose magnitudes rose by exactly 1440 min/day going back -- the
interval from each day's real last file to the one sweep instant.

The live directory separately accumulates gzip's temp files: 14 on inspection,
including three suffixes for a single source volume. They are dot-prefixed,
they are the newest entries by mtime, and their names embed a real parseable
timestamp -- so only the leading dot tells them apart from data.

The fix is to read the observation time the data DECLARES. These tests build a
tree carrying both hazards and assert that:

  * the declared basis reads through a sweep that moves every mtime;
  * the mtime basis does NOT -- i.e. the hazard is real, not hypothetical, so
    this file fails if someone "simplifies" the basis back;
  * gzip temp dotfiles are excluded even though their names would parse;
  * AQPI's basis is untouched, because its tree does not have the defect.

That last point is the one worth being careful about: AQPI runs this code in
production (cira-aqpi, v0.5.6) over a K2 tree where newest-by-mtime equals
newest-by-filename, with no dotfiles and no compression pass, and over a DROPS
tree that is not uniform enough for a filename basis -- `ebay` holds flat
`.drops` files while `scvw` holds a nested `2026/` directory. Switching AQPI
would be a behavior change on a live deployment to fix a defect it does not
have.

Run:  python3 validation_tests/test_freshness_basis.py
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


# --------------------------------------------------------------------------
# Fixture: a trinity-shaped tree with the sweep and the temp files
# --------------------------------------------------------------------------

def build_tree(base: Path, now: datetime, last_obs_min_ago: float,
               sweep: bool) -> None:
    """One XQPI day directory plus the four composite families.

    `last_obs_min_ago` is how stale the newest real observation is.
    `sweep` sets every file's mtime to NOW, simulating the gzip pass -- which
    is the whole point: under a sweep the filesystem says everything is fresh.
    """
    day = now.strftime("%Y/%m/%d")
    vol = base / "flow" / day
    vol.mkdir(parents=True)

    newest_obs = now - timedelta(minutes=last_obs_min_ago)
    # A day of volumes ending at newest_obs, 2-minute cadence, 4 elevations.
    for i in range(40):
        t = newest_obs - timedelta(minutes=2 * i)
        for elev in ("2.5", "3.5", "4.5", "5.5"):
            ext = ".netcdf" if i < 2 else ".netcdf.gz"
            (vol / f"flow-{t:%Y%m%d-%H%M%S}_1_21_{elev}_PPI{ext}").write_bytes(b"x")

    # Two classes of litter, both dot-prefixed, which is why the exclusion keys
    # on the leading dot rather than on failing to parse a timestamp.
    #
    # 1. gzip's leftovers. Observed nine suffixes for ONE source volume inside a
    #    30 ms window -- gzip started nine times on the same file and left every
    #    attempt behind. Their names embed a real, parseable timestamp, so a
    #    pattern-only rule would accept them. Dated in the FUTURE here so a
    #    basis that reads them looks fresher than reality.
    bogus = now + timedelta(minutes=90)
    for suffix in ("faGvNy", "r3oxBg", "bnIjhZ", "iMHNAl", "FnTh1g",
                   "LIwYfB", "4u4pxw", "Zh16fg", "DV6tds"):
        (vol / f".flow-{bogus:%Y%m%d-%H%M%S}_1_21_2.5_PPI.netcdf.gz.{suffix}"
         ).write_bytes(b"x")
    # 2. An NFS silly-rename -- what the server leaves when a file is unlinked
    #    while still open. Carries NO timestamp at all, so no rule that works by
    #    extracting a time from the name can classify it either way. Only the
    #    leading dot catches both classes.
    (vol / ".nfs000000000b4a37d80000000a").write_bytes(b"x")

    pi = base / "PRODUCT_IMAGES"
    families = {
        "composite_ref":     (None, "details.json"),
        "qpe_15min":         ("in", "details_in.json"),
        "qpe_1hr":           ("in", "details_in.json"),
        "radar_precip_rate": ("in", "details_in.json"),
    }
    for fam, (unit, manifest) in families.items():
        imgs = pi / fam / "images" / unit if unit else pi / fam / "images"
        imgs.mkdir(parents=True)
        steps = []
        for i in range(15):
            t = newest_obs - timedelta(minutes=2 * i)
            name = f"{fam}_step_COMP_{t:%Y%m%d_%H%M%S}.png"
            (imgs / name).write_bytes(b"x")
            steps.append({"imageName": name,
                          "timestamp": t.strftime("%Y-%m-%dT%H:%M:%S")})
        # Orphans the rolling window never reclaimed -- real, observed on
        # qpe_15min (26 entries against a manifest of 14-15).
        for i in range(11):
            t = newest_obs - timedelta(days=30, minutes=2 * i)
            (imgs / f"{fam}_step_COMP_{t:%Y%m%d_%H%M%S}.png").write_bytes(b"x")
        (pi / fam / manifest).write_text(json.dumps({"product": fam, "steps": steps}))

    if sweep:
        swept = now.timestamp()
        for p in base.rglob("*"):
            os.utime(p, (swept, swept))


# --------------------------------------------------------------------------
# Run the real checks against the fixture, in a child process per profile
# --------------------------------------------------------------------------

_PROBE = r"""
import asyncio, json, sys
import backend.checks
from backend.registry import CHECKS
out = {}
async def main():
    for cid, c in CHECKS.items():
        if c.stage in ("LB1", "LB2"):
            r = await c.run(None)
            out[cid] = {"status": r.status, "age_s": r.payload.get("age_s"),
                        "basis": r.payload.get("freshness_basis"),
                        "summary": r.summary}
asyncio.run(main())
print("@@" + json.dumps(out))
"""


def run_checks(root: Path, profile: str, lb1: str | None = None,
               lb2: str | None = None) -> dict:
    env = dict(os.environ)
    env["SENTINEL_PROFILE"] = profile
    env["SENTINEL_DB_URL"] = "postgresql://unused/unused"
    env["SENTINEL_BACKEND_ROOT"] = str(root)
    env.pop("SENTINEL_SSCB_ROOT", None)
    # Force a basis, to demonstrate the hazard the real basis avoids.
    pre = ""
    if lb1 or lb2:
        pre = ("import backend.config as _c\n"
               + (f"_c.LB1_FRESHNESS = {lb1!r}\n" if lb1 else "")
               + (f"_c.LB2_FRESHNESS = {lb2!r}\n" if lb2 else "")
               + "import backend.checks.layer1_backend_product as _m1\n"
               + "import backend.checks.layer2_backend_radar as _m2\n"
               + (f"_m1.LB1_FRESHNESS = {lb1!r}\n" if lb1 else "")
               + (f"_m2.LB2_FRESHNESS = {lb2!r}\n" if lb2 else ""))
    p = subprocess.run([sys.executable, "-c", pre + _PROBE], cwd=str(ROOT),
                       env=env, capture_output=True, text=True)
    line = [l for l in p.stdout.splitlines() if l.startswith("@@")]
    if not line:
        print(p.stdout); print(p.stderr, file=sys.stderr)
        raise SystemExit(f"probe failed for {profile}")
    return json.loads(line[-1][2:])


def main() -> int:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # 200 min stale: well past FLOW's 1800 s (30 min) silence limit and past
    # the 25-min product limit, so a correct check must FAIL on both.
    STALE_MIN = 200.0

    with tempfile.TemporaryDirectory() as td:
        swept = Path(td) / "swept"
        build_tree(swept, now, STALE_MIN, sweep=True)

        print("a swept tree is stale in fact, however fresh its mtimes look:")
        got = run_checks(swept, "xqpi")
        lb2 = got["layer2.backend.FLOW"]
        check("LB2 uses the declared basis", lb2["basis"] == "filename", str(lb2["basis"]))
        check("LB2 fails on a 200-min-stale radar", lb2["status"] == "fail",
              lb2["summary"])
        check("...and reports the true age, not the sweep's",
              lb2["age_s"] is not None and abs(lb2["age_s"] / 60 - STALE_MIN) < 3,
              f"{(lb2['age_s'] or 0) / 60:.1f} min")
        for cid, r in sorted(got.items()):
            if r["basis"] == "manifest":
                check(f"LB1 {cid.split('.')[-1]} fails on stale manifest",
                      r["status"] == "fail", r["summary"])
                check(f"...{cid.split('.')[-1]} age is the declared age",
                      r["age_s"] is not None and abs(r["age_s"] / 60 - STALE_MIN) < 3,
                      f"{(r['age_s'] or 0) / 60:.1f} min")

        print("\nthe hazard is real: the mtime basis is fooled by the same tree:")
        fooled = run_checks(swept, "xqpi", lb1="newest_mtime", lb2="dir_mtime")
        f2 = fooled["layer2.backend.FLOW"]
        check("LB2 on dir_mtime passes a radar that has been silent 200 min",
              f2["status"] == "pass", f"{f2['status']} — {f2['summary']}")
        f1 = [r for k, r in fooled.items() if k.startswith("layer1.")]
        check("LB1 on newest_mtime passes every stale product",
              all(r["status"] == "pass" for r in f1),
              ", ".join(sorted({r["status"] for r in f1})))
        check("...so swapping the basis back would silently mask outages",
              f2["status"] == "pass" and lb2["status"] == "fail")

        print("\ngzip temp dotfiles are excluded though their names parse:")
        # The bogus temps embed now+90min. If they were counted the age would go
        # negative; the real newest is STALE_MIN behind.
        check("LB2 age is positive, so no temp file was read",
              (lb2["age_s"] or 0) > 0, f"{lb2['age_s']} s")
        check("...and is not pulled toward the temps' future stamp",
              (lb2["age_s"] or 0) / 60 > STALE_MIN - 3,
              f"{(lb2['age_s'] or 0) / 60:.1f} min vs {STALE_MIN}")
        # The NFS silly-rename is the case a timestamp-extracting rule cannot
        # classify at all: there is no time in the name to accept or reject.
        check("an NFS silly-rename neither matches nor breaks the scan",
              lb2["status"] == "fail" and (lb2["age_s"] or 0) > 0,
              f"{lb2['status']} @ {(lb2['age_s'] or 0) / 60:.1f} min")

        print("\na fresh tree passes, so the basis is not simply always-fail:")
        fresh = Path(td) / "fresh"
        build_tree(fresh, now, 4.0, sweep=False)
        ok = run_checks(fresh, "xqpi")
        check("LB2 passes a radar 4 min behind",
              ok["layer2.backend.FLOW"]["status"] == "pass",
              ok["layer2.backend.FLOW"]["summary"])
        check("every LB1 product passes",
              all(r["status"] == "pass" for k, r in ok.items()
                  if k.startswith("layer1.")),
              ", ".join(f"{k}={r['status']}" for k, r in sorted(ok.items())
                        if k.startswith("layer1.") and r["status"] != "pass"))
        check("manifest orphans do not count as the newest step",
              abs((ok["layer1.backend.qpe_15min"]["age_s"] or 0) / 60 - 4.0) < 3,
              f"{(ok['layer1.backend.qpe_15min']['age_s'] or 0) / 60:.1f} min")

    print("\nAQPI keeps the basis its tree actually supports:")
    out = subprocess.run(
        [sys.executable, "-c",
         "import backend.config as c; print(c.LB1_FRESHNESS, c.LB2_FRESHNESS)"],
        cwd=str(ROOT), capture_output=True, text=True,
        env=dict(os.environ, SENTINEL_DB_URL="postgresql://unused/unused",
                 **{k: v for k, v in os.environ.items() if k != "SENTINEL_PROFILE"}))
    basis = out.stdout.strip().splitlines()[-1] if out.returncode == 0 else out.stderr
    check("the default profile still reads mtime", basis == "newest_mtime dir_mtime",
          basis)

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall freshness-basis assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
