"""Active-silence lookup. A silence matches an alarm when:

  * now is in [starts, ends]
  * every key in ``matchers`` equals the alarm's value (str-compared)

Pulls live from the silences table — config-file silences are merged at
startup, ad-hoc silences are POSTed at runtime.
"""
from __future__ import annotations
import json
from datetime import datetime


def matches_silence(silence: dict, alarm: dict, now: datetime) -> bool:
    if not (silence["starts"] <= now <= silence["ends"]):
        return False
    matchers = silence["matchers"]
    if isinstance(matchers, str):           # asyncpg returns JSONB-as-str on first decode
        matchers = json.loads(matchers)
    for k, v in matchers.items():
        if str(alarm.get(k)) != str(v):
            return False
    return True


def find_active_silence(silences: list[dict], alarm: dict, now: datetime) -> dict | None:
    for s in silences:
        if matches_silence(s, alarm, now):
            return s
    return None
