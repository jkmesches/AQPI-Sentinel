"""schema.sql must apply cleanly to a genuinely empty database.

Run directly (no DB, no network):

    python validation_tests/test_schema_fresh_install.py

This exists because of a bug that was invisible to everyone who already had a
database. `CREATE INDEX IF NOT EXISTS idx_ms_ts ON metric_samples(ts)` sat two
lines ABOVE `CREATE TABLE IF NOT EXISTS metric_samples`. On any existing
deployment the table was already there, so the statement succeeded and every
restart and upgrade looked fine. On a fresh database it raised
UndefinedTableError, the backend exited during startup, and the container sat
in a restart loop — the first thing a new operator would ever see.

That is the shape of the whole class: statements that reference an object
defined later in the file are silently correct on every machine that has run
an earlier version, and broken only on first install. Nobody developing the
project is ever in that state, which is exactly why it needs a test.

Checked statically rather than by standing up Postgres: it runs in
milliseconds with no dependencies, so it can sit in the normal suite instead
of a docker-only tier.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent.parent / "backend" / "db" / "schema.sql"

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    return "\n".join(re.sub(r"--.*$", "", ln) for ln in sql.splitlines())


def main() -> int:
    check("schema.sql exists", SCHEMA.exists(), str(SCHEMA))
    if not SCHEMA.exists():
        return 1
    sql = strip_comments(SCHEMA.read_text())

    # Where each table is defined, by character offset.
    created: dict[str, int] = {}
    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
                         sql, re.I):
        created.setdefault(m.group(1).lower(), m.start())
    check("tables were parsed out of the file", len(created) > 5, f"{len(created)} tables")

    # --- every index must come after the table it indexes ----------------
    late = []
    n_idx = 0
    for m in re.finditer(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
                         r"(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+ON\s+"
                         r"([A-Za-z_][A-Za-z0-9_]*)", sql, re.I):
        n_idx += 1
        idx, tbl = m.group(1), m.group(2).lower()
        if tbl not in created:
            late.append(f"{idx} indexes unknown table {tbl}")
        elif m.start() < created[tbl]:
            late.append(f"{idx} is defined before CREATE TABLE {tbl}")
    check(f"all {n_idx} indexes are defined after their table",
          not late, "; ".join(late))

    # --- same rule for foreign keys --------------------------------------
    bad_fk = []
    for m in re.finditer(r"REFERENCES\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.I):
        tbl = m.group(1).lower()
        if tbl not in created:
            bad_fk.append(f"REFERENCES unknown table {tbl}")
        elif m.start() < created[tbl]:
            bad_fk.append(f"REFERENCES {tbl} before it is created")
    check("all foreign keys reference already-created tables",
          not bad_fk, "; ".join(bad_fk))

    # --- and for anything that ALTERs or TRIGGERs on a table -------------
    bad_alter = []
    for m in re.finditer(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)", sql, re.I):
        tbl = m.group(1).lower()
        if tbl in created and m.start() < created[tbl]:
            bad_alter.append(f"ALTER TABLE {tbl} before it is created")
    check("all ALTER TABLE statements follow their table",
          not bad_alter, "; ".join(bad_alter))

    # --- idempotence: re-applying on every boot must not fail ------------
    # store.connect() runs this file at every startup, not just the first.
    plain_tables = re.findall(r"CREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS)", sql, re.I)
    check("every CREATE TABLE uses IF NOT EXISTS (schema re-runs on every boot)",
          not plain_tables, f"{len(plain_tables)} without it")
    plain_idx = re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?!CONCURRENTLY|IF\s+NOT\s+EXISTS)", sql, re.I)
    check("every CREATE INDEX uses IF NOT EXISTS",
          not plain_idx, f"{len(plain_idx)} without it")

    print(f"\n{len(failures)} FAILED: {', '.join(failures)}" if failures
          else "\nPASS — schema.sql is safe to apply to an empty database")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
