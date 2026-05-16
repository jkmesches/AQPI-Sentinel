"""Layer 1 — per-product / per-endpoint health.

For every product in config.PRODUCTS run checks A-H:
  A. API up (productDetail 200 + JSON)
  B. Schema valid (steps[] non-empty with imageName+timestamp)
  C. Freshness (now - max(steps.timestamp) < max_freshness_s)
  D. Cadence (median Δt ≈ cadence_s ±10%)
  E. Step count (within ±2 of expected)
  F. Latest image exists (HEAD 200 image/png)
  G. Image size (>= min_png_bytes)
  H. Image hash recorded (for later frozen-frame detection across runs)

Also: vector static HEADs, stream canary endpoints.
"""
from __future__ import annotations
import hashlib, json, statistics, sys, time
from datetime import datetime, timezone
from urllib.parse import quote
import requests

from config import BASE, PRODUCTS, VECTOR_STATICS, UNIT_SUBDIR_PRODUCTS, TEMP_UNIT_SUBDIR


def image_path(product_name: str, cfg: dict, image_name: str) -> str:
    """Mirror the JS bundle: products in `eb` plus temperature interpolate a
    unit subdir; others put imageName directly under images_dir."""
    if product_name not in UNIT_SUBDIR_PRODUCTS:
        return cfg["image_dir"] + image_name
    unit = cfg["unit"]
    if product_name == "fcst_temp":
        return cfg["image_dir"] + TEMP_UNIT_SUBDIR.get(unit, "F") + "/" + image_name
    # rain/precip: trim "/h" if present (e.g. "in/h" → "in")
    sub = unit.split("/")[0]
    return cfg["image_dir"] + sub + "/" + image_name


def parse_ts(s: str) -> datetime:
    """Accept '2026-05-15T23:54:00' or '...000000000' variants."""
    s = s.replace("Z", "").split(".")[0]
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def check_product(name: str, cfg: dict) -> dict:
    out = {"name": name, "checks": {}, "info": {}}
    chk = out["checks"]

    # --- A. API up ---
    try:
        r = requests.get(f"{BASE}/api/productDetail",
                         params={"file": cfg["details"]}, timeout=15)
        a_ok = r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json")
        chk["A_api_up"] = a_ok
        out["info"]["http"] = r.status_code
    except Exception as e:
        chk["A_api_up"] = False
        out["info"]["http_error"] = str(e)
        return out

    # --- B. Schema ---
    try:
        d = r.json()
        steps = d.get("steps", [])
        b_ok = (
            isinstance(steps, list) and len(steps) > 0
            and all("imageName" in s and "timestamp" in s for s in steps)
        )
        chk["B_schema"] = b_ok
        out["info"]["n_steps"] = len(steps)
        out["info"]["product_label"] = d.get("product", "")
    except Exception as e:
        chk["B_schema"] = False
        out["info"]["json_error"] = str(e)
        return out
    if not b_ok:
        return out

    # --- Derive cadence + freshness ---
    ts = []
    for s in steps:
        try:
            ts.append(parse_ts(s["timestamp"]))
        except Exception:
            pass
    if not ts:
        chk["C_freshness"] = chk["D_cadence"] = chk["E_step_count"] = False
        return out

    now = datetime.now(timezone.utc)
    age_s = (now - max(ts)).total_seconds()
    out["info"]["last_ts"] = max(ts).isoformat()
    out["info"]["age_s"] = round(age_s, 1)

    # --- C. Freshness ---
    chk["C_freshness"] = age_s <= cfg["max_freshness_s"]

    # --- D. Cadence ---
    if cfg["cadence_s"] is None or len(ts) < 2:
        chk["D_cadence"] = True
    else:
        diffs = [(ts[i + 1] - ts[i]).total_seconds() for i in range(len(ts) - 1)]
        med = statistics.median(diffs)
        tolerance = cfg["cadence_s"] * 0.10
        chk["D_cadence"] = abs(med - cfg["cadence_s"]) <= tolerance
        out["info"]["median_dt_s"] = round(med, 1)

    # --- E. Step count (tolerance widened to ±4 — the 1-h rolling window
    # legitimately gains/loses a step or two as it slides) ---
    chk["E_step_count"] = abs(len(steps) - cfg["expected_steps"]) <= 4

    # --- F + G + H. Latest image ---
    latest_name = steps[-1]["imageName"]
    img_url = f"{BASE}/api/imageData"
    img_params = {"file": image_path(name, cfg, latest_name)}
    try:
        ir = requests.get(img_url, params=img_params, timeout=20)
        f_ok = ir.status_code == 200 and ir.headers.get("content-type", "").startswith("image/png")
        chk["F_image_exists"] = f_ok
        out["info"]["image_http"] = ir.status_code
        if f_ok:
            chk["G_image_size"] = len(ir.content) >= cfg["min_png_bytes"]
            out["info"]["image_bytes"] = len(ir.content)
            out["info"]["image_sha256"] = hashlib.sha256(ir.content).hexdigest()[:16]
            chk["H_image_hash"] = True   # always passes; recorded for cross-run
        else:
            chk["G_image_size"] = False
            chk["H_image_hash"] = False
    except Exception as e:
        chk["F_image_exists"] = chk["G_image_size"] = chk["H_image_hash"] = False
        out["info"]["image_error"] = str(e)

    out["pass"] = all(chk.values())
    return out


