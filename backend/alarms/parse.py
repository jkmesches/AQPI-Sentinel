"""Tiny duration parser. ``"15m"`` → 900, ``"1h"`` → 3600, etc."""
from __future__ import annotations
import re

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.I)
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s: str | int | None, default: int = 0) -> int:
    if s is None:
        return default
    if isinstance(s, (int, float)):
        return int(s)
    m = _DURATION_RE.match(str(s))
    if not m:
        raise ValueError(f"bad duration: {s!r} (expected like '15m', '1h')")
    return int(m.group(1)) * _UNIT_S[m.group(2).lower()]
