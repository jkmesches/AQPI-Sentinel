"""The CoSMoS cycle manifest, read off the backend mount: items 3 and 11 of
wiki plans/74-.

Why this exists
---------------
The four LB1 CoSMoS products go stale for several reasons that a frame's age
alone cannot tell apart: HRRR forcing arrived short, the warmstart rebuild
held the model lock, the model produced no forecast, or the publish failed.
The first two are the schedule working as designed, and the last two are
breakage. The driver now names the outcome in a manifest after every cycle,
and these two checks read it.

``layer1.model.cycle`` (item 3) reports the newest cycle's outcome: pass when
it ran, warn when it skipped on purpose, fail when it lost or failed to publish
a forecast. It declines to judge a manifest 150 min old or dated ahead of this
host's clock, since such an outcome says nothing about the current cycle and a
stale pass is the dangerous kind.

``layer1.model.manifest`` (item 11) reports the manifest's own age, which is
the only signal for a driver that died before writing anything: a preprocess
crash, an OOM kill, a dead K2 mount. It warns from 150 min, and on a manifest
dated ahead of this host's clock, and fails past 240.

The rules live in ``backend.cosmos_manifest``. This module only reads the file.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import TYPE_CHECKING, Any

from typing_extensions import override

# Relative, as the whole package imports.
from ..config import (BACKEND_PRODUCT_CADENCE_S, BACKEND_SOURCE,  # noqa: TID252
                      COSMOS_MANIFEST_FAIL_S, COSMOS_MANIFEST_REL, COSMOS_MANIFEST_WARN_S,
                      COSMOS_PRODUCTS, SETTINGS)
from ..cosmos_manifest import (OUTCOME_STATUS, CycleManifest,  # noqa: TID252
                               ManifestError, hrrr_complete, in_window, manifest_age_s,
                               parse_manifest)
from ..errors import humanize_error  # noqa: TID252
from ..registry import register  # noqa: TID252
from .base import Check, CheckResult, Status, utcnow

if TYPE_CHECKING:
    from .transports import CheckContext

# The LB1 product checks' bound on a blocking read, for the same reason: a
# hung NFS mount blocks open(2) indefinitely.
FS_TIMEOUT_S = 5.0

MANIFEST_CHECK_ID = "layer1.model.manifest"
CYCLE_CHECK_ID = "layer1.model.cycle"
TARGET = "cosmos"


def manifest_path() -> str:
    """Absolute path of the cycle manifest inside this container.

    Returns:
        The path under SETTINGS.backend_root.

    Raises:
        RuntimeError: If no backend mount is configured, where none of the
            callers is registered.
    """
    root = SETTINGS.backend_root
    if root is None:
        raise RuntimeError("no backend mount is configured")
    return os.path.join(root, *COSMOS_MANIFEST_REL)


def _load(path: str) -> CycleManifest:
    """Blocking. Read and parse the manifest at `path`.

    Raises:
        ManifestError: For a document json cannot hold in memory, such as
            one nested deeper than the interpreter's recursion limit.
    """
    with open(path, "rb") as fh:
        try:
            doc = json.load(fh)
        except RecursionError as e:
            raise ManifestError("manifest nests too deeply to read") from e
    return parse_manifest(doc)


async def read_manifest(path: str) -> CycleManifest:
    """Read the manifest with a bound on how long a hung mount may hold it.

    Args:
        path: The manifest's absolute path.

    Returns:
        The parsed manifest.

    Raises:
        TimeoutError: If the read takes longer than FS_TIMEOUT_S.
        OSError: If the file is missing or unreadable.
        ValueError: If it is not JSON, or not a manifest (ManifestError).
    """
    return await asyncio.wait_for(asyncio.to_thread(_load, path), FS_TIMEOUT_S)


def read_failure(exc: BaseException) -> tuple[Status, str]:
    """The status and summary for a manifest that could not be read.

    A missing file is a fail, because the driver writes one after every cycle
    and its absence is a fact about the pipeline. A hung mount, an unreadable
    file or a malformed document is an error, a broken read rather than a
    verdict on the cycle.

    Args:
        exc: What read_manifest raised.

    Returns:
        The status and the summary line.
    """
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return "error", f"manifest read timed out after {FS_TIMEOUT_S:g}s, mount may be hung"
    if isinstance(exc, FileNotFoundError):
        return "fail", "no cycle manifest on the backend"
    if isinstance(exc, ManifestError):
        return "error", f"manifest unreadable: {exc}"
    if isinstance(exc, json.JSONDecodeError | UnicodeDecodeError):
        return "error", f"manifest is not valid JSON: {humanize_error(exc)}"
    return "error", f"manifest read failed: {humanize_error(exc)}"


def _describe(m: CycleManifest, now: datetime) -> dict[str, Any]:
    """The manifest's facts as a payload, shared by both checks.

    Its reason goes under `manifest_reason`, because `reason` in a payload is
    what marks a skip inconclusive (alarms.suppression).
    """
    return {
        "path": manifest_path(), "metfile": m.metfile, "outcome": m.outcome,
        "manifest_reason": m.reason, "wrote_at": m.wrote_at.isoformat(),
        "manifest_age_s": round(manifest_age_s(m, now), 1),
        "file_count": m.file_count, "file_count_expected": m.file_count_expected,
        "hrrr_complete": hrrr_complete(m),
        "lock_since": m.lock_since.isoformat() if m.lock_since else None,
    }


class _ManifestCheck(Check):
    """What the two manifest checks share: where, how often, which tree."""

    stage = "LB1"

    def __init__(self) -> None:
        self.target = TARGET
        self.source_tag, self.source_label = BACKEND_SOURCE
        self.cadence_s = max(60, int(BACKEND_PRODUCT_CADENCE_S))

    def _result(self, t0: datetime, status: Status, summary: str,
                payload: dict[str, Any], metrics: dict[str, float]) -> CheckResult:
        """Wrap one verdict in this check's envelope."""
        return CheckResult(check_id=self.id, target=self.target, stage=self.stage,
                           status=status, started_at=t0, finished_at=utcnow(),
                           summary=summary, payload=payload, metrics=metrics)


