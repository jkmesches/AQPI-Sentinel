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

    @model_validator(mode="after")
    def _at_least_one_channel(self):
        if not (self.email or self.webhook or self.console):
            raise ValueError(f"receiver {self.name!r} has no channels")
        return self


class EscalationStep(BaseModel):
    delay: str = "0m"
    receivers: list[str]

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

    repeat_interval_s: int | None = None              # computed

    @model_validator(mode="after")
    def _parse_repeat(self):
        if self.repeat_interval:
            self.repeat_interval_s = parse_duration(self.repeat_interval)
        return self


class Silence(BaseModel):
    id: str
    matchers: dict[str, str]
    starts: datetime
    ends: datetime
    reason: str | None = None


class AlertsConfig(BaseModel):
    smtp: SmtpConfig | None = None
    receivers: list[Receiver] = Field(default_factory=list)
    escalation_policies: list[EscalationPolicy] = Field(default_factory=list)
    routes: list[Route] = Field(default_factory=list)
    silences: list[Silence] = Field(default_factory=list)

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
