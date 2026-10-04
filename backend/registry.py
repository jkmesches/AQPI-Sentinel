"""Global check + sink + condition registries.

Modules under ``backend/checks/`` and ``backend/alarms/{sinks,conditions}/``
self-register at import time. The scheduler and API read these registries
directly.
"""
from __future__ import annotations
from typing import TypeVar

from .checks.base import Check
from .config import HAS_RADARCA, RADARCA_ONLY_MODULES

CHECKS: dict[str, Check] = {}

# Checks that were declined because this profile has no HTTP origin to probe.
# Kept rather than discarded so the condition is inspectable instead of a
# silent absence — "why is this check missing" is otherwise unanswerable.
DECLINED: dict[str, str] = {}

T = TypeVar("T")


def register(check_or_cls: T) -> T:
    """Use as a class decorator OR call with a constructed instance.

    ``@register`` instantiates the class with no args and registers it.
    ``register(MyCheck(target="X"))`` registers a parameterized instance.
    Returns the original input so decorator usage doesn't change semantics.
    """
    inst = check_or_cls() if isinstance(check_or_cls, type) else check_or_cls
    if not isinstance(inst, Check):
        raise TypeError(f"register() received non-Check: {type(inst).__name__}")
    # A check from a radarca-only module has nothing to talk to on a profile
    # with no HTTP origin: it would fail every cycle forever, and L0 failures
    # cascade-suppress the backend checks that ARE working.
    #
    # Enforced HERE, not at the import site, because the import site is not a
    # guarantee. checks/__init__ skips these modules on such a profile, but
    # that only holds while nothing else imports them — prewarm.py imports
    # layer2_radar at module scope for EXPECTED_ABSENT_MOMENTS, which put
    # layer2.radar.FLOW and layer2.xband.fleet back into the registry on xqpi
    # with the gate apparently in place. One check in the one place every
    # check must pass through cannot be routed around.
    if not HAS_RADARCA:
        mod = type(inst).__module__.rsplit(".", 1)[-1]
        if mod in RADARCA_ONLY_MODULES:
            DECLINED[getattr(inst, "id", "") or type(inst).__name__] = mod
            return check_or_cls
    if not inst.id:
        raise ValueError(f"{type(inst).__name__} has no id")
    if inst.id in CHECKS:
        raise ValueError(f"Duplicate check id: {inst.id!r}")
    CHECKS[inst.id] = inst
    return check_or_cls


def all_stages() -> set[str]:
    return {c.stage for c in CHECKS.values() if c.stage}
