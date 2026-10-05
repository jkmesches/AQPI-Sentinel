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
# Import what the APPLICATION imports, not just the check package.
#
# This probe used to import backend.checks alone, which measured a module
# graph the app never runs in — and therefore validated nothing about the
# deployed registry. prewarm.py imports backend.checks.layer2_radar at module
# scope (for EXPECTED_ABSENT_MOMENTS), which executed that module's
# registrations regardless of profile and put layer2.radar.FLOW and
# layer2.xband.fleet back into the xqpi registry. The assertions below said
# "no radarca-derived check reaches xqpi" and passed, while the live instance
# on cira-aqpi served 11 checks instead of 9.
#
# So the probe imports the app, and the gate it tests is now
# registry.register()'s own refusal rather than an import that happens not to
# occur. A future module imported from some unrelated helper cannot slip
# through the way this one did.
import backend.checks                       # must precede registry
import backend.prewarm                      # the importer that bypassed the gate
import backend.api.app                       # everything the server pulls in
from backend import config
from backend.registry import CHECKS
from backend.checks.layer1_backend_product import _product_dir
from backend.checks.layer2_backend_radar import _radar_path
import asyncio as _aio
from backend.api.routes.radars import list_radar_meta
def _radar_meta(): return _aio.run(list_radar_meta())
_NOW = datetime(2026, 10, 3, 22, 14, tzinfo=timezone.utc)
print("@@" + json.dumps({
    "profile":        config.SETTINGS.profile,
    "always_mods":    list(backend.checks._ALWAYS),
    "radarca_mods":   list(backend.checks._RADARCA),
    "has_radarca":    config.HAS_RADARCA,
    "backend_source": list(config.BACKEND_SOURCE),
    "products":       sorted(config.PRODUCTS),
    "radars":         sorted(config.RADAR_FOLDER),
    "site_name": config.SITE_NAME,
    "radar_meta": {r["id"]: r for r in _radar_meta()},
    "comp_extent": config.COMP_EXTENT or None,
    "comp_extent_provisional": config.COMP_EXTENT_PROVISIONAL,
    "lb1_paths": {p: _product_dir(p) for p in config.PRODUCTS},
    "lb2_paths": {r: _radar_path(r, _NOW) for r in config.RADAR_FOLDER},
    "lb2_freshness": config.LB2_FRESHNESS,
    "composite_expected": dict(config.COMPOSITE_EXPECTED_RADARS),
    "drops_tree": config.DROPS_TREE,
    "fleet_active": __import__(
        "backend.checks.layer3_backend_processing", fromlist=["x"]) is not None
    and getattr(__import__("backend.checks.layer2_backend_radar",
                           fromlist=["x"]), "_FLEET_ACTIVE"),
    "fleet_declined": getattr(__import__("backend.checks.layer2_backend_radar",
                                         fromlist=["x"]), "FLEET_NOT_REGISTERED"),
    "ts_patterns": dict(config.RAW_VOLUME_TS_RE),
    "declined": dict(__import__("backend.registry", fromlist=["x"]).DECLINED),
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
        # LB3 composite participation. xqpi DOES write a receipt — verified on
        # trinity, with FLOW, KSOX and KVTX in it — so FLOW's participation is
        # observable here. The DROPS producer check is NOT expected: that tree
        # is Gen_X-band_QPE.py's output and the profile sets DROPS_TREE = "".
        | {f"layer3.composite.{r}" for r in xqpi["radars"]}
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
    # The registry must have actively DECLINED the checks prewarm's import
    # re-registered, rather than merely not having seen them. If this is empty
    # the import skip is carrying the whole load again and the guard is
    # untested — which is how 11 checks reached production looking like 9.
    declined = xqpi["declined"]
    check("registry.register() actively declined the leaked L2 checks",
          set(declined) >= {"layer2.radar.FLOW", "layer2.xband.fleet"},
          f"declined={sorted(declined)}")
    check("...and attributes each to its radarca-only module",
          all(m in radarca_mods for m in declined.values()),
          str(sorted(set(declined.values()))))
    check("aqpi declines nothing", not aqpi["declined"],
          str(sorted(aqpi["declined"])))
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
    # Was "aqpi LB2 still resolves under PRODUCTS/DROPS/", which pinned the
    # defect: DROPS is the QPE generator's OUTPUT, so the check measured the
    # generator rather than the radar. Inverted on 2026-10-05 — the assertion
    # is kept rather than deleted so the old target cannot quietly return.
    check("aqpi LB2 resolves to the raw arrival tree, not DROPS",
          aqpi["lb2_paths"]["XEBY"] == f"{ROOT_MOUNT}/EBAY/2026/10/03",
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

    # ---- a monitored radar must be placeable on the map ------------------
    # /api/radars/meta served AQPI's nine hard-coded Bay Area radars on every
    # profile: folder=None throughout and no FLOW at all. That is upstream of
    # any map-extent work — extents cannot place a radar absent from the
    # payload. The invariant is that anything the profile MONITORS has
    # geography; the converse is fine, since AQPI's three NEXRADs are drawn
    # for context and deliberately not monitored.
    print("\nevery monitored radar has map geography:")
    for name, prof in (("aqpi", aqpi), ("xqpi", xqpi)):
        meta = prof["radar_meta"]
        missing = sorted(set(prof["radars"]) - set(meta))
        check(f"{name}: every radar in RADAR_FOLDER appears in /meta",
              not missing, ", ".join(missing))
        check(f"{name}: each one resolves a publish folder",
              all(meta[r].get("folder") for r in prof["radars"] if r in meta),
              ", ".join(r for r in prof["radars"]
                        if r in meta and not meta[r].get("folder")))
        # range_m is consumed numerically to draw a coverage ring
        # (MapView: r.range_m / 1000), so a null would break the map rather
        # than omit a ring.
        bad = sorted(r for r, m in meta.items()
                     if not isinstance(m.get("range_m"), (int, float)))
        check(f"{name}: every entry carries a numeric range_m", not bad,
              ", ".join(bad))
    check("aqpi still serves all nine radars",
          len(aqpi["radar_meta"]) == 9, str(sorted(aqpi["radar_meta"])))
    check("...and does not leak FLOW into the AQPI map",
          "FLOW" not in aqpi["radar_meta"])
    check("xqpi serves FLOW and nothing else",
          sorted(xqpi["radar_meta"]) == ["FLOW"], str(sorted(xqpi["radar_meta"])))
    flow = xqpi["radar_meta"].get("FLOW", {})
    # X-band, from TxFrequency 9.3993 GHz in FLOW's own volume headers; the
    # kind drives the map's marker styling and legend grouping.
    check("FLOW is typed as an X-band", flow.get("kind") == "xband",
          str(flow.get("kind")))
    check("FLOW sits where its volumes say it does",
          abs(flow.get("lat", 0) - 34.2048) < 1e-4
          and abs(flow.get("lon", 0) + 118.17081) < 1e-4,
          f"{flow.get('lat')},{flow.get('lon')}")
    check("FLOW's range is the derived last-gate range, not a guess",
          flow.get("range_m") == 40_346, str(flow.get("range_m")))

    # ---- the composite extent, where a profile supplies one --------------
    print("\nthe composite overlay box is consistent with the radar:")
    check("aqpi keeps the frontend's per-product table",
          aqpi["comp_extent"] is None, str(aqpi["comp_extent"]))
    check("...and claims nothing provisional", not aqpi["comp_extent_provisional"])
    extents = xqpi["comp_extent"] or {}
    check("xqpi places composite_ref", "composite_ref" in extents, str(sorted(extents)))
    # The QPE families render at 1697x2310 (aspect 0.735) where composite_ref
    # is 1365x1108 (1.232) over the sourced 936x760 grid. Giving them
    # composite_ref's box would place three products from a fourth's geometry.
    check("...and deliberately does NOT place the QPE families",
          not ({"qpe_15min", "qpe_1hr", "radar_precip_rate"} & set(extents)),
          str(sorted(extents)))
    check("nothing is flagged provisional now the extent is sourced",
          not xqpi["comp_extent_provisional"],
          str(xqpi["comp_extent_provisional"]))
    ext = extents.get("composite_ref")
    if ext:
        check("the box is not inside out",
              ext["west"] < ext["east"] and ext["south"] < ext["north"], str(ext))
        # The composite is built from FLOW alone, so whatever the domain is, it
        # has to contain the radar.
        check("the box contains FLOW",
              ext["west"] < flow["lon"] < ext["east"]
              and ext["south"] < flow["lat"] < ext["north"],
              f"{flow['lon']},{flow['lat']} vs {ext}")
        import math as _m
        ns = (ext["north"] - ext["south"]) * 110.574
        ew = (ext["east"] - ext["west"]) * 111.320 * _m.cos(_m.radians(flow["lat"]))
        # The grid is 936 x 760 cells at 250 m = 234.0 x 190.0 km in UTM. The
        # lat/lon box is its ENVELOPE, so it must come out slightly LARGER: a
        # UTM-aligned rectangle is a trapezoid in lat/lon, and the axis-aligned
        # bounds take the widest row and the tallest column. Measured 236.1 x
        # 192.3 km, i.e. 0.9% and 1.2% over, which is the ~4.7 km west-edge
        # skew showing up as expected rather than a transcription error.
        #
        # Asserted as a band, not a point, because these numbers are
        # hand-carried from an inverse transverse Mercator and a fat-fingered
        # digit would otherwise surface only as an overlay nobody can check by
        # eye. Too small would mean a lost corner; too large, a wrong zone.
        check("the box envelopes the 234 x 190 km grid, 0-3% over",
              1.0 <= ew / 234.0 < 1.03 and 1.0 <= ns / 190.0 < 1.03,
              f"{ew:.1f} x {ns:.1f} km "
              f"({ew / 234.0:.3f}x, {ns / 190.0:.3f}x)")
        # The east edge is exactly the UTM zone-11N false easting, which is
        # what identifies the domain as pinned to the central meridian rather
        # than arbitrarily placed.
        check("...with its east edge on the zone's central meridian",
              abs(ext["east"] + 117.0) < 1e-6, str(ext["east"]))
        rng_km = flow["range_m"] / 1000
        check("FLOW's ring is about a third of the domain width",
              0.30 < (2 * rng_km) / ew < 0.40, f"{(2 * rng_km) / ew:.3f}")

    print("\nthe deployment names itself:")
    check("aqpi is AQPI Sentinel", aqpi["site_name"] == "AQPI Sentinel",
          aqpi["site_name"])
    check("xqpi is XQPI Sentinel", xqpi["site_name"] == "XQPI Sentinel",
          xqpi["site_name"])

    # ---- LB2 reads arrival, and every radar has a pattern that matches ----
    # LB2 watched PRODUCTS/DROPS/<folder> until 2026-10-05 — the OUTPUT of a
    # QPE generator, one step downstream of the radar. When that generator
    # stopped at 04:07 UTC all five X-band checks failed for 12 h while three
    # of the radars were arriving within a minute throughout.
    print("\nLB2 measures arrival, per radar:")
    for name, prof in (("aqpi", aqpi), ("xqpi", xqpi)):
        check(f"{name}: freshness comes from the declared observation time",
              prof["lb2_freshness"] == "filename", prof["lb2_freshness"])
        missing = sorted(set(prof["radars"]) - set(prof["ts_patterns"]))
        check(f"{name}: every monitored radar has a timestamp pattern",
              not missing, ", ".join(missing))
        # A pattern that matches nothing makes _newest_declared raise, which
        # LB2 renders as "no data directory for the current UTC day" — against
        # a directory that may be full of current files.
        import re as _re
        bad = []
        for r, pat in prof["ts_patterns"].items():
            try:
                c = _re.compile(pat)
            except _re.error as e:
                bad.append(f"{r}: {e}"); continue
            if c.groups != 2:
                bad.append(f"{r}: {c.groups} groups, need 2 (date, time)")
        check(f"{name}: every pattern compiles and yields date+time groups",
              not bad, "; ".join(bad))
    check("no AQPI radar still points at the DROPS tree",
          not any("/DROPS/" in v for v in aqpi["lb2_paths"].values()),
          ", ".join(f"{k}={v}" for k, v in aqpi["lb2_paths"].items() if "/DROPS/" in v))
    check("XEBY resolves to EBAY, which is not derivable from its id",
          aqpi["lb2_paths"]["XEBY"].endswith("/EBAY/2026/10/03"),
          aqpi["lb2_paths"]["XEBY"])
    # The two producers name files differently and nothing reconciles them:
    # aqpi.scvw-<d>-<t>_... against AQPI.SSCB_<d>_<t>.nc. One shared pattern
    # matched 0 of 295 CBAND files.
    check("CBAND's pattern differs from the X-bands'",
          aqpi["ts_patterns"]["CBAND"] != aqpi["ts_patterns"]["XSCV"],
          "a single pattern cannot match both producers")
    import re as _re2
    check("...and matches a real CBAND filename",
          bool(_re2.compile(aqpi["ts_patterns"]["CBAND"])
               .match("AQPI.SSCB_20261005_162356.nc")))
    check("...while the X-band pattern does NOT match it",
          not _re2.compile(aqpi["ts_patterns"]["XSCV"])
                 .match("AQPI.SSCB_20261005_162356.nc"),
          "if this passes the two patterns were merged and CBAND is at risk")
    check("the X-band pattern matches a real X-band filename",
          bool(_re2.compile(aqpi["ts_patterns"]["XSCV"])
               .match("aqpi.scvw-20261005-162541_317_2_2_PPI.netcdf")))

    # ---- LB3 and fleet correlation --------------------------------------
    print("\nLB3 watches the gap between arrival and publication:")
    check("aqpi expects all six radars in the composite",
          sorted(aqpi["composite_expected"]) == sorted(aqpi["radars"]),
          str(sorted(aqpi["composite_expected"])))
    check("...and CBAND's receipt directory is SSCB, not derivable from its id",
          aqpi["composite_expected"].get("CBAND") == "SSCB",
          str(aqpi["composite_expected"].get("CBAND")))
    # The composite driver's own arrays intend nine inputs; the three NEXRAD
    # trees do not exist at all and that predates everything we have logs for.
    # Deriving `expected` from those arrays would fire forever on day one.
    check("...and no NEXRAD radar is in the expected set",
          not any(r.startswith("K") for r in aqpi["composite_expected"]),
          str(sorted(aqpi["composite_expected"])))
    check("xqpi expects only FLOW",
          sorted(xqpi["composite_expected"]) == ["FLOW"],
          str(sorted(xqpi["composite_expected"])))
    # KSOX and KVTX ARE in xqpi's receipt, as 18-day-old volumes being consumed
    # right now. Sentinel does not monitor their arrival, so it has no band to
    # judge them by; that finding belongs in a report, not in a check that
    # fires forever.
    check("...and not the two stale NEXRAD volumes in its receipt",
          "KSOX" not in xqpi["composite_expected"]
          and "KVTX" not in xqpi["composite_expected"])

    a_ids = {c["id"] for c in aqpi["checks"]}
    check("aqpi registers one participation check per radar",
          all(f"layer3.composite.{r}" in a_ids for r in aqpi["radars"]))
    check("...and exactly one DROPS producer check, not one per folder",
          len([i for i in a_ids if i.startswith("layer3.backend.")]) == 1,
          str(sorted(i for i in a_ids if i.startswith("layer3.backend."))))
    check("xqpi registers NO DROPS producer check",
          "layer3.backend.drops" not in x_ids,
          "gated on DROPS_TREE, which the profile states for itself")
    check("...because its profile declares no DROPS tree",
          xqpi["drops_tree"] == "", repr(xqpi["drops_tree"]))

    print("\nfleet correlation registers only where a fleet exists:")
    check("aqpi has a correlatable fleet", aqpi["fleet_active"] is True)
    check("...and registers the backend fleet check",
          "layer2.backend.fleet" in a_ids)
    # 3 of 1 is not a threshold that can be reached. A permanent skip row is
    # worse than an absent one: it reads as a broken check, not an N/A.
    check("xqpi does NOT register it", "layer2.backend.fleet" not in x_ids,
          "one radar cannot be correlated against itself")
    check("...and says why, rather than vanishing silently",
          isinstance(xqpi["fleet_declined"], str)
          and "cannot reach the systemic threshold" in xqpi["fleet_declined"],
          str(xqpi["fleet_declined"]))

    print("\nevery LB3 check targets a bare radar id, for the grouped rows:")
    for name, prof in (("aqpi", aqpi), ("xqpi", xqpi)):
        bad = sorted(f"{c['id']}->{c['target']}" for c in prof["checks"]
                     if c["id"].startswith("layer3.composite.")
                     and c["target"] != c["id"].rsplit(".", 1)[-1])
        check(f"{name}: participation target == radar id", not bad, "; ".join(bad))

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
