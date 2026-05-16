"""Layer 3 (part B) — filename↔API timestamp parity.

Several products encode the scan time directly in the PNG filename. Parse it
out and compare with steps[*].timestamp from /api/productDetail. Mismatches =
pipeline desync (file renamed without regenerating, symlink to old data, etc.)
This is the cheapest, no-OCR equivalent of "ground-truth timestamp on the
image".

Products without an in-filename timestamp (fcst_* use step indices like
'C_hrrr_accum_step0.png') are flagged as 'unparseable' — for those we rely
on Layer 3 part A (headless-browser screenshot + OCR of the JS overlay) or
trust the API metadata.
"""
from __future__ import annotations
import re, sys
from datetime import datetime, timezone
import requests

from config import BASE, PRODUCTS

# (regex pattern, strptime fmt). Tried in order; first hit wins.
PATTERNS = [
    # comp_ref / water_*: "20260516_0028.png" or "20260516_0100.png"
    (re.compile(r'^(\d{8}_\d{4})\.png$'),                "%Y%m%d_%H%M"),
    # qpe / precip_rate_radar: "..._YYYYMMDD_HHMMSS_rainfall.nc.png"
    (re.compile(r'_(\d{8}_\d{6})_rainfall\.nc\.png$'),   "%Y%m%d_%H%M%S"),
    # X-band: "scwa_CorrReflectivity_20260515-2336.png"
    (re.compile(r'_(\d{8}-\d{4})\.png$'),                "%Y%m%d-%H%M"),
]


def parse_filename_ts(name: str) -> datetime | None:
    for rx, fmt in PATTERNS:
        m = rx.search(name)
        if m:
            try:
                return datetime.strptime(m.group(1), fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def parse_api_ts(s: str) -> datetime:
    s = s.replace("Z", "").split(".")[0]
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def check(name: str, cfg: dict) -> dict:
    r = requests.get(f"{BASE}/api/productDetail",
                     params={"file": cfg["details"]}, timeout=15)
    if r.status_code != 200:
        return {"name": name, "verdict": "API_FAIL", "http": r.status_code}
    d = r.json()
    steps = d.get("steps", [])
    if not steps:
        return {"name": name, "verdict": "NO_STEPS"}

    mismatches, unparseable, matched = [], 0, 0
    for s in steps:
        nm = s["imageName"]
        api_ts = parse_api_ts(s["timestamp"])
        file_ts = parse_filename_ts(nm)
        if file_ts is None:
            unparseable += 1
            continue
        if abs((api_ts - file_ts).total_seconds()) <= 60:
            matched += 1
        else:
            mismatches.append((nm, s["timestamp"], file_ts.isoformat()))

    if unparseable == len(steps):
        verdict = "UNPARSEABLE"     # no time encoded in filename
    elif mismatches:
        verdict = "DESYNC"
    elif matched == 0:
        verdict = "NO_MATCH"
    else:
        verdict = "PARITY_OK"

    out = {"name": name, "verdict": verdict, "n_steps": len(steps),
           "matched": matched, "unparseable": unparseable,
           "mismatch_count": len(mismatches),
           "sample_name": steps[-1]["imageName"]}
    if mismatches:
        out["first_mismatch"] = mismatches[0]
    return out


def main():
    print("="*82)
    print(" LAYER 3 (B) — filename ↔ API timestamp parity")
    print("="*82)
    rows = [check(n, c) for n, c in PRODUCTS.items()]
    print(f"\n{'product':<26} {'verdict':<14} {'matched':>7}/{'total':<5} unparseable mismatches  sample_imageName")
    for r in rows:
        sample = r.get("sample_name", "")
        print(f"{r['name']:<26} {r['verdict']:<14} "
              f"{r.get('matched','-'):>7}/{r.get('n_steps','-'):<5} "
              f"{r.get('unparseable','-'):>11} "
              f"{r.get('mismatch_count','-'):>10}  {sample}")
        if r.get("first_mismatch"):
            nm, api_ts, file_ts = r["first_mismatch"]
            print(f"   ↳ first mismatch: file_ts={file_ts}  api_ts={api_ts}  name={nm}")
    fails = [r for r in rows if r["verdict"] in ("DESYNC", "API_FAIL", "NO_STEPS", "NO_MATCH")]
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
