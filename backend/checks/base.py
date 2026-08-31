"""Universal Check interface and result envelope.

This is the hinge for extensibility: every monitoring probe — current or
future, HTTP or filesystem or SSH or anything else — is a ``Check`` subclass
that emits a ``CheckResult``. The scheduler, store, alarm router, API, and
frontend all consume the generic ``CheckResult`` shape.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from .transports import CheckContext


Status = Literal["pass", "warn", "fail", "skip", "error"]


@dataclass
class CheckResult:
    """One execution of a check. Persisted to ``check_runs``; numerics in
    ``metrics`` are also written to ``metric_samples`` for time-series."""

    check_id:    str
    target:      str
    stage:       str
    status:      Status
    started_at:  datetime
    finished_at: datetime
    summary:     str                       = ""
    payload:     dict[str, Any]            = field(default_factory=dict)
    metrics:     dict[str, float]          = field(default_factory=dict)
    artifacts:   list[str]                 = field(default_factory=list)


class Check:
    """Subclass + override the class attributes + ``run()``.

    For parameterized checks (one per product / radar / mount point) override
    ``__init__`` and set the instance-specific attributes there, then register
    each instance with ``register(MyCheck(target="X"))`` instead of decorating
    the class.
    """

    id:         str = ""
    stage:      str = ""
    target:     str = ""
    cadence_s:  int = 60
    depends_on: list[str] = []
    # Dependencies used for ALARM SUPPRESSION ONLY — they do not cause the
    # scheduler to demote this check's result to `skip`.
    #
    # `depends_on` means "if that is broken, my observation is meaningless
    # collateral" (origin unreachable -> per-radar results are unknowable), so
    # the scheduler demotes to skip. That is wrong for a dependency which is
    # an AGGREGATE OF THIS CHECK rather than a cause of it: the fleet
    # correlation check is computed from the radar verdicts, so letting it
    # demote them would have an aggregate marking its own inputs "not
    # measured" when in fact we measured them precisely. Use this instead to
    # get one page instead of five while keeping every per-radar verdict
    # visible and true.
    alarm_only_depends_on: list[str] = []
    # Emit a `timed_out` metric (0 on success, 1 on read timeout) so
    # avg(timed_out) over this check is a true truncation rate.
    #
    # Opt-in, because a rate needs BOTH outcomes. The scheduler can only see
    # failures; the success side has to come from the check itself. If a check
    # sets this without also emitting `timed_out: 0.0` on its success path the
    # metric reads 1.0 forever, and if it emits 0 without setting this it reads
    # 0.0 forever. Either way it silently lies, which is worse than absent.
    reports_timeout_rate: bool = False

    async def run(self, ctx: "CheckContext") -> CheckResult:
        raise NotImplementedError


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