class CosmosManifestAgeCheck(_ManifestCheck):
    """Item 11: how long since the driver last wrote a manifest."""

    id = MANIFEST_CHECK_ID

    @override
    async def run(self, ctx: CheckContext) -> CheckResult:  # noqa: ARG002  interface parameter
        """Read the manifest and judge its age against 150 and 240 min.

        Args:
            ctx: The scheduler's transports, unused by a filesystem read.

        Returns:
            Pass, warn from 150 min or when dated ahead of this host's clock,
            fail past 240, or the read failure.
        """
        t0 = utcnow()
        try:
            m = await read_manifest(manifest_path())
        except (OSError, ValueError, TimeoutError) as e:
            failed, summary = read_failure(e)
            return self._result(t0, failed, summary, {"path": manifest_path()}, {})
        age = manifest_age_s(m, t0)
        # Dated ahead, the age is unknown, and a driver whose clock runs
        # ahead and then dies would otherwise read healthy until real time
        # caught up. A manifest a second ahead reads so for one run at most,
        # which the alarm hold-down absorbs.
        status: Status = ("fail" if age > COSMOS_MANIFEST_FAIL_S
                          else "pass" if in_window(m, t0) else "warn")
        when = (f"{age / 60:.0f} min ago" if age >= 0
                else f"{-age:.0f} s ahead of this host's clock")
        summary = (f"manifest for {m.metfile or 'an unnamed cycle'} written {when}"
                   f"  (warn {COSMOS_MANIFEST_WARN_S / 60:.0f},"
                   f" fail {COSMOS_MANIFEST_FAIL_S / 60:.0f} min)")
        return self._result(t0, status, summary, _describe(m, t0),
                            {"manifest_age_s": float(age)})


class CosmosCycleCheck(_ManifestCheck):
    """Item 3: what the newest cycle did, and why."""

    id = CYCLE_CHECK_ID

    def __init__(self) -> None:
        super().__init__()
        self.depends_on = [MANIFEST_CHECK_ID]

    @override
    async def run(self, ctx: CheckContext) -> CheckResult:  # noqa: ARG002  interface parameter
        """Read the manifest and report its outcome while it is fresh.

        Args:
            ctx: The scheduler's transports, unused by a filesystem read.

        Returns:
            The outcome's verdict, an inconclusive skip for a manifest 150 min
            old or dated ahead of this host's clock, or the read failure.
        """
        t0 = utcnow()
        try:
            m = await read_manifest(manifest_path())
        except (OSError, ValueError, TimeoutError) as e:
            failed, summary = read_failure(e)
            return self._result(t0, failed, summary, {"path": manifest_path()}, {})
        payload = _describe(m, t0)
        age = manifest_age_s(m, t0)
        metrics = {"manifest_age_s": float(age)}
        if not in_window(m, t0):
            # Inconclusive, so an open alarm stays as it is and none opens.
            # The manifest's own check reports the age.
            payload["reason"] = "source_out_of_window"
            when = (f"is {age / 60:.0f} min old" if age >= 0
                    else f"is dated {-age:.0f} s ahead of this host's clock")
            return self._result(
                t0, "skip", f"newest manifest {when} and cannot speak for the current"
                f" cycle ({m.metfile} {m.outcome})", payload, metrics)
        verdict = OUTCOME_STATUS.get(m.outcome)
        if verdict is None:
            return self._result(t0, "error", f"{m.metfile}: unknown outcome {m.outcome!r}",
                                payload, metrics)
        detail = f": {m.reason}" if m.reason else ""
        if m.lock_since is not None:
            detail += f" (lock taken {m.lock_since:%H:%M}Z)"
        status: Status = "warn" if verdict == "warn" else "fail" if verdict == "fail" else "pass"
        return self._result(t0, status, f"{m.metfile} {m.outcome}{detail}", payload, metrics)


# Gated like the LB1 product checks: no mount, no checks. A profile without
# CoSMoS lists no CoSMoS products and registers neither.
if SETTINGS.backend_root and COSMOS_PRODUCTS:
    register(CosmosManifestAgeCheck())
    register(CosmosCycleCheck())
