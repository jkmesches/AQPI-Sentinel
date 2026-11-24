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
  - A literal color in a scoped block (.band, the light footer). The ui
    check's palette is the tokens defined at the root, so a literal there
    reads as an off-palette color on every page.
  - A text field drawn with an edge under 3:1 (WCAG 1.4.11). Fields sat at
    1.2 to 1.4:1 until 2026-11-24, because the global border rule sat outside
    any CSS layer and beat every border color class in the markup.
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
NON_TEXT = 3.0
SRC = ROOT / "frontend" / "src"
CONTROL_RE = r"<(input|select|textarea)\b"
BORDER_CLASS_RE = r"(?<![\w-])border(?![\w-])"
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


def resolved(body: str, theme: dict[str, str]) -> dict[str, str]:
    """The --color-* tokens a block points at other tokens, resolved in a theme.

    Args:
        body: A block's text.
        theme: Token name to hex value, for the var() references to resolve in.

    Returns:
        Token name without its prefix, to the hex value its reference resolves to.

    Raises:
        KeyError: If a reference names a token the theme does not define.
    """
    refs = re.finditer(r"--color-([\w-]+):\s*var\(--color-([\w-]+)\)", body)
    return {m[1]: theme[m[2]] for m in refs}


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
BAND = resolved(block(".band"), DARK)
BAND_BG = DARK["band"]
SCOPED = (".band", '[data-theme="light"] .site-footer')
LITERAL_TEXT_RE = r"--color-(?:default|bright|muted|faint|accent|ok|warn|fail|error|info):\s*#"


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
    foot_dark = re.search(r"--color-footer:\s*(#[0-9a-fA-F]{6})",
                          block(':root, [data-theme="dark"]'))
    if foot_dark is None:
        check("the dark footer has a background", False)
        return
    worst = min(contrast(DARK[t], foot_dark[1]) for t in ("faint", "muted", "default"))
    check(f"dark footer text is {AA}:1", worst >= AA, f"{worst:.2f}")
    foot = resolved(block('[data-theme="light"] .site-footer'), LIGHT)
    light_bg = re.search(r"--color-footer:\s*(#[0-9a-fA-F]{6})", block('[data-theme="light"]'))
    worst = min(contrast(c, light_bg[1]) for c in foot.values()) if light_bg and foot else 0.0
    check(f"light footer text is {AA}:1", worst >= AA, f"{worst:.2f}")


def test_scoped_blocks() -> None:
    """The band and the light footer set their text colors from root tokens."""
    print("\nscoped blocks:")
    for selector in SCOPED:
        literal = re.findall(LITERAL_TEXT_RE, block(selector))
        check(f"{selector} sets no text color as a literal", not literal, ", ".join(literal))
    check("the band resolves every text token", set(BAND_TEXT) <= set(BAND),
          ", ".join(sorted(set(BAND_TEXT) - set(BAND))))


def test_claims() -> None:
    """Each 'N:1' a comment states is a ratio the color reaches."""
    print("\ncomments that state a ratio:")
    claims = 0
    for name, theme, body in (("dark", DARK, block("@theme")),
                              ("light", LIGHT, block('[data-theme="light"]'))):
        for m in re.finditer(CLAIM_RE, body):
            token, hex_, said, on = m[1], m[2].lower(), float(m[3]), m[4]
            where = on or "surface-hi"
            if where not in theme:
                check(f"{name} {token} names a surface app.css defines", False, where)
                continue
            got = contrast(hex_, theme[where])
            claims += 1
            check(f"{name} {token} {said}:1 on {where}", got >= said - 0.05,
                  f"measured {got:.2f}")
    check("the comments state ratios to check", claims >= 15, str(claims))


def tag_text(source: str, start: int) -> str:
    """The text of the tag opening at an index, through its closing '>'.

    Args:
        source: A Svelte file's text.
        start: The index of the tag's '<'.

    Returns:
        The tag, skipping any '>' inside quotes or {expressions}.

    Raises:
        ValueError: If the tag never closes.
    """
    depth, quote = 0, ""
    for i in range(start + 1, len(source)):
        c = source[i]
        if quote:
            quote = "" if c == quote else quote
        elif c in "\"'" and depth == 0:
            quote = c
        elif c in "{}":
            depth += 1 if c == "{" else -1
        elif c == ">" and depth == 0:
            return source[start:i + 1]
    raise ValueError(f"tag at {start} never closes")


def test_controls() -> None:
    """Text fields, menus and text areas have a 3:1 edge, and the markup applies it."""
    print("\ncontrol edges:")
    for name, theme in (("dark", DARK), ("light", LIGHT)):
        low = [f"{s} {contrast(theme['border-control'], theme[s]):.2f}" for s in SURFACES
               if contrast(theme["border-control"], theme[s]) < NON_TEXT]
        check(f"{name} border-control is {NON_TEXT}:1 on every surface", not low, ", ".join(low))
    layered = re.search(r"@layer base \{\s*\*, ::before, ::after \{ border-color:", CSS)
    check("the global border color sits in the base layer", layered is not None)
    unlayered = re.findall(r"^\*[^{]*\{[^}]*border-color", CSS, re.M)
    check("no unlayered rule sets border-color on every element", not unlayered,
          ", ".join(unlayered))
    bordered, off = 0, []
    for path in sorted(SRC.rglob("*.svelte")):
        source = path.read_text()
        for m in re.finditer(CONTROL_RE, source):
            tag = tag_text(source, m.start())
            if not re.search(BORDER_CLASS_RE, tag):
                continue
            bordered += 1
            if "border-[var(--color-border-control)]" not in tag:
                line = source.count("\n", 0, m.start()) + 1
                off.append(f"{path.relative_to(SRC)}:{line}")
    check("every bordered control uses border-control", not off, ", ".join(off))
    check("the markup has bordered controls to check", bordered >= 100, str(bordered))


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
    test_scoped_blocks()
    test_claims()
    test_controls()
    test_map_copy()
    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nall palette assertions passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
