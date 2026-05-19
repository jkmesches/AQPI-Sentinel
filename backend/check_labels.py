"""Plain-English names for check_ids.

Mirrors frontend/src/lib/format.ts's `prettyCheckLabel` so emails, push
payloads, scheduler demote summaries, and the desktop / mobile UI all
use the same vocabulary. Kept manually in sync — if you add or rename
a product or radar, edit both surfaces.

Pure-string, no DB lookups. Designed to be safe to import from any
check module without circular-import risk (no dependency on registry).
"""
from __future__ import annotations

_PRODUCTS: dict[str, str] = {
    "comp_ref":              "Composite Reflectivity",
    "comp_now":              "Composite Nowcast",
    "qpe_15min":             "QPE 15-min",
    "qpe_1hr":               "QPE 1-hour",
    "precip_rate_radar":     "Precipitation Rate",
    "fcst_total_precip":     "Forecast — Total Precipitation",
    "fcst_total_precip_cum": "Forecast — Cumulative Precipitation",
    "fcst_precip_rate":      "Forecast — Precip Rate",
    "fcst_temp":             "Forecast — Temperature",
    "water_level":           "Water Level",
    "water_depth":           "Water Depth",
    "max_water_level":       "Max Water Level",
    "max_water_depth":       "Max Water Depth",
}

_L0_TARGETS: dict[str, str] = {
    "tls_cert":               "TLS certificate",
    "origin_alive":           "Origin reachable",
    "public":                 "Public dashboard page",
    "root_notfound":          "Root URL (404 check)",
    "website_public":         "Public dashboard page",
    "website_root_notfound":  "Root URL (404 check)",
    "internet":               "Sentinel Internet",
    "dns":                    "Sentinel DNS",
}

_VECTORS: dict[str, str] = {
    "flowlines":   "Stream flowlines",
    "watersheds":  "Watersheds",
    "stations":    "Stations",
}

_STREAMS: dict[str, str] = {
    "stream":      "Stream gauges (live)",
    "stream_csv":  "Stream gauges (CSV)",
}


def _title(s: str) -> str:
    return s.replace("_", " ").title() if s else ""


def _product(target: str) -> str:
    return _PRODUCTS.get(target) or _title(target)


def pretty_check_label(check_id: str | None, target: str = "") -> str:
    """Return a friendly name for a (check_id, target) pair. Falls back
    to a cleaned-up version of the target when no specific match is
    known, and to the check_id when target is also empty."""
    cid = check_id or ""
    t = target or ""
    if cid.startswith("layer0.tls."):           return "TLS certificate"
    if cid.startswith("layer0.origin."):        return "Origin reachable"
    if cid.startswith("layer0.website.public"): return "Public dashboard page"
    if cid.startswith("layer0.website.root"):   return "Root URL (404 check)"
    if cid.startswith("layer0.net.internet"):   return "Sentinel Internet"
    if cid.startswith("layer0.net.dns"):        return "Sentinel DNS"
    if cid.startswith("layer0."):               return _L0_TARGETS.get(t) or _title(t)
    if cid.startswith("layer1.product."):       return _product(t)
    if cid.startswith("layer1.stream."):        return "Stream Reach canary"
    if cid.startswith("layer1.vector."):        return _VECTORS.get(t) or _title(t)
    if cid.startswith("layer2.radar."):         return t or cid
    if cid.startswith("layer3."):               return f"Overlay reconcile — {_product(t)}" if t else "Overlay reconcile"
    if cid.startswith("layer4.xband."):         return t or cid
    if cid.startswith("layer4."):               return _product(t)
    return _title(t) if t else cid
