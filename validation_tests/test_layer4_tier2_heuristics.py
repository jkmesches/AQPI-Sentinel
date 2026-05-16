"""Layer 4 Tier 2 — heuristic pathology detectors.

Per-scan detectors that flag known failure signatures, all implemented in
numpy + PIL only:

  - all_extreme       : fraction of pixels saturated to a single color bin
                        (transmitter or amp saturation)
  - speckle_ratio     : ratio of isolated lit pixels (no lit neighbors) to
                        total lit pixels — broken radars often produce
                        uncorrelated salt-and-pepper noise
  - range_ring_score  : (X-band only, radar disc images) — variance of mean
                        intensity across range bins. Real weather is variable
                        in range; a fault that adds a constant ring shows up
                        as one bin much hotter than its neighbors
  - frozen_frame      : compares the perceptual hash to the previously-saved
                        hash for the same product. If identical (Hamming = 0)
                        across two distinct fetches, the file content hasn't
                        changed — frozen pipeline

State persists across runs in `state/<sample>.json`.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np
from PIL import Image
import imagehash

ROOT = Path(__file__).parent
SAMPLES = ROOT / "samples"
STATE = ROOT / "state"
STATE.mkdir(exist_ok=True)

# Per-sample image kind. Only "xband" gets the range-ring test (circular
# radar disc with a well-defined center).
SAMPLE_KIND = {
    "xband_xscw.png":   "xband",
    "comp_ref.png":     "mosaic",
    "qpe_15min.png":    "mosaic",
    "water_depth.png":  "mosaic",
    "fcst_temp.png":    "field",
}


def detect_all_extreme(arr: np.ndarray) -> dict:
    """Largest single color bin's share of all active pixels. >40% on
    non-trivial coverage suggests saturation."""
    alpha = arr[:, :, 3]
    mask = alpha > 10
    n = int(mask.sum())
    if n == 0:
        return {"fraction": 0.0, "verdict": "EMPTY"}
    rgb = arr[:, :, :3][mask]
    # 8-bin quantize per channel → ~512 bins
    q = (rgb // 32).astype(np.int32)
    keys = q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2]
    counts = np.bincount(keys)
    frac = float(counts.max()) / n
    return {"fraction": round(frac, 4),
            "verdict": "SATURATED" if frac > 0.4 else "OK",
            "n_active": n}


def detect_speckle(arr: np.ndarray) -> dict:
    """Fraction of lit pixels with no lit neighbor in a 3x3 neighborhood.
    High ratio = uncorrelated noise."""
    alpha = arr[:, :, 3]
    mask = (alpha > 10).astype(np.int32)
    n = int(mask.sum())
    if n < 100:
        return {"ratio": None, "verdict": "TOO_SPARSE"}
    # neighbor count by 4-way shift sum (cheap proxy for 3x3 minus self)
    pad = np.pad(mask, 1)
    neigh = (pad[:-2, 1:-1] + pad[2:, 1:-1] + pad[1:-1, :-2] + pad[1:-1, 2:])
    isolated = ((mask == 1) & (neigh == 0)).sum()
    ratio = float(isolated) / n
    return {"ratio": round(ratio, 4),
            "verdict": "SPECKLE" if ratio > 0.35 else "OK"}


def detect_range_ring(arr: np.ndarray) -> dict:
    """X-band only: convert to polar around image center, compute mean
    intensity in each of 50 range bins, then look for outlier rings."""
    alpha = arr[:, :, 3]
    h, w = alpha.shape
    cy, cx = h / 2, w / 2
    # build radius map
    yy, xx = np.indices((h, w))
    rr = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    r_max = min(cy, cx)
    mask = alpha > 10
    if mask.sum() < 500:
        return {"score": None, "verdict": "TOO_SPARSE"}
    intensity = arr[:, :, :3].max(axis=2)
    # 50 range bins from 0 to r_max
    edges = np.linspace(0, r_max, 51)
    means = []
    for i in range(50):
        ring = (rr >= edges[i]) & (rr < edges[i + 1]) & mask
        if ring.sum() > 5:
            means.append(intensity[ring].mean())
        else:
            means.append(np.nan)
    means = np.array(means)
    valid = ~np.isnan(means)
    if valid.sum() < 10:
        return {"score": None, "verdict": "TOO_SPARSE"}
    base = float(np.nanmedian(means))
    devs = means - base
    peak = float(np.nanmax(np.abs(devs)))
    # score = peak / median; high = ringing artifact
    score = peak / (base + 1e-6)
    return {"score": round(score, 3), "peak_dev": round(peak, 1),
            "base_median": round(base, 1),
            "verdict": "RING_PEAK" if score > 0.8 else "OK"}


def detect_frozen(sample_name: str, current_phash: str) -> dict:
    """Compare current perceptual hash to the previously-saved one."""
    state_file = STATE / f"{sample_name}.json"
    prev = None
    if state_file.exists():
        prev = json.loads(state_file.read_text()).get("phash")
    state_file.write_text(json.dumps({"phash": current_phash}))
    if prev is None:
        return {"prev": None, "hamming": None, "verdict": "FIRST_RUN"}
    h_prev = imagehash.hex_to_hash(prev)
    h_cur = imagehash.hex_to_hash(current_phash)
    dist = h_prev - h_cur
    return {"prev": prev, "hamming": int(dist),
            "verdict": "FROZEN" if dist == 0 else "OK"}


def main():
    print("="*82)
    print(" LAYER 4 TIER 2 — heuristic pathology detectors")
    print("="*82)
    samples = sorted(SAMPLES.glob("*.png"))
    if not samples:
        print(f"No samples in {SAMPLES}/")
        return 1
    print(f"\n{'sample':<22} {'kind':<7} {'extreme':<14} {'speckle':<14} {'range_ring':<18} {'frozen':<14}")
    ok = True
    for s in samples:
        im = Image.open(s).convert("RGBA")
        arr = np.asarray(im)
        kind = SAMPLE_KIND.get(s.name, "unknown")
        ext = detect_all_extreme(arr)
        spk = detect_speckle(arr)
        rng = detect_range_ring(arr) if kind == "xband" else {"verdict": "N/A", "score": None}
        phash = str(imagehash.phash(im.convert("L"), hash_size=16))
        frz = detect_frozen(s.name, phash)

        cells = [
            f"{ext['verdict']}({ext.get('fraction','-')})",
            f"{spk['verdict']}({spk.get('ratio','-')})",
            f"{rng['verdict']}({rng.get('score','-')})",
            f"{frz['verdict']}({frz.get('hamming','-')})",
        ]
        print(f"{s.name:<22} {kind:<7} {cells[0]:<14} {cells[1]:<14} {cells[2]:<18} {cells[3]:<14}")
        # validation
        for d, label in ((ext, "extreme"), (spk, "speckle"), (rng, "range"), (frz, "frozen")):
            if d.get("verdict") is None:
                print(f"  FAIL {s.name}: {label} verdict None"); ok = False

    print("\n" + ("PASS: all detectors returned a verdict" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
