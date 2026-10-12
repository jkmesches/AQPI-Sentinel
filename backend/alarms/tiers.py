"""Per-tier notification gate — which half of the stack may page someone.

Every radar and product is watched two independent ways: the `L*` stages ask
radarca what it believes, the `LB*` stages read the published files off
K2/Trinity. A deployment can have good reason to want only one of them audible
— the backend checks are new on a given site and still being tuned, or the
radarca tier is known to be lying for the week and its alarms are noise — and
the alternative people reach for otherwise is deleting routes out of
alerts.yaml, which loses the alarms too.

So this gates DISPATCH ONLY. A gated alarm still opens, still carries its
severity, still appears on the timeline and in the daily report, and records a
`tier-disabled` row in notification_log where the message would have gone — so
"what did Sentinel not tell me, and when" is answerable afterwards. The thing
this must never become is a filter on evaluation: a tier that is not recording
is a tier whose history has a hole in it, and the hole is invisible later.

Stored under its own settings key rather than in the digest blob. The digest
blob is a REPORTING config; an alerting gate living inside it would mean that
turning the morning email off, or resetting its recipients, changed who gets
paged at 03:00. One fact, one home.

Fails OPEN everywhere: a missing row, a malformed blob, or a database error
all leave both tiers audible. A gate that silences alerts when it cannot read
its own configuration is worse than no gate.
"""
from __future__ import annotations

import logging
from typing import Any

from ..stages import TIERS, stage_tier

log = logging.getLogger(__name__)

SETTINGS_KEY = "alarm_tiers"

DEFAULTS: dict[str, Any] = {t: True for t in TIERS}


def may_notify(cfg: dict | None, stage: str) -> bool:
    """May an alarm at this stage be dispatched?

    Only an explicit `false` silences a tier. Anything else — key absent,
    value a string, blob not a dict — reads as audible, which is the same
    fail-open direction as an unknown stage in stage_tier().
    """
    if not isinstance(cfg, dict):
        return True
    return cfg.get(stage_tier(stage), True) is not False


async def load(pool) -> dict[str, Any]:
    """Read the gate. Never raises; an unreadable gate is an open gate."""
    cfg = dict(DEFAULTS)
    try:
        row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1",
                                  SETTINGS_KEY)
    except Exception:
        log.exception("alarm tiers: could not read %r — both tiers stay audible",
                      SETTINGS_KEY)
        return cfg
    val = row["value"] if row else None
    if isinstance(val, str):
        # A row written by something that double-encoded on save. See the
        # same unwrap in digest.load_config and the warning in put_setting.
        import json
        try:
            val = json.loads(val)
        except ValueError:
            log.warning("alarm tiers: %r is not JSON — both tiers stay audible",
                        SETTINGS_KEY)
            val = None
    if isinstance(val, dict):
        for t in TIERS:
            if t in val:
                cfg[t] = val[t] is not False
    return cfg
