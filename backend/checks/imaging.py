"""Image-processing helpers shared by Layer 4 Tier 1 and Tier 2 checks.

All operate on a raw bytes payload (PNG / JPEG); each returns a dict that
folds straight into a CheckResult's ``payload`` or ``metrics``.

Logic is a direct port of validation_tests/test_layer4_tier{1,2}.py — those
were already validated against real radarca imagery.
"""
from __future__ import annotations
import io
from typing import Any

import imagehash
import numpy as np
from PIL import Image


# --------------------------------------------------------------------------
# Tier 1 — coarse statistics
# --------------------------------------------------------------------------

def tier1_stats(png_bytes: bytes) -> dict[str, Any]:
    im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    arr = np.asarray(im)
    h, w = arr.shape[:2]
    alpha = arr[:, :, 3]
    rgb = arr[:, :, :3].astype(np.int32)

    mask = alpha > 10
    n_active = int(mask.sum())
    coverage_pct = round(100 * n_active / max(h * w, 1), 3)

    if n_active > 0:
        intensity = rgb.max(axis=2)
        v = intensity[mask]
        mean_v = float(v.mean())
        std_v = float(v.std())
        bins = (v // 16).astype(int)
        hist = np.bincount(bins, minlength=16)
        top3_idx = np.argsort(hist)[::-1][:3]
        top3 = [(int(i * 16), int(hist[i])) for i in top3_idx]
    else:
        mean_v = std_v = 0.0
        top3 = []

    # 1-step horizontal autocorrelation of the active-pixel mask
    autocorr = 1.0
    if mask.size > 1:
        m_flat = mask.astype(np.float32)
        a = m_flat[:, :-1].ravel()
        b = m_flat[:, 1:].ravel()
        if a.std() > 0 and b.std() > 0:
            autocorr = float(np.corrcoef(a, b)[0, 1])

    phash = str(imagehash.phash(im.convert("L"), hash_size=16))

    return {
        "size": (w, h),
        "coverage_pct": coverage_pct,
        "n_active_px": n_active,
        "mean_value": round(mean_v, 1),
        "std_value": round(std_v, 1),
        "top3_bins": top3,
        "autocorr": round(autocorr, 4),
        "phash": phash,
    }


# --------------------------------------------------------------------------
# Tier 2 — pathology detectors
# --------------------------------------------------------------------------

def t2_all_extreme(arr: np.ndarray, threshold: float = 0.40) -> dict[str, Any]:
    alpha = arr[:, :, 3]
    mask = alpha > 10
    n = int(mask.sum())
    if n == 0:
        return {"fraction": 0.0, "verdict": "EMPTY"}
    rgb = arr[:, :, :3][mask]
    q = (rgb // 32).astype(np.int32)
    keys = q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2]
    counts = np.bincount(keys)
    frac = float(counts.max()) / n
    return {
        "fraction": round(frac, 4),
        "verdict": "SATURATED" if frac > threshold else "OK",
    }


def t2_speckle(arr: np.ndarray, threshold: float = 0.35) -> dict[str, Any]:
    alpha = arr[:, :, 3]
    mask = (alpha > 10).astype(np.int32)
    n = int(mask.sum())
    if n < 100:
        return {"ratio": None, "verdict": "TOO_SPARSE"}
    pad = np.pad(mask, 1)
    neigh = pad[:-2, 1:-1] + pad[2:, 1:-1] + pad[1:-1, :-2] + pad[1:-1, 2:]
    isolated = int(((mask == 1) & (neigh == 0)).sum())
    ratio = isolated / n
    return {
        "ratio": round(ratio, 4),
        "verdict": "SPECKLE" if ratio > threshold else "OK",
    }


def t2_range_ring(arr: np.ndarray, threshold: float = 0.80) -> dict[str, Any]:
    """X-band radar disc only: polar-bin intensity & flag outlier rings."""
    alpha = arr[:, :, 3]
    h, w = alpha.shape
    cy, cx = h / 2, w / 2
    yy, xx = np.indices((h, w))
    rr = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    r_max = min(cy, cx)
    mask = alpha > 10
    if mask.sum() < 500:
        return {"score": None, "verdict": "TOO_SPARSE"}
    intensity = arr[:, :, :3].max(axis=2)
    edges = np.linspace(0, r_max, 51)
    means = []
    for i in range(50):
        ring = (rr >= edges[i]) & (rr < edges[i + 1]) & mask
        if ring.sum() > 5:
            means.append(intensity[ring].mean())
        else:
            means.append(np.nan)
    means = np.array(means)
    if (~np.isnan(means)).sum() < 10:
        return {"score": None, "verdict": "TOO_SPARSE"}
    base = float(np.nanmedian(means))
    peak = float(np.nanmax(np.abs(means - base)))
    score = peak / (base + 1e-6)
    return {
        "score": round(score, 3),
        "peak_dev": round(peak, 1),
        "base_median": round(base, 1),
        "verdict": "RING_PEAK" if score > threshold else "OK",
    }


def tier2_heuristics(
    png_bytes: bytes,
    kind: str = "mosaic",
    extreme_threshold: float = 0.40,
) -> dict[str, Any]:
    """Combined T2 sub-detectors. ``kind`` ∈ {"xband", "mosaic"}; range_ring
    only runs for ``xband`` (circular disc images).

    ``extreme_threshold`` is the max single-color-bin fraction before we call
    a frame SATURATED. The default (0.40) suits radar imagery where a
    near-monochrome scene is genuinely anomalous; forecast products that
    encode scalar fields with thresholded color ramps (water_depth, etc.)
    routinely exceed 0.40 in normal operation and should pass a much
    higher threshold from the caller's product profile."""
    im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    arr = np.asarray(im)
    out = {
        "extreme": t2_all_extreme(arr, threshold=extreme_threshold),
        "speckle": t2_speckle(arr),
    }
    out["range_ring"] = t2_range_ring(arr) if kind == "xband" else {"verdict": "N/A"}
    return out
