"""Global check + sink + condition registries.

Modules under ``backend/checks/`` and ``backend/alarms/{sinks,conditions}/``
self-register at import time. The scheduler and API read these registries
directly.
"""
from __future__ import annotations
from typing import TypeVar

from .checks.base import Check

CHECKS: dict[str, Check] = {}

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
    if not inst.id:
        raise ValueError(f"{type(inst).__name__} has no id")
    if inst.id in CHECKS:
        raise ValueError(f"Duplicate check id: {inst.id!r}")
    CHECKS[inst.id] = inst
    return check_or_cls


def all_stages() -> set[str]:
    return {c.stage for c in CHECKS.values() if c.stage}
