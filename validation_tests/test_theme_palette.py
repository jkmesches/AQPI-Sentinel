"""The CSU theme's colors: contrast, the band, and the map's copy of the palette.

Run directly (no DB, no network):

    python validation_tests/test_theme_palette.py

Joseph chose theme A (dark) and C (light) on 2026-11-24. Ways the palette goes
wrong, each silent:

  - A token edited below 4.5:1 on a surface it sits on. The standards
    session's ui check of 2026-11-24 found 220 contrast failures, all from
    three tokens whose comments claimed more contrast than they had.
  - A comment claiming a ratio the color does not reach. Those three
    survived review that way.
  - $lib/palette.ts, which the map paints with because MapLibre cannot read
    CSS custom properties, drifting from app.css after a token edit.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "frontend" / "src" / "app.css").read_text()
PALETTE_TS = (ROOT / "frontend" / "src" / "lib" / "palette.ts").read_text()

TEXT = ("default", "muted", "faint", "accent", "ok", "warn", "fail", "error", "info")
SURFACES = ("canvas", "surface", "surface-hi")
BAND_TEXT = ("default", "muted", "faint", "accent", "ok", "warn", "fail", "error", "info")
AA = 4.5
# A token whose comment states a ratio: name, color, ratio, and what it is on.
CLAIM_RE = r"--color-([\w-]+):\s*(#[0-9a-fA-F]{6});\s*/\*\s*([\d.]+):1(?: on ([\w-]+))?"
failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    """Print one assertion and record a failure."""
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def block(selector: str) -> str:
    """The body of the first CSS block opened by a selector.

    Args:
        selector: The text before the block's opening brace.

    Returns:
        Everything between the braces.

    Raises:
        ValueError: If the block is not in app.css.
    """
    start = CSS.find(selector + " {")
    if start < 0:
        raise ValueError(f"no block {selector!r} in app.css")
    open_at = CSS.index("{", start)
    return CSS[open_at + 1:CSS.index("\n}", open_at)]


def tokens(body: str) -> dict[str, str]:
    """The --color-* tokens a block sets, lowercased.

    Args:
        body: A block's text.

    Returns:
        Token name without its prefix, to its hex value.
    """
    return {m[1]: m[2].lower() for m in re.finditer(r"--color-([\w-]+):\s*(#[0-9a-fA-F]{6})", body)}


def lum(h: str) -> float:
    """WCAG relative luminance of a #rrggbb color."""
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two colors."""
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


DARK = tokens(block("@theme"))
LIGHT = {**DARK, **tokens(block('[data-theme="light"]'))}
BAND = tokens(block(".band"))
BAND_BG = DARK["band"]


def test_contrast() -> None:
    """Text and status tokens on each surface, per theme, and on the band."""
    print("contrast:")
    for name, theme in (("dark", DARK), ("light", LIGHT)):
        low = [f"{t} on {s} {contrast(theme[t], theme[s]):.2f}" for t in TEXT for s in SURFACES
               if contrast(theme[t], theme[s]) < AA]
        check(f"every {name} text and status token is {AA}:1 on every surface", not low,
              ", ".join(low))
    low = [f"{t} {contrast(BAND[t], BAND_BG):.2f}" for t in BAND_TEXT
           if contrast(BAND[t], BAND_BG) < AA]
    check(f"every band token is {AA}:1 on the band", not low, ", ".join(low))
    foot_dark = re.search(r"--footer-bg:\s*(#[0-9a-fA-F]{6})", block(':root, [data-theme="dark"]'))
    if foot_dark is None:
        check("the dark footer has a background", False)
        return
    worst = min(contrast(DARK[t], foot_dark[1]) for t in ("faint", "muted", "default"))
    check(f"dark footer text is {AA}:1", worst >= AA, f"{worst:.2f}")
    foot = tokens(block('[data-theme="light"] .site-footer'))
    light_bg = re.search(r"--footer-bg:\s*(#[0-9a-fA-F]{6})", block('[data-theme="light"]'))
    worst = min(contrast(c, light_bg[1]) for c in foot.values()) if light_bg else 0.0
    check(f"light footer text is {AA}:1", worst >= AA, f"{worst:.2f}")


def test_claims() -> None:
    """Each 'N:1' a comment states is a ratio the color reaches."""
    print("\ncomments that state a ratio:")
    claims = 0
    for name, theme, body in (("dark", DARK, block("@theme")),
                              ("light", LIGHT, block('[data-theme="light"]')),
                              ("band", {**BAND, "on": BAND_BG}, block(".band"))):
        for m in re.finditer(CLAIM_RE, body):
            token, hex_, said, on = m[1], m[2].lower(), float(m[3]), m[4]
            where = "band" if name == "band" else (on or "surface-hi")
            bg = BAND_BG if name == "band" else theme.get(where, "")
            got = contrast(hex_, bg)
            claims += 1
            check(f"{name} {token} {said}:1 on {where}", got >= said - 0.05,
                  f"measured {got:.2f}")
    check("the comments state ratios to check", claims >= 15, str(claims))


def test_map_copy() -> None:
    """$lib/palette.ts holds the same values as app.css."""
    print("\nthe map's copy of the palette:")
    for name, theme in (("dark", DARK), ("light", LIGHT)):
        m = re.search(name + r":\s*\{([^}]*)\}", PALETTE_TS)
        if m is None:
            check(f"palette.ts has a {name} table", False)
            continue
        ts = {k: v.lower() for k, v in re.findall(r"(\w+):\s*'(#[0-9a-fA-F]{6})'", m[1])}
        wrong = [f"{k} {v} vs {theme.get(k)}" for k, v in ts.items() if theme.get(k) != v]
        check(f"every {name} value matches app.css", bool(ts) and not wrong, ", ".join(wrong))


def main() -> int:
    """Run every section.

    Returns:
        The process exit status, 1 when any assertion failed.
    """
    test_contrast()
    test_claims()
    test_map_copy()
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall palette assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