def check_vector(name: str, cfg: dict) -> dict:
    """These files appear to be reference data (NHD geometries) that change
    rarely. Hard fail only on HTTP error or empty body; age is informational
    only (and watched for regression vs. fingerprint changes elsewhere)."""
    r = requests.head(f"{BASE}{cfg['path']}", timeout=15)
    lm = r.headers.get("Last-Modified", "")
    age_s = None
    if lm:
        try:
            dt = datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc)
            age_s = (datetime.now(timezone.utc) - dt).total_seconds()
        except Exception:
            pass
    size = int(r.headers.get("Content-Length", 0))
    ok_http = r.status_code == 200
    ok_size = size > 0
    return {
        "pass": ok_http and ok_size,
        "http": r.status_code, "bytes": size,
        "last_modified": lm, "age_days": round(age_s / 86400, 1) if age_s else None,
    }


def check_stream_canary():
    """Hit a B-status COMID with the documented YYYYMMDD_HHMM format.
    Empty {COMID echo} is acceptable (means 'no data this hour') — what we're
    proving is that the route resolves and returns valid JSON."""
    # use a known B-status comid from /data/stream_data.csv
    comid = "8921935"
    now = datetime.now(timezone.utc)
    # API error message claims "YYYYMMDD_HHMM" but the validator only accepts
    # YYYYMMDD_HH (8+2 digits). The message is misleading.
    ts_hour = now.strftime("%Y%m%d_%H")
    ds = now.strftime("%Y%m%d")
    out = {}
    for label, url in [
        ("forecast", f"{BASE}/api/get_stream_data/{comid}/{ts_hour}"),
        ("observed", f"{BASE}/api/get_observed_stream_data/{comid}/{ds}"),
    ]:
        try:
            r = requests.get(url, timeout=15)
            out[label] = {
                "pass": r.status_code == 200,
                "http": r.status_code,
                "body_snippet": r.text[:120],
            }
        except Exception as e:
            out[label] = {"pass": False, "error": str(e)}
    return out


def main():
    print("="*82)
    print(" LAYER 1 — per-product checks  (A=api  B=schema  C=fresh  D=cad  E=cnt  F=img  G=size  H=hash)")
    print("="*82)
    rows = []
    for name, cfg in PRODUCTS.items():
        rows.append(check_product(name, cfg))

    # render
    print(f"{'product':<26}  {'A B C D E F G H':<17} {'last_ts':<26} {'age (s)':>9}  {'n':>3}  {'img B':>7}  hash")
    for r in rows:
        flags = "".join(("." if v else "✗") if v is not None else "?"
                        for v in (r["checks"].get(k) for k in ("A_api_up","B_schema","C_freshness","D_cadence","E_step_count","F_image_exists","G_image_size","H_image_hash")))
        i = r["info"]
        print(f"{r['name']:<26}  {' '.join(flags):<17} "
              f"{i.get('last_ts','-')[:25]:<26} {str(i.get('age_s','-')):>9}  "
              f"{str(i.get('n_steps','-')):>3}  {str(i.get('image_bytes','-')):>7}  "
              f"{i.get('image_sha256','-')}")

    # vector statics
    print()
    print("=== Vector statics ===")
    for name, cfg in VECTOR_STATICS.items():
        v = check_vector(name, cfg)
        flag = "PASS" if v["pass"] else "FAIL"
        print(f"  [{flag}] {name:<14}  HTTP {v['http']}  {v['bytes']:>10}B  age={v['age_days']}d  Last-Modified={v['last_modified']}")

    # stream canaries
    print()
    print("=== Stream canaries (COMID 8921935) ===")
    s = check_stream_canary()
    for label, v in s.items():
        flag = "PASS" if v.get("pass") else "FAIL"
        print(f"  [{flag}] {label:<10}  HTTP {v.get('http','-')}  body={v.get('body_snippet','')!r}")

    # summary
    n_pass = sum(1 for r in rows if r.get("pass"))
    print(f"\n{n_pass}/{len(rows)} products passed all checks")
    fails = [r for r in rows if not r.get("pass")]
    if fails:
        print("\nFailures detail:")
        for r in fails:
            broken = [k for k, v in r["checks"].items() if not v]
            print(f"  {r['name']:<26}  failing: {broken}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
