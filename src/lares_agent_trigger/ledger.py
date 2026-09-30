"""The `agent_runs` table: claim a subject, count the day, close the row.

This service is the ledger's only writer, and the ledger's unique key on
(use_case, subject_kind, subject_key) is also the trigger's dedupe key: the row
is inserted *before* the run is started, so two deliveries of one event cannot
become two runs. A conflict is therefore not an error but the answer "somebody
already has this one".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Literal

import psycopg
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .config import Settings

SubjectKind = Literal["episode", "alert_group", "chat", "none"]
TriggerKind = Literal["event", "schedule", "message", "manual"]

_Pool = AsyncConnectionPool[psycopg.AsyncConnection[DictRow]]


@dataclass(frozen=True)
class Usage:
    """What a finished run reports about itself, as far as the harness knows it."""

    model_source: str | None = None
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost: Decimal | None = None
    duration_seconds: float | None = None
    tool_count: int | None = None


@dataclass(frozen=True)
class OpenRow:
    """A row that holds its subject but was never closed."""

    id: int
    attempt: int


class Ledger:
    """A pool plus the statements the event path needs."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: _Pool | None = None

    async def open(self) -> None:
        pool: _Pool = AsyncConnectionPool(
            conninfo=self._settings.db_dsn,
            min_size=self._settings.db_pool_min,
            max_size=self._settings.db_pool_max,
            kwargs={"autocommit": True, "row_factory": dict_row},
            open=False,
        )
        await pool.open(wait=True, timeout=10.0)
        self._pool = pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def _require_pool(self) -> _Pool:
        if self._pool is None:
            raise RuntimeError("ledger pool not open — call open() first")
        return self._pool

    async def is_reachable(self) -> bool:
        """Round-trip `SELECT 1`; False rather than an exception, for the health probe."""
        try:
            async with self._require_pool.connection() as conn:
                await conn.execute("SELECT 1")
        except Exception:
            return False
        return True

    async def claim(
        self,
        *,
        use_case: str,
        trigger: TriggerKind,
        subject_kind: SubjectKind,
        subject_key: str,
        language: str,
    ) -> int | None:
        """Insert the queued row for this subject; None when it already exists.

        `ON CONFLICT DO NOTHING` on the unique index is the whole dedupe: the
        first delivery gets an id, every later one gets None and is skipped.
        """
        async with self._require_pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                    INSERT INTO agent_runs
                        (use_case, trigger, subject_kind, subject_key, status, language)
                    VALUES (%s, %s, %s, %s, 'queued', %s)
                    ON CONFLICT (use_case, subject_kind, subject_key) DO NOTHING
                    RETURNING id
                    """,
                    (use_case, trigger, subject_kind, subject_key, language),
                )
            ).fetchall()
        return int(rows[0]["id"]) if rows else None

    async def open_row(
        self, *, use_case: str, subject_kind: SubjectKind, subject_key: str
    ) -> OpenRow | None:
        """The row holding this subject if it is still `queued` or `running`.

        With one reader on the consumer, nothing else is working on it while
        this is asked: such a row was left by a pod that died mid-run.
        """
        async with self._require_pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT id, attempt FROM agent_runs
                    WHERE use_case = %s AND subject_kind = %s AND subject_key = %s
                      AND status IN ('queued', 'running')
                    """,
                    (use_case, subject_kind, subject_key),
                )
            ).fetchall()
        return OpenRow(id=int(rows[0]["id"]), attempt=int(rows[0]["attempt"])) if rows else None

    async def runs_today(self, use_case: str, *, excluding: int) -> int:
        """How many runs this use case has already spent today.

        Capped rows do not count — an event refused for the day must not push
        the next one further away. The row just claimed is excluded by id, so
        the count is of runs that came before it.
        """
        # Postgres does the day arithmetic, so a pod on UTC and a psql session
        # agree on where the house's day starts.
        async with self._require_pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT count(*) AS runs FROM agent_runs
                    WHERE use_case = %s
                      AND status <> 'capped'
                      AND id <> %s
                      AND created_at >= date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s
                    """,
                    (use_case, excluding, self._settings.timezone, self._settings.timezone),
                )
            ).fetchall()
        return int(rows[0]["runs"])

    async def mark_capped(self, run_id: int) -> None:
        """Close the row as `capped`: the event is recorded, no run was started."""
        async with self._require_pool.connection() as conn:
            await conn.execute(
                "UPDATE agent_runs SET status = 'capped', finished_at = now() WHERE id = %s",
                (run_id,),
            )

    async def mark_retrying(self, run_id: int, *, attempt: int, error: str) -> None:
        """Put the row back to `queued` for the next attempt, with the error that caused it."""
        async with self._require_pool.connection() as conn:
            await conn.execute(
                """
                UPDATE agent_runs SET status = 'queued', attempt = %s, error = %s,
                    harness_run_id = NULL
                WHERE id = %s
                """,
                (attempt, error, run_id),
            )

    async def mark_running(self, run_id: int, harness_run_id: str) -> None:
        """Note that the harness took the run, so a stuck run is identifiable."""
        async with self._require_pool.connection() as conn:
            await conn.execute(
                "UPDATE agent_runs SET status = 'running', harness_run_id = %s WHERE id = %s",
                (harness_run_id, run_id),
            )

    async def record(self, run_id: int, *, text: str | None, usage: Usage) -> None:
        """Write what the run produced and what it cost; the row stays open.

        This is the `stored` output: the text is in the ledger before any other
        target sees it. `tldr` is the first line of the text — the sentence an
        explanation opens with — and the tool trace holds the call count, never
        a raw result.
        """
        tldr = text.strip().splitlines()[0] if text and text.strip() else None
        duration = (
            timedelta(seconds=usage.duration_seconds)
            if usage.duration_seconds is not None
            else None
        )
        trace: Jsonb | None = (
            Jsonb({"tool_count": usage.tool_count}) if usage.tool_count is not None else None
        )
        async with self._require_pool.connection() as conn:
            await conn.execute(
                """
                UPDATE agent_runs SET
                    tldr = %s, text = %s, model_source = %s, model = %s, tokens_in = %s,
                    tokens_out = %s, cost = %s, duration = %s, tool_trace = %s
                WHERE id = %s
                """,
                (
                    tldr,
                    text,
                    usage.model_source,
                    usage.model,
                    usage.tokens_in,
                    usage.tokens_out,
                    usage.cost,
                    duration,
                    trace,
                    run_id,
                ),
            )

    async def finish(
        self,
        run_id: int,
        *,
        status: Literal["completed", "failed"],
        error: str | None = None,
        output_ref: Sequence[str] = (),
    ) -> None:
        """Close the row, with one `output_ref` entry per thing a delivery created.

        A message or a mail has no state to follow, so its `output_state`
        position is NULL.
        """
        refs = list(output_ref)
        async with self._require_pool.connection() as conn:
            await conn.execute(
                """
                UPDATE agent_runs SET
                    status = %s, finished_at = now(), error = %s,
                    output_ref = %s::text[], output_state = %s::text[]
                WHERE id = %s
                """,
                (status, error, refs, [None] * len(refs), run_id),
            )
