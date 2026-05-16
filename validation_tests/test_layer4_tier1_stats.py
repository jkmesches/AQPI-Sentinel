"""Layer 4 Tier 1 — coarse statistical anomaly detection.

Per scan / per radar, compute cheap statistics that distinguish "healthy"
imagery from gross failures (transmitter off, all-saturation, noise flood,
frozen frame). These metrics are meant to be fed into a 7-day rolling
baseline; here we just demonstrate the computation on real samples.

Metrics:
  - coverage        : % of pixels with any non-transparent return (alpha > 0)
  - n_active_px     : same, raw count
  - mean_value      : average over the colormap-quantized intensity (proxy for
                      mean dBZ once the colormap inverse is wired)
  - std_value       : std-dev over same
  - histogram_top   : top-3 quantized intensity bins by frequency
  - spatial_autocorr: 1-step autocorrelation of valid-pixel mask
                      (proxy for "is the signal spatially clustered?")
  - phash           : 16-byte perceptual hash (for cross-run frozen detection)
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
from PIL import Image
import imagehash

SAMPLES = Path(__file__).parent / "samples"


def compute_stats(img_path: Path) -> dict:
    im = Image.open(img_path).convert("RGBA")
    arr = np.asarray(im)
    h, w = arr.shape[:2]
    alpha = arr[:, :, 3]
    rgb = arr[:, :, :3].astype(np.int32)

    # active = non-transparent
    mask = alpha > 10
    n_active = int(mask.sum())
    coverage = n_active / (h * w)

    if n_active > 0:
        # Single-channel intensity proxy: max(R, G, B) on active pixels —
        # for radar/precip products this captures the colormap value crudely.
        intensity = rgb.max(axis=2)
        v = intensity[mask]
        mean_v = float(v.mean())
        std_v = float(v.std())
        # Quantize into 16 bins for a discrete histogram.
        bins = (v // 16).astype(int)
        hist = np.bincount(bins, minlength=16)
        top3_idx = np.argsort(hist)[::-1][:3]
        top3 = [(int(i * 16), int(hist[i])) for i in top3_idx]
    else:
        mean_v = std_v = 0.0
        top3 = []

    # 1-step horizontal autocorrelation of the binary active mask: real
    # weather is spatially clustered (high), salt-and-pepper noise is low.
    if mask.size > 1:
        m_flat = mask.astype(np.float32)
        # Pearson r between mask and its 1-px right-shifted self
        a = m_flat[:, :-1].ravel()
        b = m_flat[:, 1:].ravel()
        if a.std() > 0 and b.std() > 0:
            autocorr = float(np.corrcoef(a, b)[0, 1])
        else:
            autocorr = 1.0   # uniform mask, perfectly autocorrelated
    else:
        autocorr = 0.0

    # Perceptual hash for cross-run change detection
    phash = str(imagehash.phash(im.convert("L"), hash_size=16))

    return {
        "size": (w, h),
        "coverage_pct": round(coverage * 100, 3),
        "n_active_px": n_active,
        "mean_value": round(mean_v, 1),
        "std_value":  round(std_v, 1),
        "top3_bins":  top3,
        "autocorr":   round(autocorr, 4),
        "phash":      phash,
    }


def main():
    print("="*82)
    print(" LAYER 4 TIER 1 — coarse statistical anomaly metrics")
    print("="*82)
    samples = sorted(SAMPLES.glob("*.png"))
    if not samples:
        print(f"No sample PNGs in {SAMPLES}/ — run test_layer1_endpoints.py first")
        return 1
    print(f"\n{'sample':<22} {'size':<12} {'coverage':>10} {'active':>10} {'mean':>7} {'std':>7} {'autocorr':>9}  top3_bins")
    rows = []
    for s in samples:
        st = compute_stats(s)
        rows.append((s.name, st))
        size = f"{st['size'][0]}x{st['size'][1]}"
        top3 = ",".join(f"{b}:{c}" for b, c in st["top3_bins"])
        print(f"{s.name:<22} {size:<12} {st['coverage_pct']:>9}% {st['n_active_px']:>10} "
              f"{st['mean_value']:>7} {st['std_value']:>7} {st['autocorr']:>9}  {top3}")

    # Smoke validation: every metric should be finite & in expected range.
    print("\n--- validation ---")
    ok = True
    for name, st in rows:
        if not (0 <= st["coverage_pct"] <= 100):
            print(f"  FAIL {name}: coverage out of range"); ok = False
        if not (-1.0 <= st["autocorr"] <= 1.0):
            print(f"  FAIL {name}: autocorr out of range"); ok = False
        if len(st["phash"]) != 64:   # hash_size=16 -> 16*16=256 bits = 64 hex
            print(f"  FAIL {name}: phash length unexpected ({len(st['phash'])})"); ok = False
    if ok:
        print("  PASS: all metrics finite and in range")

    # Sanity comparison: which is the highest-coverage product?
    print("\n--- highest-coverage products (most data) ---")
    for n, s in sorted(rows, key=lambda r: -r[1]["coverage_pct"])[:5]:
        print(f"  {n:<22} coverage={s['coverage_pct']}%  autocorr={s['autocorr']}")
    print("\n--- lowest-coverage (sparsest scans / mostly empty) ---")
    for n, s in sorted(rows, key=lambda r: r[1]["coverage_pct"])[:3]:
        print(f"  {n:<22} coverage={s['coverage_pct']}%  autocorr={s['autocorr']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
