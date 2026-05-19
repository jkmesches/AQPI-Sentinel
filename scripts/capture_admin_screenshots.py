"""Capture screenshots of every admin page for the documentation site.

Why this exists: docs/03-administration.md walks through each admin
surface with inline screenshots. Maintaining those screenshots
manually breaks every time the UI changes. This script automates the
capture so a refresh is one command.

Usage:
    pip install playwright
    playwright install chromium

    SENTINEL_URL=https://your-deploy.example.com \\
    SENTINEL_ADMIN_EMAIL=you@example.com \\
    SENTINEL_ADMIN_PASSWORD='...' \\
        python scripts/capture_admin_screenshots.py

    # Or interactively (will prompt for missing values):
    python scripts/capture_admin_screenshots.py

Captures land in docs/images/ as `admin-<slug>.png`. The doc
references those filenames directly.

Tips:
- Run against a deploy that has SOME state (a few groups, a few
  silences, a non-empty audit log) so the captured pages aren't
  empty placeholders. The script doesn't seed data.
- Re-run after any non-trivial UI change. Diff `docs/images/*.png`
  to spot regressions.
- Auth happens via POST /api/auth/login → Bearer token in
  localStorage. The same flow the dashboard uses; no special
  test-mode hooks required.
"""
from __future__ import annotations

import asyncio
import getpass
import os
import sys
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Playwright not installed. Run:", file=sys.stderr)
    print("    pip install playwright && playwright install chromium", file=sys.stderr)
    sys.exit(1)


# Output directory (relative to repo root, where the script lives in scripts/).
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE.parent / "docs" / "images"


# Each entry: (slug, path, optional wait-for selector, optional filename).
# When filename is None the file is written as `admin-{slug}.png`; pass a
# string to override (used for the public views that don't belong under
# the admin- prefix — they're linked from the README).
PAGES: list[tuple[str, str, str | None, str | None]] = [
    ("login",        "/login",              "form input[name='email']", None),
    # Public views — referenced by README; keep filenames matching.
    ("home",         "/",                   ".panel",                   "live-dashboard.png"),
    ("timeline",     "/timeline",           None,                        "timeline.png"),
    ("history",      "/history",            None,                        "history.png"),
    # Admin views.
    ("users",        "/admin/users",        None, None),
    ("email",        "/admin/email",        None, None),
    ("groups",       "/admin/groups",       None, None),
    ("alerts",       "/admin/alerts",       None, None),
    ("thresholds",   "/admin/thresholds",   None, None),
    ("silences",     "/admin/silences",     None, None),
    ("audit",        "/admin/audit",        None, None),
    ("devices",      "/settings/devices",   None, None),
    # Mobile shell — viewport switched to phone-sized before capture.
    ("m-status",     "/m",                  None, None),
    ("m-timeline",   "/m/timeline",         None, None),
    ("m-uptime",     "/m/uptime",           None, None),
    ("m-alarms",     "/m/alarms",           None, None),
    ("m-history",    "/m/history",          None, None),
    ("m-more",       "/m/more",             None, None),
]
MOBILE_SLUG_PREFIX = "m-"


def _prompt(name: str, *, secret: bool = False, default: str = "") -> str:
    """Pull from env, fall back to prompt. Empty values aren't acceptable."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    while True:
        prompt = f"{name}{(' [' + default + ']') if default else ''}: "
        val = (getpass.getpass(prompt) if secret else input(prompt)).strip() or default
        if val:
            return val
        print("  (required)")


async def login(page, base_url: str, email: str, password: str) -> str:
    """POST credentials to /api/auth/login, stash the returned token in
    localStorage under the key the frontend reads. Return the token."""
    resp = await page.request.post(
        f"{base_url.rstrip('/')}/api/auth/login",
        data={"email": email, "password": password},
    )
    if not resp.ok:
        body = await resp.text()
        raise SystemExit(
            f"login failed: HTTP {resp.status} — {body[:200]}"
        )
    payload = await resp.json()
    token = payload["token"]
    # Seed localStorage AND the cookie. Sentinel's frontend prefers the
    # Bearer header from localStorage but falls back to the session
    # cookie; covering both makes the script robust to either path.
    await page.context.add_cookies([
        {
            "name":     "sentinel-session",
            "value":    token,
            "url":      base_url,
            "httpOnly": True,
            "sameSite": "Lax",
        }
    ])
    # Frontend reads the token from localStorage under the literal key
    # `sentinel.token` (see frontend/src/lib/origin.ts + auth.svelte.ts).
    # The dot matters — `sentinel-token` would silently fail and every
    # admin page would render the "ADMIN ONLY · sign in" stub.
    await page.goto(f"{base_url.rstrip('/')}/login")
    await page.evaluate(
        "(t) => window.localStorage.setItem('sentinel.token', t)",
        token,
    )
    return token


async def capture(base_url: str, email: str, password: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()

        # Desktop context — broad enough to capture full admin tables
        # without horizontal scroll.
        desktop = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=2,
        )
        page = await desktop.new_page()
        await login(page, base_url, email, password)

        # Mobile context — iPhone-ish. Touch + small viewport so /m
        # routes render in their actual deployment shape.
        mobile = await browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            ),
        )
        m_page = await mobile.new_page()
        await login(m_page, base_url, email, password)

        for slug, path, wait_for, filename in PAGES:
            is_mobile = slug.startswith(MOBILE_SLUG_PREFIX) or slug == "login"
            target = m_page if is_mobile else page
            url = f"{base_url.rstrip('/')}{path}"
            print(f"  → {slug:14}  {url}")
            try:
                await target.goto(url, wait_until="networkidle", timeout=20_000)
            except Exception as e:
                print(f"     ! navigation failed: {e}")
                continue
            if wait_for:
                try:
                    await target.wait_for_selector(wait_for, timeout=5_000)
                except Exception:
                    pass
            # Brief settle so live data finishes rendering.
            await target.wait_for_timeout(800)
            out = OUT_DIR / (filename or f"admin-{slug}.png")
            # Full-page for desktop (admin tables can be tall and benefit
            # from showing the whole layout). For mobile we use the
            # viewport-only screenshot — mobile timelines and history
            # lists scroll forever, and a full_page capture of /m/timeline
            # came in at 1170 × 127,479 px (~11 MB). The viewport
            # screenshot is what an iPhone user actually sees.
            await target.screenshot(path=str(out), full_page=not is_mobile)

        await browser.close()

    print(f"\n{len(PAGES)} pages captured → {OUT_DIR.relative_to(HERE.parent)}")


def main() -> int:
    base_url = _prompt("SENTINEL_URL", default="http://localhost:3000")
    email    = _prompt("SENTINEL_ADMIN_EMAIL")
    password = _prompt("SENTINEL_ADMIN_PASSWORD", secret=True)
    print(f"\nCapturing screenshots from {base_url} as {email}...\n")
    try:
        asyncio.run(capture(base_url, email, password))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
