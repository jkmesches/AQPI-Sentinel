"""Layer 2 — per-radar reconciliation.

For each radar in the declared status list AND each X-band productPrefix,
cross-check the dashboard's declared UP/DOWN against the observed 60-minute
image window from /api/xbandRadarImages.

Also: mosaic-membership check (composite product description vs declared
status), and a per-moment matrix per radar.
"""
from __future__ import annotations
import sys
from datetime import datetime, timezone
import requests

from config import BASE, RADAR_FOLDER, STATUS_TO_RADAR, X_MOMENTS, moment_to_prefix

# Live-probe timeout. Deliberately matches the production ceiling
# (transports/http.DEFAULT_TIMEOUT_S) rather than being a round number: these
# tests hit the same endpoint the fleet does, and a ceiling below it fails for
# reasons unrelated to what is being tested. The 10s values here were set when
# upstream's p95 was ~2.4s; measured 2026-08-31 its p90 is 8-13s, so a 10s
# ceiling coin-flips. Raise this only alongside DEFAULT_TIMEOUT_S.
LIVE_TIMEOUT_S = 20

# Threshold: observed silence longer than this with declared=UP = "ghost UP"
SILENT_FAIL_S = 600   # 10 min


def declared_status():
    r = requests.get(f"{BASE}/api/radar-status/", timeout=LIVE_TIMEOUT_S)
    r.raise_for_status()
    return {row["radar"]: row["status"] for row in r.json()}


def observed_window(radar_id: str, prefix: str) -> dict:
    """radar_id is the public id (XSCW, XEBY, CBAND, ...); we translate to the
    on-disk folder slug ('scwa', 'ebay', 'sscb', ...) before hitting the API."""
    folder = RADAR_FOLDER.get(radar_id, radar_id.lower())
    r = requests.get(f"{BASE}/api/xbandRadarImages/",
                     params={"radarFolder": folder, "productPrefix": prefix},
                     timeout=LIVE_TIMEOUT_S)
    if r.status_code != 200:
        return {"http": r.status_code, "error": True}
    d = r.json()
    images = d.get("images", []) or []
    meta = d.get("meta", []) or []
    return {
        "http": 200,
        "n_images": len(images),
        "window_start": d.get("windowStartUtc"),
        "window_end":   d.get("windowEndUtc"),
        "plots_root":   d.get("plotsRoot"),
        "sample":       images[-1] if images else None,
    }


def reconcile(declared: str, observed: dict) -> str:
    if observed.get("error"):
        return "OBSERVED_API_ERROR"
    n = observed.get("n_images", 0)
    if declared == "UP" and n > 0:
        return "HEALTHY"
    if declared == "UP" and n == 0:
        return "GHOST_UP"            # silent failure — status lies
    if declared == "DOWN" and n == 0:
        return "CONFIRMED_DOWN"
    if declared == "DOWN" and n > 0:
        return "STUCK_DOWN_FLAG"      # status says DOWN but data is flowing
    return "UNKNOWN"


def main():
    print("="*82)
    print(" LAYER 2 — radar declared-status vs observed image flow")
    print("="*82)

    dec_raw = declared_status()
    # normalize the status keys to the radar ids used by the JS bundle
    dec = {STATUS_TO_RADAR.get(k, k): v for k, v in dec_raw.items()}
    print(f"\n/api/radar-status/ returned {len(dec_raw)} entries (mapped to radar ids):")
    for r, s in dec.items():
        print(f"  {r:<8} {s}")

    # Primary moment: Reflectivity → CorrReflectivity (most fundamental).
    radars = list(dec.keys())
    primary_moment = "Reflectivity"
    print(f"\n--- Reconciliation on '{primary_moment}' (1-h rolling window) ---")
    print(f"{'radar':<8} {'folder':<6} {'declared':<6} {'n_imgs':>6}  {'verdict':<18} window")
    overall = {}
    for radar in radars:
        folder = RADAR_FOLDER.get(radar, radar.lower())
        prefix = moment_to_prefix(radar, primary_moment)
        obs = observed_window(radar, prefix)
        v = reconcile(dec[radar], obs)
        overall[radar] = (dec[radar], obs, v)
        window = f"{obs.get('window_start','-')[:19]} → {obs.get('window_end','-')[:19]}"
        print(f"{radar:<8} {folder:<6} {dec[radar]:<6} {str(obs.get('n_images','-')):>6}  {v:<18} {window}")

    # Moment-level matrix per radar
    print(f"\n--- Per-radar dual-pol moment availability (1-h window) ---")
    header = f"{'radar':<8}  " + "  ".join(f"{m[:14]:<14}" for m in X_MOMENTS)
    print(header)
    for radar in radars:
        cells = []
        for m in X_MOMENTS:
            prefix = moment_to_prefix(radar, m)
            obs = observed_window(radar, prefix)
            if obs.get("error"):
                cells.append("ERR")
            else:
                cells.append(str(obs.get("n_images", 0)))
        print(f"{radar:<8}  " + "  ".join(f"{c:<14}" for c in cells))

    # Mosaic-membership sanity (comp_ref description names 7 radars).
    # If declared-UP but missing from comp_ref's input set → flag.
    composite_members = {"KBBX", "KDAX", "KMUX", "XSCW", "XEBY", "XSCV", "XSCR"}
    print(f"\n--- Mosaic-membership check ---")
    print(f"composite_ref_max is documented to merge: {sorted(composite_members)}")
    declared_radars = set(dec.keys())
    not_in_mosaic = declared_radars - composite_members
    missing_from_status = composite_members - declared_radars
    print(f"  radars in status but NOT documented as mosaic inputs: {sorted(not_in_mosaic)}")
    print(f"  radars documented as mosaic inputs but NOT in status: {sorted(missing_from_status)}")

    # summary
    verdicts = [v for _, _, v in overall.values()]
    healthy = verdicts.count("HEALTHY")
    confirmed_down = verdicts.count("CONFIRMED_DOWN")
    ghost = verdicts.count("GHOST_UP")
    stuck = verdicts.count("STUCK_DOWN_FLAG")
    print(f"\n{healthy} healthy   {confirmed_down} confirmed_down   {ghost} ghost_up   {stuck} stuck_down_flag")
    return 0 if (ghost == 0 and stuck == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
