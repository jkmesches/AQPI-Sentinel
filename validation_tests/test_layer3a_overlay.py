"""Layer 3 (part A) — JS-rendered overlay timestamp parity (Playwright).

The PNG files have no embedded text — the timestamp the user sees is rendered
into the DOM by the dashboard. We need a headless browser to read it.

STATUS: FEASIBILITY-VALIDATED, CLICK SELECTOR NEEDS HARDENING.

The overlay-text extraction itself works perfectly — for the default-loaded
product, the regex reliably pulls "DAY HH:MM:SS Z DD-MMM-YYYY" and we can
compare to the API. The current product-switch click strategy
(page.get_by_text(label, exact=True)) hits *some* text node in the DOM but
does not always reach the Material Tailwind ListItem that fires the state
change; on most products the page remains on the default product. Production
fixes to try:
  - page.locator('[role="button"]', has_text=label) or a parent .ListItem CSS
    selector
  - locator(label).locator('..').click()  — click the wrapping element
  - direct JS state poke via page.evaluate to bypass the click chain entirely
"""
from __future__ import annotations
import re, sys
from datetime import datetime, timezone
from pathlib import Path
import requests
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

from config import BASE, PRODUCTS

# Products in the JS bundle's `eC` array show step[0] (initial time) when first
# selected; others show step[-1] (latest). Mirrors the dashboard exactly.
INITIAL_STEP_PRODUCTS = {
    "comp_now",
    "fcst_total_precip", "fcst_total_precip_cum",
    "fcst_precip_rate", "fcst_temp",
    "water_level", "water_depth",
    "max_water_level", "max_water_depth",
}

OUT = Path(__file__).parent / "screenshots"
OUT.mkdir(exist_ok=True)

# Accordion labels (visible in the left panel) → product id we'll cross-check.
# Note: there are two "Precip Rate" entries (radar + forecast); we use the
# first under "Radar Data" and the second under "Atmospheric Forecast" — the
# .nth() call disambiguates.
PROBE_PRODUCTS = [
    ("Total Precip, 15 minute QPE",  "qpe_15min",          0),
    ("Total Precip, 1 hour QPE",     "qpe_1hr",            0),
    ("Reflectivity",                 "comp_ref",           0),
    ("Reflectivity Nowcast",         "comp_now",           0),
    ("Hourly Accumulation",          "fcst_total_precip",  0),
    ("Total Precipitation",          "fcst_total_precip_cum", 0),
    ("Temperature",                  "fcst_temp",          0),
    ("Water Level",                  "water_level",        0),
    ("Water Depth",                  "water_depth",        0),
    ("Max Water Level",              "max_water_level",    0),
    ("Max Water Depth",              "max_water_depth",    0),
]

# Overlay format: "TUE 21:00:00 Z 05-MAY-2026"
OVERLAY_RE = re.compile(
    r'(?P<day>MON|TUE|WED|THU|FRI|SAT|SUN)\s+'
    r'(?P<time>\d{2}:\d{2}:\d{2})\s*Z\s+'
    r'(?P<date>\d{2}-(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)-\d{4})',
    re.IGNORECASE,
)


def fetch_api_overlay(product_id: str) -> dict:
    cfg = PRODUCTS[product_id]
    r = requests.get(f"{BASE}/api/productDetail",
                     params={"file": cfg["details"]}, timeout=15)
    r.raise_for_status()
    d = r.json()
    # Mirror the JS: forecasts/nowcast/water show step[0], others show step[-1].
    idx = 0 if product_id in INITIAL_STEP_PRODUCTS else -1
    s = d["steps"][idx]
    return {
        "day":  s.get("day",  "").upper()[:3],
        "date": s.get("date", "").upper(),
        "time": s.get("time", ""),
        "timestamp": s.get("timestamp", ""),
        "product": d.get("product", ""),
        "step_idx_compared": idx,
    }


def read_dashboard_overlay(page: Page, product_label: str, nth: int = 0) -> dict:
    """Click the product entry and return the rendered overlay timestamp."""
    locator = page.get_by_text(product_label, exact=True).nth(nth)
    locator.scroll_into_view_if_needed()
    locator.click(force=True)
    page.wait_for_timeout(3500)   # generous wait for productDetail + image refresh

    body = page.inner_text("body")
    m = OVERLAY_RE.search(body)
    # Heuristic: look for the product's expected uppercase label too, so we can
    # tell whether the click actually switched products.
    label_present = product_label.upper() in body.upper() and (
        any(token in body.upper() for token in
            ("HOURLY ACCUMULATION", "TOTAL PRECIPITATION", "PRECIPITATION RATE",
             "REFLECTIVITY", "REFLECTIVITY NOWCAST", "TEMPERATURE",
             "WATER LEVEL", "WATER DEPTH", "MAX WATER LEVEL", "MAX WATER DEPTH",
             "1 HR ACCUMULATION", "15 MIN ACCUMULATION"))
    )
    return {
        "found": bool(m),
        "raw": m.group(0) if m else "",
        "day":  (m.group("day").upper() if m else ""),
        "time": (m.group("time")        if m else ""),
        "date": (m.group("date").upper() if m else ""),
        "label_present": label_present,
    }


def compare(api: dict, ui: dict) -> str:
    if not ui["found"]:
        return "NO_OVERLAY_FOUND"
    ok_day  = api["day"]  == ui["day"]
    ok_date = api["date"] == ui["date"]
    ok_time = api["time"] == ui["time"]
    if ok_day and ok_date and ok_time:
        return "MATCH"
    if (ok_date and ok_day) or (ok_date and ok_time):
        return "PARTIAL"
    return "MISMATCH"


def main():
    print("="*82)
    print(" LAYER 3 (A) — JS-rendered overlay vs API timestamp parity")
    print("="*82)
    rows = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
        ctx = b.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        page.goto(f"{BASE}/public", timeout=30000, wait_until="networkidle")
        page.wait_for_timeout(3000)

        # All accordion sections render expanded on initial load — DO NOT
        # click them to "expand", that toggles them closed.

        for label, pid, nth in PROBE_PRODUCTS:
            try:
                ui = read_dashboard_overlay(page, label, nth)
            except Exception as e:
                ui = {"found": False, "raw": f"err: {type(e).__name__}: {e}"}
            try:
                api = fetch_api_overlay(pid)
            except Exception as e:
                api = {"error": str(e), "day": "", "date": "", "time": ""}

            verdict = compare(api, ui)
            rows.append((label, pid, api, ui, verdict))
            # capture an evidence screenshot
            try:
                page.screenshot(path=str(OUT / f"{pid}.png"), full_page=False)
            except Exception:
                pass
        b.close()

    # render results
    print(f"{'product':<28} {'verdict':<18} {'api day/date/time':<32} {'ui day/date/time':<32}")
    for label, pid, api, ui, v in rows:
        api_str = f"{api.get('day','-')} {api.get('date','-')} {api.get('time','-')}"
        ui_str  = f"{ui.get('day','-')} {ui.get('date','-')} {ui.get('time','-')}"
        print(f"{pid:<28} {v:<18} {api_str:<32} {ui_str:<32}")
    n_mismatch = sum(1 for r in rows if r[4] not in ("MATCH", "PARTIAL"))
    n_mismatch_strict = sum(1 for r in rows if r[4] != "MATCH")
    print(f"\n{len(rows) - n_mismatch}/{len(rows)} have any overlay; "
          f"{len(rows) - n_mismatch_strict}/{len(rows)} match strictly.")
    print(f"screenshots in {OUT}")
    return 0 if n_mismatch == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
