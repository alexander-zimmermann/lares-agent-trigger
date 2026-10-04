"""The `agent_runs` table: claim a subject, count the day, close the row — and the memory.

This service is the ledger's only writer, and the ledger's unique key on
(use_case, subject_kind, subject_key) is also the trigger's dedupe key: the row
is inserted *before* the run is started, so two deliveries of one event cannot
become two runs. A conflict is therefore not an error but the answer "somebody
already has this one".

A run the harness started on its own — a chat turn, a cron execution — is
already over when the trigger hears of it, so its row is written closed, in
one statement, and the same key keeps a replayed delivery from a second one.

`agent_memory` holds one row of notes per use case, which this service alone
appends to; and a run a person asks for on an episode reads that episode from
the engine's table, the one table here this service only reads.
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

from . import memory
from .config import Settings

SubjectKind = Literal["episode", "alert_group", "chat", "none"]
TriggerKind = Literal["event", "schedule", "message", "manual"]
# How a row ends that a run reached the end of.
ClosedStatus = Literal["completed", "failed"]
# Where an output with a state stands: a pull request is open, merged or closed.
OutputState = Literal["open", "merged", "closed"]

_Pool = AsyncConnectionPool[psycopg.AsyncConnection[DictRow]]


@dataclass(frozen=True)
class ToolUse:
    """One tool the model asked for: its name and its arguments, never its result."""

    name: str
    arguments: str


@dataclass(frozen=True)
class CallTrace:
    """One call to the model inside a run: what it read, wrote, took and asked for."""

    number: int
    tokens_in: int | None
    tokens_out: int | None
    seconds: float
    tools: tuple[ToolUse, ...]


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
    # The model calls in order, when the harness's hook reported them.
    calls: tuple[CallTrace, ...] = ()


@dataclass(frozen=True)
class Episode:
    """An episode as the engine recorded it, in what a run on it needs."""

    episode_id: int
    fault: str
    # The channel, room or plant it was measured on.
    subject: str
    severity: int


@dataclass(frozen=True)
class AbandonedRow:
    """A row no event will come back for that a stopped pod left open, as it was closed."""

    id: int
    use_case: str
    subject_kind: SubjectKind
    subject_key: str
    attempt: int


@dataclass(frozen=True)
class OpenOutput:
    """One output the ledger holds as `open`: its row, its position and its ref."""

    run_id: int
    use_case: str
    # 1-based, as Postgres counts array positions.
    position: int
    ref: str


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

    async def runs_today(self, use_case: str, *, excluding: int | None = None) -> int:
        """How many runs this use case has already spent today.

        Capped rows do not count — an event refused for the day must not push
        the next one further away — and neither do hand-fed ones, which asked
        no model. A row just claimed is excluded by id, so the count is of
        runs that came before it.
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
                      AND trigger <> 'manual'
                      AND id IS DISTINCT FROM %s
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
        target sees it.
        """
        async with self._require_pool.connection() as conn:
            await conn.execute(
                """
                UPDATE agent_runs SET
                    tldr = %s, text = %s, model_source = %s, model = %s, tokens_in = %s,
                    tokens_out = %s, cost = %s, duration = %s, tool_trace = %s
                WHERE id = %s
                """,
                (*_output_columns(text, usage), run_id),
            )

    async def finish(
        self,
        run_id: int,
        *,
        status: ClosedStatus,
        error: str | None = None,
        output_ref: Sequence[str] = (),
        output_state: Sequence[OutputState | None] = (),
    ) -> None:
        """Close the row, with one `output_ref` entry per thing a delivery created.

        `output_state` stands beside the refs, position for position: a pull
        request starts `open`; a message or a mail has no state to follow,
        and its position is NULL.
        """
        async with self._require_pool.connection() as conn:
            await conn.execute(
                """
                UPDATE agent_runs SET
                    status = %s, finished_at = now(), error = %s,
                    output_ref = %s::text[], output_state = %s::text[]
                WHERE id = %s
                """,
                (status, error, list(output_ref), list(output_state), run_id),
            )

    async def record_turn(
        self,
        *,
        use_case: str,
        trigger: TriggerKind,
        subject_kind: SubjectKind,
        subject_key: str,
        session_id: str | None,
        harness_run_id: str | None,
        status: ClosedStatus | Literal["running"],
        language: str,
        text: str | None,
        error: str | None,
        usage: Usage,
    ) -> int | None:
        """Write the row of a run that is already over; None when its key exists.

        A turn the harness ran on its own, or a hand-fed run. The row is
        closed, unless it is `running` because its text still has to be
        delivered, which then closes it.
        """
        async with self._require_pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                    INSERT INTO agent_runs
                        (use_case, trigger, subject_kind, subject_key, session_id,
                         harness_run_id, status, language, error, finished_at,
                         tldr, text, model_source, model, tokens_in, tokens_out, cost,
                         duration, tool_trace)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s::text = 'running' THEN NULL ELSE now() END,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (use_case, subject_kind, subject_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        use_case,
                        trigger,
                        subject_kind,
                        subject_key,
                        session_id,
                        harness_run_id,
                        status,
                        language,
                        error,
                        status,
                        *_output_columns(text, usage),
                    ),
                )
            ).fetchall()
        return int(rows[0]["id"]) if rows else None

    async def close_abandoned(self, error: str) -> list[AbandonedRow]:
        """Close as failed every row still open that no event comes back for; the rows closed.

        A run a person asked for, a cron run and a hand-fed run are started by
        no event, so no redelivery ever finds their rows again; the last two
        are open only while their text is being delivered. Chat rows are
        written closed and never match.
        """
        async with self._require_pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                    UPDATE agent_runs SET status = 'failed', finished_at = now(), error = %s
                    WHERE trigger <> 'event' AND status IN ('queued', 'running')
                    RETURNING id, use_case, subject_kind, subject_key, attempt
                    """,
                    (error,),
                )
            ).fetchall()
        return [
            AbandonedRow(
                id=int(row["id"]),
                use_case=str(row["use_case"]),
                subject_kind=row["subject_kind"],
                subject_key=str(row["subject_key"]),
                attempt=int(row["attempt"]),
            )
            for row in rows
        ]

    async def open_outputs(self) -> list[OpenOutput]:
        """Every output whose state the ledger holds as `open`, oldest row first."""
        async with self._require_pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT r.id, r.use_case, o.position, o.ref
                    FROM agent_runs r,
                         unnest(r.output_ref, r.output_state)
                             WITH ORDINALITY AS o(ref, state, position)
                    WHERE o.state = 'open'
                    ORDER BY r.id, o.position
                    """
                )
            ).fetchall()
        return [
            OpenOutput(
                run_id=int(row["id"]),
                use_case=str(row["use_case"]),
                position=int(row["position"]),
                ref=str(row["ref"]),
            )
            for row in rows
        ]

    async def set_output_state(self, run_id: int, position: int, state: OutputState) -> None:
        """Set the state of one output, by its row and its position."""
        async with self._require_pool.connection() as conn:
            await conn.execute(
                "UPDATE agent_runs SET output_state[%s] = %s WHERE id = %s",
                (position, state, run_id),
            )

    async def open_request(self, use_case: str, episode_id: int) -> int | None:
        """The run a person asked for on this episode that is still queued or running."""
        async with self._require_pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT id FROM agent_runs
                    WHERE use_case = %s AND trigger = 'message' AND subject_kind = 'episode'
                      AND split_part(subject_key, ':', 1) = %s
                      AND status IN ('queued', 'running')
                    """,
                    (use_case, str(episode_id)),
                )
            ).fetchall()
        return int(rows[0]["id"]) if rows else None

    async def episode(self, episode_id: int) -> Episode | None:
        """The episode of this id in the engine's table; None when there is none."""
        async with self._require_pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id, fault, subject, severity FROM episodes WHERE id = %s",
                    (episode_id,),
                )
            ).fetchall()
        if not rows:
            return None
        row = rows[0]
        return Episode(
            episode_id=int(row["id"]),
            fault=str(row["fault"]),
            subject=str(row["subject"]),
            severity=int(row["severity"]),
        )

    async def append_memory(self, use_case: str, note: str) -> int:
        """Append a note to the use case's memory, cut to its bound; the bytes it now holds.

        Read and written under a row lock, so two notes at once both land.
        """
        async with self._require_pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO agent_memory (use_case, text) VALUES (%s, '')"
                " ON CONFLICT (use_case) DO NOTHING",
                (use_case,),
            )
            rows = await (
                await conn.execute(
                    "SELECT text FROM agent_memory WHERE use_case = %s FOR UPDATE", (use_case,)
                )
            ).fetchall()
            text = memory.appended(str(rows[0]["text"]), note)
            await conn.execute(
                "UPDATE agent_memory SET text = %s, updated_at = now() WHERE use_case = %s",
                (text, use_case),
            )
        return len(text.encode("utf-8"))


def _output_columns(text: str | None, usage: Usage) -> tuple[object, ...]:
    """What a run produced and cost, in column order from `tldr` to `tool_trace`.

    `tldr` is the first line of the text — the sentence an answer opens with —
    and the tool trace holds the tool count and, where the hook reported
    them, the model calls in order with the tools each asked for. Never a raw
    result.
    """
    tldr = text.strip().splitlines()[0] if text and text.strip() else None
    duration = (
        timedelta(seconds=usage.duration_seconds) if usage.duration_seconds is not None else None
    )
    trace: dict[str, object] = {}
    if usage.tool_count is not None:
        trace["tool_count"] = usage.tool_count
    if usage.calls:
        trace["calls"] = [
            {
                "call": call.number,
                "tokens_in": call.tokens_in,
                "tokens_out": call.tokens_out,
                "seconds": call.seconds,
                "tools": [{"name": t.name, "arguments": t.arguments} for t in call.tools],
            }
            for call in usage.calls
        ]
    return (
        tldr,
        text,
        usage.model_source,
        usage.model,
        usage.tokens_in,
        usage.tokens_out,
        usage.cost,
        duration,
        Jsonb(trace) if trace else None,
    )
