"""Async Postgres access. One pool per process.

Codecs: ``payload`` columns are JSONB. We register a codec that handles dict
↔ JSONB in both directions so callers never have to ``json.dumps`` manually.
"""
from __future__ import annotations
import json
import logging
from typing import Any

import asyncpg

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
        assert self.pool is not None
        rows = await self.pool.fetch(
            """
            SELECT DISTINCT ON (check_id)
                id, check_id, target, stage, status, started_at, finished_at,
                summary, payload, artifacts
            FROM check_runs
            ORDER BY check_id, finished_at DESC
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
        self, check_id: str, target: str, metric: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        assert self.pool is not None
        rows = await self.pool.fetch(
            """
            SELECT ts, value
            FROM metric_samples
            WHERE check_id = $1 AND target = $2 AND metric = $3
            ORDER BY ts DESC
            LIMIT $4
            """,
            check_id, target, metric, limit,
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
        assert self.pool is not None
        if status == "open":
            where = "WHERE closed_at IS NULL"
        elif status == "closed":
            where = "WHERE closed_at IS NOT NULL"
        else:
            where = ""
        rows = await self.pool.fetch(
            f"SELECT * FROM alarms {where} ORDER BY opened_at DESC LIMIT $1",
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
