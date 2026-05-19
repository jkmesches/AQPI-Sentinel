"""Layer 3 (A) — JS-rendered overlay parity (Playwright).

Reads the date/time pill the dashboard *renders* on the active product and
compares to /api/productDetail. The PNG files themselves contain no text;
this is the only way to know what end-users actually see.

We probe the DEFAULT-loaded product (fcst_total_precip on this dashboard).
Switching products mid-session requires a robust click selector that we're
deferring — the validation work showed Material Tailwind ListItems are
flaky to click without bespoke locators.

Cadence is 5 min — Chromium spin-up + page load is ~5 s, so this is heavier
than other checks. Shadow status: ``skip`` if parity matches (most of the
time) so the dashboard isn't dominated by passing-yet-noisy Layer 3A rows.
"""
from __future__ import annotations
import re

from ..config import PRODUCTS, SETTINGS
from ..errors import humanize_error
from ..registry import register
from .base import Check, CheckResult, utcnow
from .helpers import parse_api_ts

DEFAULT_PRODUCT = "fcst_total_precip"
OVERLAY_RE = re.compile(
    r"(?P<day>MON|TUE|WED|THU|FRI|SAT|SUN)\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s*Z\s+"
    r"(?P<date>\d{2}-(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)-\d{4})",
    re.IGNORECASE,
)


@register
class Layer3OverlayCheck(Check):
    id = "layer3.overlay.default_product"
    stage = "L3"
    target = DEFAULT_PRODUCT
    cadence_s = 300
    depends_on = ["layer0.website.public", f"layer1.product.{DEFAULT_PRODUCT}"]

    async def run(self, ctx) -> CheckResult:
        t0 = utcnow()
        page = await ctx.browser.new_page()
        try:
            await page.goto(f"{SETTINGS.base}/public", timeout=20000, wait_until="networkidle")
            await page.wait_for_timeout(2500)
            body = await page.inner_text("body")
        except Exception as e:
            try:
                await page.close()
            except Exception:
                pass
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="error",
                started_at=t0, finished_at=utcnow(),
                summary=f"Page load failed: {humanize_error(e)}",
                payload={"error": str(e)},
            )
        finally:
            try:
                await page.close()
            except Exception:
                pass

        m = OVERLAY_RE.search(body)
        if not m:
            return CheckResult(
                check_id=self.id, target=self.target, stage=self.stage,
                status="warn",
                started_at=t0, finished_at=utcnow(),
                summary="overlay not found in rendered DOM",
                payload={},
            )

        ui_day, ui_time, ui_date = (
            m.group("day").upper(), m.group("time"), m.group("date").upper()
        )

        # Compare to API. The default product is in eC (initial-step group),
        # so we use steps[0] not steps[-1].
        cfg = PRODUCTS[self.target]
        r = await ctx.http.get(
            f"{SETTINGS.base}/api/productDetail", params={"file": cfg["details"]},
        )
        api_day = api_date = api_time = ""
        if r.status_code == 200:
            steps = r.json().get("steps", [])
            if steps:
                s = steps[0]
                api_day  = (s.get("day", "")  or "").upper()[:3]
                api_date = (s.get("date", "") or "").upper()
                api_time = s.get("time", "") or ""

        # Verdict — match / partial / mismatch / no-api
        if not api_day:
            verdict, status = "NO_API", "warn"
        elif (api_day == ui_day and api_date == ui_date and api_time == ui_time):
            verdict, status = "MATCH", "pass"
        else:
            verdict, status = "MISMATCH", "warn"

        return CheckResult(
            check_id=self.id, target=self.target, stage=self.stage,
            status=status,
            started_at=t0, finished_at=utcnow(),
            summary=f"verdict={verdict}  ui={ui_day} {ui_date} {ui_time}  api={api_day} {api_date} {api_time}",
            payload={
                "verdict": verdict,
                "ui":  {"day": ui_day,  "date": ui_date,  "time": ui_time},
                "api": {"day": api_day, "date": api_date, "time": api_time},
                "raw": m.group(0),
            },
        )
