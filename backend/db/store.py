"""Async Postgres access. One pool per process.

Codecs: ``payload`` columns are JSONB. We register a codec that handles dict
↔ JSONB in both directions so callers never have to ``json.dumps`` manually.
"""
from __future__ import annotations
import json
import logging
from typing import Any

import asyncpg

from ..alarms.suppression import INCONCLUSIVE_SKIP_REASONS
from ..checks.base import CheckResult

log = logging.getLogger(__name__)


async def _init_codecs(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: json.dumps(v, default=str),
        decoder=json.loads,
        schema="pg_catalog",
    )


class Store:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1, max_size=10,
            init=_init_codecs,
        )
        log.info("postgres pool established")
        await self._apply_schema()

    async def _apply_schema(self) -> None:
        """Apply backend/db/schema.sql idempotently on startup. All
        statements are CREATE TABLE/INDEX IF NOT EXISTS so re-running
        on an existing DB is a no-op. Lets a fresh deploy (empty
        postgres volume) come up clean without an out-of-band
        migration step."""
        assert self.pool is not None
        from pathlib import Path
        schema_path = Path(__file__).resolve().parent / "schema.sql"
        if not schema_path.exists():
            log.warning("schema.sql not found at %s — skipping auto-migrate", schema_path)
            return
        sql = schema_path.read_text()
        async with self.pool.acquire() as conn:
            await conn.execute(sql)
        log.info("schema applied (idempotent)")

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    # ----- writes ---------------------------------------------------------
    async def write_check_run(self, r: CheckResult) -> int:
        assert self.pool is not None
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO check_runs
                    (check_id, target, stage, status, started_at, finished_at,
                     summary, payload, artifacts)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                RETURNING id
                """,
                r.check_id, r.target, r.stage, r.status, r.started_at,
                r.finished_at, r.summary, r.payload, r.artifacts,
            )
            run_id = row["id"]
            if r.metrics:
                await conn.executemany(
                    """
                    INSERT INTO metric_samples (ts, check_id, target, metric, value)
                    VALUES ($1,$2,$3,$4,$5)
                    """,
                    [
                        (r.finished_at, r.check_id, r.target, m, float(v))
                        for m, v in r.metrics.items()
                    ],
                )
            return run_id

    # ----- reads ----------------------------------------------------------
    async def latest_per_check(self) -> list[dict[str, Any]]:
        """Newest run per check_id, via a loose index scan.

        === Load-bearing ===
        The obvious spelling of this is `SELECT DISTINCT ON (check_id) ...
        ORDER BY check_id, finished_at DESC`, and that is what this was
        until 2026-08-08. Do not go back to it.

        Postgres has no loose (skip) index scan, so DISTINCT ON reads
        EVERY row of check_runs in index order and discards all but the
        first per key in the Unique node — and because the select list
        includes `payload`, it also heap-fetches all of them. At 2.2M
        rows that measured 6.8s and 1.9M buffer reads per call. Worse,
        before idx_run_check_finished existed the planner had no usable
        index at all (idx_run_check is on started_at, not finished_at),
        so it did a Seq Scan + full Sort, spilling ~1.4GB to
        base/pgsql_tmp on every call.

        This method is called from three hot paths — the scheduler tick,
        every alarm evaluation, and every /api/status poll — so those
        spills stacked faster than they drained and filled the disk,
        taking production down for 6 days from 2026-08-01. See
        docs/MAINTENANCE.md "Disk space".

        The recursive CTE below walks the ~40 distinct check_ids by
        repeatedly asking the index for the next key greater than the
        last, then does one LIMIT 1 index descent per key. Same result,
        160 buffer hits, 3.2ms, and — the part that matters — cost that
        stays flat as check_runs grows instead of scaling with it.
        """
        assert self.pool is not None
        rows = await self.pool.fetch(
            """
            WITH RECURSIVE keys AS (
                (SELECT check_id FROM check_runs ORDER BY check_id LIMIT 1)
                UNION ALL
                SELECT (SELECT c.check_id FROM check_runs c
                         WHERE c.check_id > k.check_id
                         ORDER BY c.check_id LIMIT 1)
                FROM keys k
                WHERE k.check_id IS NOT NULL
            )
            SELECT r.id, r.check_id, r.target, r.stage, r.status,
                   r.started_at, r.finished_at, r.summary, r.payload,
                   r.artifacts
            FROM keys k
            CROSS JOIN LATERAL (
                SELECT id, check_id, target, stage, status, started_at,
                       finished_at, summary, payload, artifacts
                FROM check_runs r2
                WHERE r2.check_id = k.check_id
                ORDER BY r2.finished_at DESC
                LIMIT 1
            ) r
            WHERE k.check_id IS NOT NULL
            """
        )
        return [dict(r) for r in rows]

    async def latest_run(self, check_id: str) -> dict[str, Any] | None:
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """
            SELECT id, check_id, target, stage, status, started_at, finished_at,
                   summary, payload, artifacts
            FROM check_runs
            WHERE check_id = $1
            ORDER BY finished_at DESC
            LIMIT 1
            """,
            check_id,
        )
        return dict(row) if row else None

    async def history(
        self, check_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        assert self.pool is not None
        rows = await self.pool.fetch(
            """
            SELECT id, check_id, target, stage, status, started_at, finished_at,
                   summary, payload
            FROM check_runs
            WHERE check_id = $1
            ORDER BY finished_at DESC
            LIMIT $2
            """,
            check_id, limit,
        )
        return [dict(r) for r in rows]

    async def metric_series(
        self, check_id: str, target: str, metric: str, limit: int = 500,
        since=None,
    ) -> list[dict[str, Any]]:
        """Newest-first metric samples. ``since`` returns only newer ones,
        which is what the dashboard's incremental sparkline refresh uses —
        re-sending the whole window every poll would be most of a megabyte
        per tab per minute for data the client already holds."""
        assert self.pool is not None
        rows = await self.pool.fetch(
            """
            SELECT ts, value
            FROM metric_samples
            WHERE check_id = $1 AND target = $2 AND metric = $3
              AND ($5::timestamptz IS NULL OR ts > $5)
            ORDER BY ts DESC
            LIMIT $4
            """,
            check_id, target, metric, limit, since,
        )
        return [{"ts": r["ts"], "value": r["value"]} for r in rows]

    async def latest_per_check_map(self) -> dict[str, dict[str, Any]]:
        """{check_id: latest_run} convenience for the alarm engine."""
        rows = await self.latest_per_check()
        return {r["check_id"]: r for r in rows}

    # ===================================================================
    # Alarms
    # ===================================================================
    async def open_alarm(
        self, *, check_id: str, target: str, stage: str, severity: str,
        opened_at, suppressed_by: str | None, message: str, payload: dict
    ) -> int:
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """
            INSERT INTO alarms
              (check_id, target, stage, severity, opened_at, suppressed_by,
               message, payload)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            RETURNING id
            """,
            check_id, target, stage, severity, opened_at, suppressed_by,
            message, payload,
        )
        return row["id"]

    async def close_alarm(self, alarm_id: int, when) -> None:
        assert self.pool is not None
        await self.pool.execute(
            "UPDATE alarms SET closed_at = $2 WHERE id = $1 AND closed_at IS NULL",
            alarm_id, when,
        )

    async def update_alarm_severity(self, alarm_id: int, severity: str) -> None:
        assert self.pool is not None
        await self.pool.execute(
            "UPDATE alarms SET severity = $2 WHERE id = $1",
            alarm_id, severity,
        )

    async def non_pass_streak_start(self, check_id: str, target: str):
        """When the current unbroken run of non-pass results began.

        Returns None if the most recent result was pass/skip — i.e. there is
        no streak in progress. Used by the alarm engine's hold-down: a
        condition has to persist before it is worth calling an alarm.

        Derived from check_runs rather than tracked in memory on purpose. An
        in-process counter resets on every deploy, and this project deploys
        often — a restart would forgive every in-flight streak and re-open
        alarms that had already been held down. Reading the history means the
        hold-down survives restarts, which is exactly when a genuine outage is
        most likely to be in progress.

        An INCONCLUSIVE skip does not break the streak. The scheduler demotes
        fail/error to skip when a dependency is unhealthy; treating that as a
        good run would restart the hold-down clock every time an upstream
        blipped, so a permanently-broken target re-qualifies for a brand-new
        alarm a few minutes after every blip. See alarms.suppression.

        Served by idx_run_check_finished (check_id, finished_at DESC).
        """
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """
            SELECT min(finished_at) AS started
            FROM check_runs
            WHERE check_id = $1 AND target = $2
              AND finished_at > coalesce(
                    (SELECT max(finished_at) FROM check_runs
                     WHERE check_id = $1 AND target = $2
                       AND (status = 'pass'
                            OR (status = 'skip'
                                AND coalesce(payload->>'reason', '')
                                      <> ALL($3::text[])))),
                    '-infinity'::timestamptz)
            """,
            check_id, target, list(INCONCLUSIVE_SKIP_REASONS),
        )
        return row["started"] if row else None

    async def find_open_alarm(self, check_id: str, target: str) -> dict | None:
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """
            SELECT * FROM alarms
            WHERE check_id = $1 AND target = $2 AND closed_at IS NULL
            ORDER BY opened_at DESC LIMIT 1
            """,
            check_id, target,
        )
        return dict(row) if row else None

    async def list_open_alarms(self) -> list[dict]:
        assert self.pool is not None
        rows = await self.pool.fetch(
            "SELECT * FROM alarms WHERE closed_at IS NULL ORDER BY opened_at DESC"
        )
        return [dict(r) for r in rows]

    async def list_alarms(
        self, *, status: str = "open", limit: int = 200,
    ) -> list[dict]:
        """LEFT JOIN the most-recent un-revoked ack so the dashboard can
        render acked-but-still-open alarms differently from un-acked ones
        without an extra round-trip per row."""
        assert self.pool is not None
        if status == "open":
            where = "WHERE a.closed_at IS NULL"
        elif status == "closed":
            where = "WHERE a.closed_at IS NOT NULL"
        else:
            where = ""
        rows = await self.pool.fetch(
            f"""
            SELECT a.*,
                   ak.acked_by, ak.acked_at, ak.note AS ack_note
            FROM alarms a
            LEFT JOIN LATERAL (
                SELECT acked_by, acked_at, note
                FROM alarm_acks
                WHERE alarm_id = a.id AND revoked_at IS NULL
                ORDER BY acked_at DESC
                LIMIT 1
            ) ak ON true
            {where}
            ORDER BY a.opened_at DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]

    async def fetch_alarm(self, alarm_id: int) -> dict | None:
        assert self.pool is not None
        row = await self.pool.fetchrow("SELECT * FROM alarms WHERE id=$1", alarm_id)
        return dict(row) if row else None

    # ===================================================================
    # Acks
    # ===================================================================
    async def ack_alarm(self, alarm_id: int, user: str, note: str | None) -> None:
        assert self.pool is not None
        await self.pool.execute(
            """
            INSERT INTO alarm_acks (alarm_id, acked_by, acked_at, note)
            VALUES ($1, $2, now(), $3)
            """,
            alarm_id, user, note,
        )

    async def unack_alarm(self, alarm_id: int) -> None:
        assert self.pool is not None
        await self.pool.execute(
            """
            UPDATE alarm_acks SET revoked_at = now()
            WHERE alarm_id = $1 AND revoked_at IS NULL
            """,
            alarm_id,
        )

    async def is_acked(self, alarm_id: int) -> bool:
        assert self.pool is not None
        v = await self.pool.fetchval(
            """
            SELECT 1 FROM alarm_acks
            WHERE alarm_id = $1 AND revoked_at IS NULL
            LIMIT 1
            """,
            alarm_id,
        )
        return bool(v)

    async def ack_state(self, alarm_id: int) -> dict | None:
        assert self.pool is not None
        row = await self.pool.fetchrow(
            """
            SELECT acked_by, acked_at, note
            FROM alarm_acks
            WHERE alarm_id = $1 AND revoked_at IS NULL
            ORDER BY acked_at DESC LIMIT 1
            """,
            alarm_id,
        )
        return dict(row) if row else None

    # ===================================================================
    # Silences
    # ===================================================================
    async def create_silence(
        self, *, sid: str, matchers: dict, starts, ends,
        reason: str | None, created_by: str | None,
    ) -> None:
        assert self.pool is not None
        await self.pool.execute(
            """
            INSERT INTO silences (id, matchers, starts, ends, reason, created_at, created_by)
            VALUES ($1, $2, $3, $4, $5, now(), $6)
            ON CONFLICT (id) DO UPDATE SET
              matchers = EXCLUDED.matchers,
              starts = EXCLUDED.starts,
              ends = EXCLUDED.ends,
              reason = EXCLUDED.reason
            """,
            sid, matchers, starts, ends, reason, created_by,
        )

    async def delete_silence(self, sid: str) -> None:
        assert self.pool is not None
        await self.pool.execute("DELETE FROM silences WHERE id=$1", sid)

    async def list_active_silences(self, now) -> list[dict]:
        assert self.pool is not None
        rows = await self.pool.fetch(
            "SELECT * FROM silences WHERE starts <= $1 AND ends >= $1 ORDER BY starts",
            now,
        )
        return [dict(r) for r in rows]

    async def list_silences(self) -> list[dict]:
        assert self.pool is not None
        rows = await self.pool.fetch("SELECT * FROM silences ORDER BY starts DESC")
        return [dict(r) for r in rows]

    # ===================================================================
    # Notification log
    # ===================================================================
    async def write_notification(
        self, *, alarm_id: int, receiver: str, channel: str, step_idx: int,
        template: str | None, body_excerpt: str | None,
        status: str, error: str | None,
    ) -> None:
        assert self.pool is not None
        await self.pool.execute(
            """
            INSERT INTO notification_log
              (alarm_id, sent_at, receiver, channel, escalation_step,
               template, body_excerpt, delivery_status, error)
            VALUES ($1, now(), $2, $3, $4, $5, $6, $7, $8)
            """,
            alarm_id, receiver, channel, step_idx, template, body_excerpt, status, error,
        )

    async def notification_count_for_step(self, alarm_id: int, step_idx: int) -> int:
        assert self.pool is not None
        return await self.pool.fetchval(
            """
            SELECT count(*) FROM notification_log
            WHERE alarm_id=$1 AND escalation_step=$2
            """,
            alarm_id, step_idx,
        )

    async def last_notification_at(self, alarm_id: int, step_idx: int):
        assert self.pool is not None
        return await self.pool.fetchval(
            """
            SELECT max(sent_at) FROM notification_log
            WHERE alarm_id=$1 AND escalation_step=$2
            """,
            alarm_id, step_idx,
        )

    async def list_notifications(
        self, alarm_id: int | None = None, limit: int = 200,
    ) -> list[dict]:
        assert self.pool is not None
        if alarm_id is not None:
            rows = await self.pool.fetch(
                """
                SELECT * FROM notification_log
                WHERE alarm_id=$1 ORDER BY sent_at DESC LIMIT $2
                """,
                alarm_id, limit,
            )
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM notification_log ORDER BY sent_at DESC LIMIT $1",
                limit,
            )
        return [dict(r) for r in rows]
