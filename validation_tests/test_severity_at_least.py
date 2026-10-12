"""Route `severity_at_least` — raises severity, never filters, old name still loads.

Run directly (no DB, no network):

    python validation_tests/test_severity_at_least.py

The route-level key was called `severity_floor` until 2026-10-12, and
alarms/router.py documented it as a FILTER:

    floor=warn -> notify only on broken (fail/error)

It never filtered. compute_severity applies it as _max_sev(floor, sev), which
only raises, and nothing on the notification path tests severity at all. Those
documented semantics belong to a different `severity_floor` — the one in
push.py, on per-device push routing, which really does drop notifications
below a rank. One identifier, two opposite behaviours.

Renaming the route-level key was chosen over making it match its
documentation, because the L0 route uses it to be critical AT OPEN in order to
page fast (hold_down 2m, "five minutes is too much latency to accept"). Under
the filtering reading, `critical` would have meant "notify only once open past
30 minutes" — a fail opens at `warn` and reaches `critical` only after
PROMOTE_AFTER_S — so implementing the docs would have silently delayed the one
route whose purpose is speed.

Route ignores unknown keys, so the rename needs the old spelling to keep
working: every deployment carries its own alerts.yaml, and a silent rename
would have dropped L0's setting with nothing to show for it.
"""
from __future__ import annotations
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.alarms.models import Route, load_config          # noqa: E402
from backend.alarms.router import _SEV_RANK                   # noqa: E402

_fail = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _fail
    print(("  ok    " if ok else "  FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        _fail += 1


def main() -> int:
    print("the new key is read, the old one still loads:")
    r = Route(policy="p", severity_at_least="critical")
    check("severity_at_least set directly", r.severity_at_least == "critical")

    r = Route(policy="p", severity_floor="critical")
    check("deprecated severity_floor is migrated",
          r.severity_at_least == "critical", str(r.severity_at_least))

    r = Route(policy="p", severity_at_least="warn", severity_floor="critical")
    check("both set -> the new name wins",
          r.severity_at_least == "warn", str(r.severity_at_least))

    r = Route(policy="p")
    check("neither set -> None", r.severity_at_least is None)

    print("\nthe deprecation is audible, not silent:")
    logging.disable(logging.NOTSET)
    log = logging.getLogger("backend.alarms.models")
    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, rec): records.append(rec.getMessage())

    h = Grab()
    log.addHandler(h)
    prev = log.level
    log.setLevel(logging.WARNING)
    Route(policy="p", severity_floor="critical")
    Route(policy="p", severity_at_least="critical")          # must NOT warn
    log.setLevel(prev)
    log.removeHandler(h)
    check("old spelling logs exactly one warning", len(records) == 1,
          f"{len(records)} warning(s)")
    check("the warning names the replacement",
          bool(records) and "severity_at_least" in records[0])

    print("\nit raises severity and cannot lower it (_max_sev semantics):")
    check("critical outranks warn outranks info",
          _SEV_RANK["critical"] > _SEV_RANK["warn"] > _SEV_RANK["info"])

    print("\nthe shipped alerts.yaml uses the new spelling:")
    import io
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    raw = io.open(os.path.join(root, "alerts.yaml"), encoding="utf-8").read()
    # Comments may discuss the old name; only a real key assignment counts.
    keys = [ln for ln in raw.splitlines()
            if ln.strip().startswith("severity_floor:")]
    check("no route still assigns severity_floor", not keys, "; ".join(keys))

    cfg = load_config(os.path.join(root, "alerts.yaml"))
    l0 = [r for r in cfg.routes if r.match.get("stage") == "L0"]
    check("L0 route still resolves to critical",
          bool(l0) and l0[0].severity_at_least == "critical",
          l0[0].severity_at_least if l0 else "no L0 route")
    check("L0 keeps its 2m hold-down and 10m repeat",
          bool(l0) and l0[0].hold_down_s == 120 and l0[0].repeat_interval_s == 600,
          f"hold_down={l0[0].hold_down_s}s repeat={l0[0].repeat_interval_s}s" if l0 else "-")

    print("\npush.py keeps its own filtering severity_floor (not renamed):")
    import inspect
    from backend import push
    src = inspect.getsource(push)
    check("push still reads routing['severity_floor']",
          '"severity_floor"' in src or "'severity_floor'" in src)

    print()
    if _fail:
        print(f"FAILED: {_fail} assertion(s)")
        return 1
    print("all severity_at_least assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
