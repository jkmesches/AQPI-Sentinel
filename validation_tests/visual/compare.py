#!/usr/bin/env python3
"""Compare two map-console captures and report what the refactor moved.

Reads two directories written by ``capture.py`` and, for every state in both,
diffs the structural snapshot. Keys either capture measured as volatile are
dropped first, so a frame that advanced between the two runs does not read as
a regression.

``layerOrder`` gets a sequence diff rather than an index-wise one. A layer
inserted at the wrong place shifts every index after it, and a plain
comparison would report two hundred differences for one fault.

Usage
-----
::

    python validation_tests/visual/compare.py BEFORE_DIR AFTER_DIR [--full]

Exit status is 0 when nothing structural changed.
"""
from __future__ import annotations

import argparse
import json
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import TypeAlias

CAPTURE = Path(__file__).resolve().parent
sys.path.insert(0, str(CAPTURE))

from capture import flatten  # noqa: E402  sibling module, loaded by path

Json: TypeAlias = "str | int | float | bool | list[Json] | dict[str, Json] | None"

# How many differing keys to print per state before summarizing the rest. A
# state that moved everything is one fault, and a thousand lines of it buries
# the next state.
SHOWN_PER_STATE = 12

# Marks a line the exit status ignores. The capture measured this state's own
# two loads disagreeing, so a later run disagreeing with it proves nothing.
WARN = "(order varies between loads of this state) "


def load(directory: Path) -> dict[str, dict[str, Json]]:
    """Read every state record in a capture directory.

    Args:
        directory: A directory written by `capture.py`.

    Returns:
        Records by state name.

    Raises:
        FileNotFoundError: If the directory holds no manifest, which means it
            is not a capture and comparing it would report nonsense.
    """
    if not (directory / "manifest.json").is_file():
        raise FileNotFoundError(f"{directory} has no manifest.json")
    out: dict[str, dict[str, Json]] = {}
    for path in sorted(directory.glob("*.json")):
        if path.name == "manifest.json":
            continue
        rec = json.loads(path.read_text())
        out[str(rec["name"])] = rec
    return out


def volatile_keys(left: dict[str, Json], right: dict[str, Json]) -> set[str]:
    """Keys that either capture saw change on its own.

    Args:
        left: A state record from the first capture.
        right: The same state from the second.

    Returns:
        The union of both volatile lists.
    """
    keys: set[str] = set()
    for rec in (left, right):
        vol = rec.get("volatile")
        if isinstance(vol, list):
            keys.update(str(k) for k in vol)
    return keys


def order_diff(before: list[str], after: list[str]) -> list[str]:
    """Describe how a layer order changed, in moves rather than indices.

    Args:
        before: Layer ids in paint order, from the first capture.
        after: Layer ids in paint order, from the second.

    Returns:
        One line per insertion, removal or replacement.
    """
    lines: list[str] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(a=before, b=after).get_opcodes():
        if tag == "equal":
            continue
        gone = before[i1:i2]
        came = after[j1:j2]
        if tag == "delete":
            lines.append(f"layerOrder: dropped {gone} after "
                         f"{before[i1 - 1] if i1 else '(start)'}")
        elif tag == "insert":
            lines.append(f"layerOrder: added {came} after "
                         f"{after[j1 - 1] if j1 else '(start)'}")
        else:
            lines.append(f"layerOrder: {gone} became {came}")
    return lines


def as_str_list(value: Json) -> list[str]:
    """Coerce a snapshot field to a list of ids.

    Args:
        value: The `layerOrder` field of a snapshot.

    Returns:
        The ids, or an empty list when the field is absent or malformed.
    """
    if not isinstance(value, list):
        return []
    return [str(v) for v in value]


def compare_state(left: dict[str, Json], right: dict[str, Json],
                  *, full: bool) -> list[str]:
    """Diff one state, with volatile keys and layer order handled apart.

    Args:
        left: The state record from the first capture.
        right: The same state from the second.
        full: Print every differing key rather than the first few.

    Returns:
        Report lines, empty when nothing structural changed.
    """
    a_snap = left.get("snapshot")
    b_snap = right.get("snapshot")
    if not isinstance(a_snap, dict) or not isinstance(b_snap, dict):
        return ["snapshot missing from one side"]

    # A state whose own two loads disagreed about paint order cannot hold a
    # later refactor to that order. The difference is still printed, with the
    # reason, so it stays visible instead of being dropped.
    loose = bool(left.get("unstableOrder")) or bool(right.get("unstableOrder"))
    order = order_diff(as_str_list(a_snap.get("layerOrder")),
                       as_str_list(b_snap.get("layerOrder")))
    lines = [f"{WARN}{line}" for line in order] if loose else list(order)

    skip = volatile_keys(left, right)
    a_flat = flatten({k: v for k, v in a_snap.items() if k != "layerOrder"})
    b_flat = flatten({k: v for k, v in b_snap.items() if k != "layerOrder"})
    keys = sorted((set(a_flat) | set(b_flat)) - skip)

    diffs: list[str] = []
    for key in keys:
        a_val = a_flat.get(key, "<absent>")
        b_val = b_flat.get(key, "<absent>")
        if a_val != b_val:
            diffs.append(f"{key}: {a_val!r} -> {b_val!r}")

    shown = diffs if full else diffs[:SHOWN_PER_STATE]
    lines.extend(shown)
    if len(diffs) > len(shown):
        lines.append(f"... and {len(diffs) - len(shown)} more "
                     f"(pass --full to see them)")
    return lines


def run(before: Path, after: Path, *, full: bool) -> int:
    """Compare two captures and print a report.

    Args:
        before: Capture taken first.
        after: Capture taken second.
        full: Print every differing key.

    Returns:
        Process exit status, non-zero when anything structural changed.
    """
    left, right = load(before), load(after)
    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))
    shared = sorted(set(left) & set(right))

    changed = 0
    warned = 0
    for name in shared:
        lines = compare_state(left[name], right[name], full=full)
        if not lines:
            print(f"  ok    {name}")
            continue
        structural = [line for line in lines if not line.startswith(WARN)]
        if structural:
            changed += 1
        else:
            warned += 1
        print(f"  {'DIFF' if structural else 'WARN'}  {name}")
        for line in lines:
            print(f"          {line}")

    for name in only_left:
        print(f"  GONE  {name} was captured before and is missing now")
    for name in only_right:
        print(f"  NEW   {name} appeared in the second capture")

    total = len(shared)
    print(f"\n{total - changed - warned}/{total} states identical; "
          f"{changed} changed, {warned} order-only, "
          f"{len(only_left)} gone, {len(only_right)} new")
    return 1 if (changed or only_left or only_right) else 0


def main() -> int:
    """Parse arguments and compare.

    Returns:
        Process exit status.
    """
    ap = argparse.ArgumentParser(description="diff two map-console captures")
    ap.add_argument("before", type=Path, help="capture taken first")
    ap.add_argument("after", type=Path, help="capture taken second")
    ap.add_argument("--full", action="store_true",
                    help="print every differing key, not the first few")
    args = ap.parse_args()
    try:
        return run(args.before, args.after, full=args.full)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        # A capture killed partway through leaves records and no manifest,
        # which happened here when the deployment was rebuilt mid-run. Say so
        # in one line, because a traceback reads like the comparison failed
        # rather than like the capture did.
        print(f"cannot compare: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
