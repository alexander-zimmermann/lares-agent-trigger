"""The chat and cron runs of the harness, from their hook deliveries to a closed ledger row.

The harness runs these on its own — a person writing in Discord, a cron job
coming due — so the trigger hears of them only as they happen. Each call to
the model inside a turn is added to that turn's tally; the turn's end writes
the row with what the tally holds: the tokens, the model and its source, the
tools asked for, the time from the first call to the last, and the last
call's reply as the answer.

Whose run a turn was is decided at its end. A Discord turn belongs to the chat
use case; a cron execution to the schedule use case its managed job is named
after; the rest is left alone, above all the API runs this service started
itself, which already hold their row.

The tallies live in memory. A turn whose calls came in before a restart and
whose end came after gets its row without them, never with a guess.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from .hermes import HermesClient
from .hooks import ModelCall, TurnEnded
from .ledger import ClosedStatus, Ledger, SubjectKind, TriggerKind, Usage
from .metrics import Metrics
from .use_cases import UseCase, chat_use_case, scheduled_use_case

logger = logging.getLogger(__name__)

Outcome = Literal["counted", "recorded", "duplicate", "ignored"]

# The surface a person writes on, and the one the harness's cron runs as.
_CHAT_PLATFORM = "discord"
_CRON_PLATFORM = "cron"
# A tally nobody closed in this long belongs to a turn whose end was lost.
_TALLY_LIFETIME_SECONDS = 3600.0


@dataclass
class _Tally:
    """What the calls of one turn added up to so far."""

    model: str | None = None
    model_source: str | None = None
    tokens_in: int | None = 0
    tokens_out: int | None = 0
    tool_calls: int = 0
    started_at: float | None = None
    ended_at: float | None = None
    answer: str | None = None
    numbers: set[int] = field(default_factory=set)
    touched: float = field(default_factory=time.monotonic)

    def add(self, call: ModelCall) -> None:
        self.numbers.add(call.number)
        self.touched = time.monotonic()
        # The last call names who served the turn: a fallback shows up here.
        self.model, self.model_source = call.model, call.model_source
        self.tokens_in = _plus(self.tokens_in, call.tokens_in)
        self.tokens_out = _plus(self.tokens_out, call.tokens_out)
        self.tool_calls += call.tool_calls
        if self.started_at is None or call.started_at < self.started_at:
            self.started_at = call.started_at
        if self.ended_at is None or call.ended_at > self.ended_at:
            self.ended_at = call.ended_at
        self.answer = call.content

    def usage(self) -> Usage:
        started, ended = self.started_at, self.ended_at
        return Usage(
            model_source=self.model_source,
            model=self.model,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            duration_seconds=ended - started if started is not None and ended is not None else None,
            tool_count=self.tool_calls,
        )


@dataclass(frozen=True)
class _Owner:
    """Whose run a turn was, and how the ledger names its subject."""

    use_case: UseCase
    trigger: TriggerKind
    subject_kind: SubjectKind
    subject_key: str


class TurnRuns:
    """The hook path, wired to the three things it talks to."""

    def __init__(
        self,
        use_cases: dict[str, UseCase],
        ledger: Ledger,
        hermes: HermesClient,
        metrics: Metrics,
    ) -> None:
        self._use_cases = use_cases
        self._ledger = ledger
        self._hermes = hermes
        self._metrics = metrics
        self._tallies: dict[str, _Tally] = {}

    async def handle(self, hook: ModelCall | TurnEnded) -> Outcome:
        """Add a call to its turn's tally, or write the row of a turn that ended."""
        if isinstance(hook, ModelCall):
            return self._count(hook)
        return await self._record(hook)

    def _count(self, call: ModelCall) -> Outcome:
        if call.platform not in (_CHAT_PLATFORM, _CRON_PLATFORM):
            return "ignored"
        self._forget_stale()
        tally = self._tallies.setdefault(call.turn_id, _Tally())
        # A redelivered call is one the tally already holds.
        if call.number in tally.numbers:
            return "duplicate"
        tally.add(call)
        return "counted"

    async def _record(self, turn: TurnEnded) -> Outcome:
        owner = await self._owner(turn)
        if owner is None:
            self._tallies.pop(turn.turn_id, None)
            return "ignored"

        # Kept until the row is written: a delivery refused for a retry needs it again.
        tally = self._tallies.get(turn.turn_id)
        status: ClosedStatus = "completed" if turn.completed else "failed"
        usage = tally.usage() if tally is not None else Usage(model=turn.model)
        run_id = await self._ledger.record_turn(
            use_case=owner.use_case.name,
            trigger=owner.trigger,
            subject_kind=owner.subject_kind,
            subject_key=owner.subject_key,
            session_id=turn.session_id,
            harness_run_id=turn.turn_id,
            status=status,
            language=owner.use_case.language,
            text=tally.answer if tally is not None and turn.completed else None,
            error=None if turn.completed else turn.exit_reason,
            usage=usage,
        )
        self._tallies.pop(turn.turn_id, None)
        if run_id is None:
            return "duplicate"
        self._metrics.recorded_runs.labels(use_case=owner.use_case.name, status=status).inc()
        return "recorded"

    async def _owner(self, turn: TurnEnded) -> _Owner | None:
        if turn.platform == _CHAT_PLATFORM:
            use_case = chat_use_case(self._use_cases)
            if use_case is None:
                logger.warning(
                    "a %s turn arrived, and no message use case is enabled", turn.platform
                )
                return None
            # Session, then turn: the part before the colon finds the conversation.
            return _Owner(use_case, "message", "chat", f"{turn.session_id}:{turn.turn}")
        if turn.platform == _CRON_PLATFORM:
            return await self._cron_owner(turn)
        logger.debug("left a %s turn alone: not a chat and not a cron run", turn.platform)
        return None

    async def _cron_owner(self, turn: TurnEnded) -> _Owner | None:
        run = turn.cron_run
        if run is None:
            logger.warning("a cron turn with task id %r names no job", turn.task_id)
            return None
        name = await self._hermes.job_name(run.job_id)
        use_case = scheduled_use_case(self._use_cases, name) if name is not None else None
        if use_case is None:
            logger.info("cron job %s (%s) runs no declared use case", run.job_id, name or "gone")
            return None
        # Job, then execution: the part before the colon finds every run of the job.
        return _Owner(use_case, "schedule", "none", f"{run.job_id}:{run.execution_id}")

    def _forget_stale(self) -> None:
        cutoff = time.monotonic() - _TALLY_LIFETIME_SECONDS
        for turn_id in [key for key, tally in self._tallies.items() if tally.touched < cutoff]:
            del self._tallies[turn_id]


def _plus(total: int | None, part: int | None) -> int | None:
    """A sum that stays unknown once one part was."""
    return None if total is None or part is None else total + part
