"""Pydantic models for ``alerts.yaml``.

The YAML loader (:func:`load_config`) returns an :class:`AlertsConfig` with
``delay``/``repeat_interval`` strings already parsed to seconds. Validation
happens here so the rest of the system can trust the shapes.
"""
from __future__ import annotations
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .parse import parse_duration


# --------------------------------------------------------------------------
# Pieces
# --------------------------------------------------------------------------

class SmtpConfig(BaseModel):
    host: str
    port: int = 587
    starttls: bool = True
    username: str | None = None
    password: str | None = None
    from_: str = Field("Sentinel <noreply@example.com>", alias="from")
    reply_to: str | None = None


class Receiver(BaseModel):
    name: str
    email: list[str] = Field(default_factory=list)
    webhook: str | None = None
    console: bool = False
    template: str = "default"
    # Optional group memberships. At dispatch time the engine looks up each
    # group's active members (filtered by the group's notification schedule)
    # and unions their emails into this receiver's email list. Lets ops
    # define "page everyone in the on-call group" as a single receiver.
    group_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _at_least_one_channel(self):
        # A group reference also counts as a channel — the engine will
        # expand it to emails at dispatch time. Validation runs on saved
        # config, so this catches "receiver with no way to reach anyone"
        # while letting group-only receivers through.
        if not (self.email or self.webhook or self.console or self.group_ids):
            raise ValueError(f"receiver {self.name!r} has no channels")
        return self


class EscalationStep(BaseModel):
    delay: str = "0m"
    receivers: list[str] = Field(default_factory=list)
    # Optional direct group references — equivalent to creating a Receiver
    # whose only channel is group_ids. Lets admins pick "page group on-call"
    # in a step without making a wrapper receiver.
    group_ids: list[int] = Field(default_factory=list)

    delay_s: int = 0   # computed

    @model_validator(mode="after")
    def _parse_delay(self):
        self.delay_s = parse_duration(self.delay)
        return self


class EscalationPolicy(BaseModel):
    name: str
    steps: list[EscalationStep]


class Condition(BaseModel):
    """One side-condition on a route. Multiple keys = AND."""
    time_of_day_in: str | None = None
    time_of_day_not_in: str | None = None
    timezone: str = "UTC"
    weekday_only: bool = False
    duration_at_severity_min: int | None = None
    count_of_targets_failing: str | None = None      # e.g. ">=2"
    metric_above: dict[str, Any] | None = None       # {metric, value}


class Route(BaseModel):
    match: dict[str, str] = Field(default_factory=dict)
    when: Condition | None = None
    policy: str
    severity_floor: str | None = None                 # "warn" | "critical"
    repeat_interval: str | None = None
    group_by: list[str] = Field(default_factory=list)

    # === Load-bearing: how long a condition must persist to be an alarm ===
    #
    # Without this, one bad check run opens an alarm and one good run closes
    # it. Measured over the 7 days to 2026-09-03: 2,308 alarms, of which
    # 91.9% closed in under five minutes. They flapped and self-resolved
    # before a human could look, and every one of them would have been an
    # email once SMTP was configured.
    #
    # What a hold-down keeps, over that same window:
    #     none   2,308      2m   719      5m   187      10m   122
    # The 21 alarms that ran longer than four hours and the 7 still open are
    # all inside the 187, so the signal survives intact.
    #
    # Nothing is hidden by this. Every individual bad run is still recorded
    # and still drawn on the timeline; it simply stops being an *alarm*,
    # which should mean "this persisted", not "this happened once".
    hold_down: str | None = None
    repeat_interval_s: int | None = None              # computed
    hold_down_s: int | None = None                    # computed

    @model_validator(mode="after")
    def _parse_repeat(self):
        if self.repeat_interval:
            self.repeat_interval_s = parse_duration(self.repeat_interval)
        if self.hold_down:
            self.hold_down_s = parse_duration(self.hold_down)
        return self


class Silence(BaseModel):
    id: str
    matchers: dict[str, str]
    starts: datetime
    ends: datetime
    reason: str | None = None


# Applied to any route that does not set its own hold_down. Five minutes is
# where the measured curve flattens: it removes 92% of alarms while keeping
# every one that lasted long enough for a person to act on. Set to "0" to
# restore the old open-on-first-failure behavior.
DEFAULT_HOLD_DOWN = "5m"


class AlertsConfig(BaseModel):
    smtp: SmtpConfig | None = None
    receivers: list[Receiver] = Field(default_factory=list)
    escalation_policies: list[EscalationPolicy] = Field(default_factory=list)
    routes: list[Route] = Field(default_factory=list)
    silences: list[Silence] = Field(default_factory=list)
    # Global default; a route's own hold_down wins.
    hold_down: str = DEFAULT_HOLD_DOWN

    # Validated at load so a typo is loud. The first version caught the
    # ValueError in hold_down_for() and fell back to the default, which meant
    # `hold_down: 30` (intending 30 seconds) silently became 300 — a
    # misconfiguration that looked like it had been applied.
    @model_validator(mode="after")
    def _validate_hold_down(self):
        parse_duration(self.hold_down)   # raises on a bad value
        return self

    def hold_down_for(self, route: "Route | None") -> int:
        """Seconds a condition must persist before it becomes an alarm."""
        if route is not None and route.hold_down_s is not None:
            return route.hold_down_s
        return parse_duration(self.hold_down)

    # --------------------- lookups --------------------------------------
    def receiver(self, name: str) -> Receiver | None:
        for r in self.receivers:
            if r.name == name:
                return r
        return None

    def policy(self, name: str) -> EscalationPolicy | None:
        for p in self.escalation_policies:
            if p.name == name:
                return p
        return None


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------

_DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "alerts.yaml"


def _interpolate_env(node):
    """Replace ``${VAR}`` with ``os.environ[VAR]`` (string-typed)."""
    if isinstance(node, dict):
        return {k: _interpolate_env(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_interpolate_env(v) for v in node]
    if isinstance(node, str) and "${" in node:
        import re
        return re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: os.environ.get(m.group(1), ""), node)
    return node


def load_config(path: str | Path | None = None) -> AlertsConfig:
    p = Path(path) if path else _DEFAULT_PATH
    if not p.exists():
        # Empty config → one default route that fires nothing. Engine still
        # opens/closes alarms; just no dispatch.
        return AlertsConfig()
    raw = yaml.safe_load(p.read_text()) or {}
    raw = _interpolate_env(raw)
    return AlertsConfig.model_validate(raw)


async def load_config_from_db_or_yaml(pool) -> AlertsConfig:
    """Prefer a settings(key='alerts_config') row over the on-disk YAML.
    Falls back to the YAML file (and finally an empty config) so a fresh
    install with no DB row still has a sane default. The admin UI saves
    to the DB row and triggers `engine.reload(...)`."""
    import json as _json
    try:
        row = await pool.fetchrow(
            "SELECT value FROM settings WHERE key = 'alerts_config'"
        )
    except Exception:
        row = None
    if row is not None:
        val = row["value"]
        if isinstance(val, str):
            val = _json.loads(val)
        try:
            return AlertsConfig.model_validate(val)
        except Exception:
            # bad DB row — fall through to YAML
            pass
    return load_config()
