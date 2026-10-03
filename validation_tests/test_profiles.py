#!/usr/bin/env python3
"""SENTINEL_PROFILE must select a whole monitoring target, with nothing left over.

Why this exists
---------------
Sentinel was written for one deployment. Dr. Chandrasekar asked for a second
one -- "XQPI Sentinel", watching the FLOW radar -- and FLOW is not like AQPI in
the one way that matters most: nothing serves it over HTTP. There is no radarca
in front of it. The products land on a filesystem tree and that is all.

So roughly two thirds of the check stack has no subject under XQPI. A check with
no subject does not go quiet; it fails, forever, every cycle. Three of them got
as far as registering under the new profile during this work:

    layer0.net.radardisplay_tls   probes a display host XQPI does not have
    layer0.origin.episode         correlates slow episodes on an origin ditto
    layer0.origin.latency         times SETTINGS.base, which XQPI never sets

None of those would have been visible in a unit test of any individual check.
They are only visible in the *registry*, which is why the assertions here are
about the registry as a whole rather than about any check's behaviour.

The two failure directions are not symmetric, so both are tested:

  * something radarca-shaped survives into XQPI -- a permanently red tile, and
    worse, an operator learning to ignore red tiles;
  * something gets gated that AQPI still needs -- a check silently stops
    running on the deployment that has been relying on it for months, which is
    the more dangerous of the two because nothing turns red at all.

The XQPI registry is asserted as an EXACT set for that reason. The product and
radar parts are derived from the profile's own config, so adding an XQPI product
does not break this test; only the always-on L0 ids are literal, and a new
always-on check genuinely should require a deliberate edit here that says so.

Each profile is loaded in a subprocess: config.py reads the environment at
import time, so one interpreter can only ever hold one profile.

Run:  python3 validation_tests/test_profiles.py
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# L0 checks that have a subject on ANY deployment: they probe the internet and
# the host Sentinel runs on, neither of which depends on what is being watched.
UNIVERSAL_L0 = {
    "layer0.net.dns",
    "layer0.net.internet",
    "layer0.self.backup",
    "layer0.self.disk",
}

# Stages derived from scraping radarca. A check on one of these under a profile
# with no display tier is a permanent failure by construction.
RADARCA_STAGES = {"L0", "L1", "L2", "L3", "L3B", "L4-T1T2"}

# Deterministic, nonexistent mounts: enough to register the LB checks and to
# resolve their paths, without depending on a share being mounted. The paths are
# asserted as strings, so the test gives the same answer on a dev box as on
# cira-aqpi -- which is where a layout mistake would otherwise first appear, as
# "product directory does not exist" on every product at once.
ROOT_MOUNT = "/nonexistent-for-registration"
SSCB_MOUNT = "/nonexistent-sscb"

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


# --------------------------------------------------------------------------
# Load a profile in a child interpreter and report its registry
# --------------------------------------------------------------------------

_PROBE = r"""
import json, sys
from datetime import datetime, timezone
import backend.checks                       # must precede registry
from backend import config
from backend.registry import CHECKS
from backend.checks.layer1_backend_product import _product_dir
from backend.checks.layer2_backend_radar import _radar_path
_NOW = datetime(2026, 10, 3, 22, 14, tzinfo=timezone.utc)
print("@@" + json.dumps({
    "profile":        config.SETTINGS.profile,
    "always_mods":    list(backend.checks._ALWAYS),
    "radarca_mods":   list(backend.checks._RADARCA),
    "has_radarca":    config.HAS_RADARCA,
    "backend_source": list(config.BACKEND_SOURCE),
    "products":       sorted(config.PRODUCTS),
    "radars":         sorted(config.RADAR_FOLDER),
    "lb1_paths": {p: _product_dir(p) for p in config.PRODUCTS},
    "lb2_paths": {r: _radar_path(r, _NOW) for r in config.RADAR_FOLDER},
    "checks": [{"id": c.id, "stage": c.stage, "target": c.target,
                "module": type(c).__module__.rsplit(".", 1)[-1],
                "source_tag": c.source_tag, "source_label": c.source_label}
               for c in sorted(CHECKS.values(), key=lambda c: c.id)],
}))
"""


def load(profile: str) -> dict:
    """Import the backend under `profile` and return its registry summary."""
    env = dict(os.environ)
    env["SENTINEL_PROFILE"] = profile
    env["SENTINEL_DB_URL"] = "postgresql://unused/unused"
    # Register the backend checks and the CBAND special tree even here, where
    # neither share is mounted. Without these the LB stages would be absent and
    # the test would pass on a dev box and fail only where it is not run.
    env["SENTINEL_BACKEND_ROOT"] = ROOT_MOUNT
    env["SENTINEL_SSCB_ROOT"] = SSCB_MOUNT
    p = subprocess.run([sys.executable, "-c", _PROBE], cwd=str(ROOT), env=env,
                       capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stdout)
        print(p.stderr, file=sys.stderr)
        raise SystemExit(f"profile {profile!r} failed to import")
    line = [l for l in p.stdout.splitlines() if l.startswith("@@")]
    if not line:
        raise SystemExit(f"profile {profile!r} printed no summary:\n{p.stdout}")
    return json.loads(line[-1][2:])


def main() -> int:
    aqpi = load("aqpi")
    xqpi = load("xqpi")

    a_ids = {c["id"] for c in aqpi["checks"]}
    x_ids = {c["id"] for c in xqpi["checks"]}

    # ---- the default is AQPI, and AQPI is untouched ----------------------
    print("the default profile is AQPI and still scrapes radarca:")
    check("no SENTINEL_PROFILE means aqpi",
          load_default_profile() == "aqpi")
    check("aqpi declares a display tier", aqpi["has_radarca"] is True)
    check("aqpi publishes to K2", aqpi["backend_source"] == ["K2", "K2"],
          str(aqpi["backend_source"]))
    for st in ("L0", "L1", "L2", "L3", "L4-T1T2", "LB1", "LB2"):
        present = any(c["stage"] == st for c in aqpi["checks"])
        check(f"aqpi registers stage {st}", present)
    # The regression that would be invisible: a module moved into _RADARCA or a
    # registration gated on HAS_RADARCA that AQPI was relying on.
    for cid in ("layer0.origin.latency", "layer0.origin.episode",
                "layer0.net.radardisplay_tls", "layer0.origin.alive"):
        check(f"aqpi still registers {cid}", cid in a_ids)
    cband = [c for c in aqpi["checks"] if c["id"] == "layer2.backend.CBAND"]
    check("aqpi keeps the CBAND-on-trinity source override",
          bool(cband) and cband[0]["source_label"] == "Trinity",
          cband[0]["source_label"] if cband else "CBAND check not registered")

    # ---- XQPI watches FLOW, off trinity, and nothing else ----------------
    print("\nxqpi watches the FLOW radar and its four products:")
    check("xqpi declares no display tier", xqpi["has_radarca"] is False)
    check("xqpi publishes to trinity", xqpi["backend_source"] == ["TR", "Trinity"],
          str(xqpi["backend_source"]))
    check("the only radar is FLOW", xqpi["radars"] == ["FLOW"], str(xqpi["radars"]))
    check("four products are configured", len(xqpi["products"]) == 4,
          str(xqpi["products"]))

    expected = (
        UNIVERSAL_L0
        | {f"layer1.backend.{p}" for p in xqpi["products"]}
        | {f"layer2.backend.{r}" for r in xqpi["radars"]}
    )
    print("\nthe xqpi registry is exactly what the profile can observe:")
    extra = sorted(x_ids - expected)
    missing = sorted(expected - x_ids)
    check("nothing is registered that xqpi cannot observe", not extra,
          ", ".join(extra) or "")
    check("everything xqpi can observe is registered", not missing,
          ", ".join(missing) or "")

    print("\nno radarca-derived check reaches xqpi:")
    for st in sorted(RADARCA_STAGES - {"L0"}):
        leaked = sorted(c["id"] for c in xqpi["checks"] if c["stage"] == st)
        check(f"no stage-{st} check registered", not leaked, ", ".join(leaked))
    # L0 is the mixed stage -- some of it is universal, some of it is radarca.
    l0 = sorted(c["id"] for c in xqpi["checks"] if c["stage"] == "L0")
    check("every xqpi L0 check is one of the universal ones",
          set(l0) <= UNIVERSAL_L0, ", ".join(sorted(set(l0) - UNIVERSAL_L0)))
    # The structural version of the same claim, against the gating tuples
    # themselves rather than against the ids: no module in _RADARCA may
    # contribute a check under xqpi, and the two module lists must partition
    # the check package so a NEW module cannot be added to neither.
    radarca_mods = set(xqpi["radarca_mods"])
    from_radarca_mod = sorted(c["id"] for c in xqpi["checks"]
                              if c["module"] in radarca_mods)
    check("no xqpi check comes from a radarca-only module", not from_radarca_mod,
          ", ".join(from_radarca_mod))
    check("the two profiles agree on which modules are radarca-only",
          set(aqpi["radarca_mods"]) == radarca_mods)
    overlap = set(xqpi["always_mods"]) & radarca_mods
    check("_ALWAYS and _RADARCA are disjoint", not overlap, ", ".join(sorted(overlap)))

    pkg = ROOT / "backend" / "checks"
    on_disk = {f.stem for f in pkg.glob("layer*.py")}
    unlisted = sorted(on_disk - set(xqpi["always_mods"]) - radarca_mods)
    check("every check module on disk is in one list or the other", not unlisted,
          ", ".join(unlisted) or "")
    phantom = sorted((set(xqpi["always_mods"]) | radarca_mods) - on_disk)
    check("neither list names a module that does not exist", not phantom,
          ", ".join(phantom))

    # ---- published-tree layout -------------------------------------------
    # The two trees do not share a layout: K2 nests products under
    # realtime/product_images/, trinity's XQPI tree puts them directly under
    # PRODUCT_IMAGES/, and FLOW's volumes are date-partitioned where AQPI's are
    # flat. A prefix carried over from the wrong profile does not raise -- every
    # LB check just reports a missing directory, which reads as an outage.
    print("\nthe published-tree layout follows the profile, not the other tree:")
    check("aqpi LB1 still resolves under realtime/product_images/",
          all(v.startswith(f"{ROOT_MOUNT}/realtime/product_images/")
              for v in aqpi["lb1_paths"].values()),
          next((v for v in aqpi["lb1_paths"].values()
                if not v.startswith(f"{ROOT_MOUNT}/realtime/product_images/")), ""))
    check("aqpi LB1 keeps its unit subdirs",
          aqpi["lb1_paths"]["qpe_15min"].endswith("/rain15min/images/in/")
          and aqpi["lb1_paths"]["comp_ref"].endswith("/composite_ref_max/images/"),
          aqpi["lb1_paths"]["qpe_15min"])
    check("aqpi LB1 keeps the fcst_temp unit special case",
          aqpi["lb1_paths"]["fcst_temp"].endswith("/temperature/images/F/"),
          aqpi["lb1_paths"]["fcst_temp"])
    check("aqpi LB2 still resolves under PRODUCTS/DROPS/",
          aqpi["lb2_paths"]["XEBY"] == f"{ROOT_MOUNT}/PRODUCTS/DROPS/ebay",
          aqpi["lb2_paths"]["XEBY"])
    check("aqpi CBAND still resolves to its own dated mount",
          aqpi["lb2_paths"]["CBAND"] == f"{SSCB_MOUNT}/2026/10/03",
          aqpi["lb2_paths"]["CBAND"])

    # From 16-§1, verbatim. composite_ref has no unit subdir and a single
    # manifest; the other three have in/ and mm/ subtrees.
    want_lb1 = {
        "composite_ref":     f"{ROOT_MOUNT}/PRODUCT_IMAGES/composite_ref/images/",
        "qpe_15min":         f"{ROOT_MOUNT}/PRODUCT_IMAGES/qpe_15min/images/in/",
        "qpe_1hr":           f"{ROOT_MOUNT}/PRODUCT_IMAGES/qpe_1hr/images/in/",
        "radar_precip_rate": f"{ROOT_MOUNT}/PRODUCT_IMAGES/radar_precip_rate/images/in/",
    }
    for pid, want in want_lb1.items():
        got = xqpi["lb1_paths"].get(pid)
        check(f"xqpi LB1 {pid}", got == want, got or "not configured")
    check("xqpi FLOW resolves to its dated volume tree",
          xqpi["lb2_paths"]["FLOW"] == f"{ROOT_MOUNT}/flow/2026/10/03",
          xqpi["lb2_paths"]["FLOW"])
    check("no xqpi path carries K2's realtime/product_images/ prefix",
          not any("realtime/product_images" in v for v in
                  (*xqpi["lb1_paths"].values(), *xqpi["lb2_paths"].values())))

    print("\nsource tags stay renderable in a two-character column:")
    for name, prof in (("aqpi", aqpi), ("xqpi", xqpi)):
        bad = sorted(f"{c['id']}={c['source_tag']!r}" for c in prof["checks"]
                     if c["source_tag"] is not None and len(c["source_tag"]) != 2)
        check(f"{name}: every source_tag is exactly 2 chars", not bad,
              ", ".join(bad))
        tagged = [c for c in prof["checks"] if c["source_tag"] is not None]
        check(f"{name}: a tag always comes with a label",
              all(c["source_label"] for c in tagged))
    check("xqpi labels every backend check Trinity",
          all(c["source_label"] == "Trinity" for c in xqpi["checks"]
              if c["source_tag"] is not None))

    print("\nan unknown profile refuses to start:")
    env = dict(os.environ, SENTINEL_PROFILE="xqpu",
               SENTINEL_DB_URL="postgresql://unused/unused")
    p = subprocess.run([sys.executable, "-c", "from backend import config"],
                       cwd=str(ROOT), env=env, capture_output=True, text=True)
    check("a typo'd SENTINEL_PROFILE is fatal, not silently aqpi",
          p.returncode != 0, f"exit {p.returncode}")
    check("...and the error names the value and the valid options",
          "xqpu" in p.stderr and "aqpi" in p.stderr and "xqpi" in p.stderr,
          p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "no stderr")

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall profile assertions passed")
    return 1 if failures else 0


def load_default_profile() -> str:
    """What profile does an unset SENTINEL_PROFILE select?"""
    env = {k: v for k, v in os.environ.items() if k != "SENTINEL_PROFILE"}
    env["SENTINEL_DB_URL"] = "postgresql://unused/unused"
    p = subprocess.run(
        [sys.executable, "-c",
         "from backend import config; print(config.SETTINGS.profile)"],
        cwd=str(ROOT), env=env, capture_output=True, text=True)
    return p.stdout.strip().splitlines()[-1] if p.returncode == 0 else f"ERROR: {p.stderr}"


if __name__ == "__main__":
    sys.exit(main())
