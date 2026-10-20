"""The chat and cron runs of the harness, from their hook deliveries to a closed ledger row.

The harness runs these on its own — a person writing in Discord, a cron job
coming due — so the trigger hears of them only as they happen. Each call to
the model inside a turn, and each tool it ran, is added to that turn's tally;
the turn's end writes the row with what the tally holds: the tokens, the model
and its source, the tools asked for, the time from the first call to the last,
and the last call's reply as the answer.

Whose run a turn was is decided at its end. A Discord turn belongs to the chat
use case; a cron execution to the schedule use case its managed job is named
after, started by its schedule — or by a person, when the chat asked for the
job to run now and this is the job's first turn since
(:meth:`TurnRuns.expect_requested`). An API-server turn is a run this service
started itself and whose row it writes on the event path, so its tally is kept
for that path to collect by session id (:meth:`TurnRuns.turn_of`). The rest is
left alone.

Each turn with a row is one trace (`spans.py`), its root started before the
row is written so the row can name it. A turn that never ended is a trace too,
once its tally is given up.

A chat turn is answered by the harness in its conversation. A cron turn of a
use case that delivers somewhere — a pull request, a page — is delivered here,
after the hook has been answered: its row is written `running` with the text,
and the delivery closes it as an event run's would, with AgentRunFailed when a
target refuses.

The tallies live in memory. A turn whose calls came in before a restart and
whose end came after gets its row without them, never with a guess.

Every call is priced at the row's write by the declared step of its profile's
source that served it (`pricing.py`), and the row's cost is their sum.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from opentelemetry.context import Context

from .closing import Closer
from .deliveries import RunOutput
from .hermes import HermesClient
from .hooks import ModelCall, ToolCall, TurnEnded
from .ledger import CallTrace, ClosedStatus, Ledger, SubjectKind, TriggerKind, Usage
from .metrics import Metrics
from .pricing import Pricing
from .spans import TURN_FAILED, TURN_WITHOUT_END, TurnTrace
from .use_cases import UseCase, chat_use_case, scheduled_use_case

logger = logging.getLogger(__name__)

Outcome = Literal["counted", "recorded", "handed_over", "duplicate", "ignored"]

# The surface a person writes on, the one the harness's cron runs as, and the
# one this service's own runs come in through.
_CHAT_PLATFORM = "discord"
_CRON_PLATFORM = "cron"
_API_PLATFORM = "api_server"
_TALLIED_PLATFORMS = (_CHAT_PLATFORM, _CRON_PLATFORM, _API_PLATFORM)
# Why a cron run whose calls came in before a restart has nothing to deliver.
_LOST = "the turn's answer came in before the trigger restarted and was lost with it"
# A tally nobody closed in this long belongs to a turn whose end was lost.
_TALLY_LIFETIME_SECONDS = 3600.0


@dataclass
class _Tally:
    """What one turn reported so far: opened by its first model call, then every call and tool."""

    calls: list[ModelCall]
    tools: list[ToolCall] = field(default_factory=list)
    touched: float = field(default_factory=time.monotonic)

    def add(self, call: ModelCall) -> bool:
        """Add a model call; False when the tally already holds it, a redelivery."""
        if any(held.number == call.number for held in self.calls):
            return False
        self.calls.append(call)
        self.touched = time.monotonic()
        return True

    def add_tool(self, call: ToolCall) -> bool:
        """Add a tool call; False when the tally already holds it, a redelivery."""
        if call.call_id is not None and any(held.call_id == call.call_id for held in self.tools):
            return False
        self.tools.append(call)
        self.touched = time.monotonic()
        return True

    @property
    def session_id(self) -> str | None:
        return self.calls[0].session_id

    @property
    def platform(self) -> str:
        return self.calls[0].platform

    @property
    def started_at(self) -> float:
        return min(call.started_at for call in self.calls)

    @property
    def reported_until(self) -> float:
        """When the last call or tool it reported ended."""
        return max([call.ended_at for call in self.calls] + [tool.ended_at for tool in self.tools])

    @property
    def answer(self) -> str | None:
        """The last call's reply: the turn's answer."""
        return self.calls[-1].content

    def usage(self) -> Usage:
        """The tokens, the time from the first call's start to the last's end, and who served.

        The last call names who served the turn: a fallback shows up there.
        """
        last = self.calls[-1]
        tokens_in: int | None = 0
        tokens_out: int | None = 0
        for call in self.calls:
            tokens_in = _plus(tokens_in, call.tokens_in)
            tokens_out = _plus(tokens_out, call.tokens_out)
        return Usage(
            model_source=last.model_source,
            model=last.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_seconds=max(call.ended_at for call in self.calls) - self.started_at,
            tool_count=sum(call.tool_calls for call in self.calls),
            calls=_call_traces(self.calls),
        )


@dataclass(frozen=True)
class ReportedTurn:
    """What the hook reported of one turn, from its first call's start to its end."""

    calls: tuple[ModelCall, ...]
    tools: tuple[ToolCall, ...]
    started_at: float
    ended_at: float
    completed: bool

    @property
    def call_traces(self) -> tuple[CallTrace, ...]:
        return _call_traces(self.calls)


@dataclass(frozen=True)
class _Owner:
    """Whose run a turn was, and how the ledger names its subject."""

    use_case: UseCase
    trigger: TriggerKind
    subject_kind: SubjectKind
    subject_key: str


class TurnRuns:
    """The hook path, wired to the things it talks to and the prices it charges."""

    def __init__(
        self,
        use_cases: dict[str, UseCase],
        ledger: Ledger,
        hermes: HermesClient,
        closer: Closer,
        pricing: Pricing,
        metrics: Metrics,
    ) -> None:
        self._use_cases = use_cases
        self._ledger = ledger
        self._hermes = hermes
        self._closer = closer
        self._pricing = pricing
        self._metrics = metrics
        # The deliveries of cron runs still going, held so they are neither collected nor lost.
        self._delivering: set[asyncio.Task[None]] = set()
        self._tallies: dict[str, _Tally] = {}
        # Ended API-server turns, by session id, until the event path collects them.
        self._finished: dict[str, tuple[ReportedTurn, float]] = {}
        self._arrivals: dict[str, asyncio.Event] = {}
        # Jobs a person asked to run now, by job id, with when they asked.
        self._requested: dict[str, float] = {}

    async def handle(self, hook: ModelCall | ToolCall | TurnEnded) -> Outcome:
        """Add a call or a tool to its turn's tally, or write the row of a turn that ended."""
        if isinstance(hook, ModelCall):
            return self._count(hook)
        if isinstance(hook, ToolCall):
            return self._count_tool(hook)
        return await self._record(hook)

    async def aclose(self) -> None:
        """Stop the deliveries still going; the next pod closes their rows."""
        for task in self._delivering:
            task.cancel()
        await asyncio.gather(*self._delivering, return_exceptions=True)

    def expect_requested(self, job_id: str) -> None:
        """Note that a person asked this job to run now: its next cron turn is theirs.

        The run-now answer carries no execution id, so the job's first turn
        after the request is taken as the requested one. The note lives in
        memory: a restart before that turn ends leaves its row on `schedule`.
        """
        self._requested[job_id] = time.monotonic()

    async def turn_of(self, session_id: str, *, wait_seconds: float) -> ReportedTurn | None:
        """The turn of the API run held by this session, waiting a moment for its end.

        The run can be over for the Runs API a moment before its turn's end has
        come through the hook; past the wait the run is recorded without it.
        """
        if session_id not in self._finished:
            arrived = self._arrivals.setdefault(session_id, asyncio.Event())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(arrived.wait(), wait_seconds)
            self._arrivals.pop(session_id, None)
        finished = self._finished.pop(session_id, None)
        return finished[0] if finished is not None else None

    def _count(self, call: ModelCall) -> Outcome:
        if call.platform not in _TALLIED_PLATFORMS:
            return "ignored"
        self._forget_stale()
        tally = self._tallies.get(call.turn_id)
        if tally is None:
            self._tallies[call.turn_id] = _Tally([call])
            return "counted"
        return "counted" if tally.add(call) else "duplicate"

    def _count_tool(self, call: ToolCall) -> Outcome:
        """Add a tool to the tally its turn's model call opened; a tool of no tally is not ours."""
        tally = self._tallies.get(call.turn_id) if call.turn_id is not None else None
        if tally is None:
            return "ignored"
        return "counted" if tally.add_tool(call) else "duplicate"

    async def _record(self, turn: TurnEnded) -> Outcome:
        if turn.platform == _API_PLATFORM:
            return self._hand_over(turn)
        owner = await self._owner(turn)
        if owner is None:
            self._tallies.pop(turn.turn_id, None)
            return "ignored"

        # Kept until the row is written: a delivery refused for a retry needs it again.
        tally = self._tallies.get(turn.turn_id)
        reported = _reported(tally, turn)
        status: ClosedStatus = "completed" if turn.completed else "failed"
        usage = tally.usage() if tally is not None else Usage(model=turn.model)
        usage = self._pricing.price(turn.profile, usage)
        text = tally.answer if tally is not None and turn.completed else None
        # A cron run's text goes where its use case delivers; the row stays
        # open until it has. A chat is answered in its own conversation.
        delivers = turn.cron_run is not None and turn.completed and owner.use_case.delivers
        # A turn of its own, never a child of whatever span is current.
        turn_trace = TurnTrace(
            owner.use_case.name,
            conversation_id=turn.session_id,
            started_at=reported.started_at,
            context=Context(),
        )
        run_id = await self._ledger.record_turn(
            use_case=owner.use_case.name,
            trigger=owner.trigger,
            subject_kind=owner.subject_kind,
            subject_key=owner.subject_key,
            session_id=turn.session_id,
            harness_run_id=turn.turn_id,
            status="running" if delivers else status,
            language=owner.use_case.language,
            text=text,
            error=None if turn.completed else turn.exit_reason,
            usage=usage,
            trace_id=turn_trace.trace_id,
        )
        self._tallies.pop(turn.turn_id, None)
        if turn.cron_run is not None:
            self._requested.pop(turn.cron_run.job_id, None)
        if run_id is None:
            return "duplicate"
        self._pricing.count(owner.use_case.name, turn.profile, usage, answered=turn.completed)
        turn_trace.finish(
            run_id=run_id,
            ended_at=reported.ended_at,
            calls=reported.calls,
            tools=reported.tools,
            error_type=None if turn.completed else TURN_FAILED,
        )
        # Inside the turn's trace, so the delivery's log lines carry it too.
        with turn_trace.current():
            logger.info(
                "%s turn %s: run %d %s",
                owner.use_case.name,
                turn.turn_id,
                run_id,
                "recorded, delivering" if delivers else status,
            )
            if not delivers:
                self._metrics.recorded_runs.labels(
                    use_case=owner.use_case.name, status=status
                ).inc()
                return "recorded"
            if text is not None and text.strip():
                delivery = self._deliver(
                    owner.use_case,
                    RunOutput(
                        run_id=run_id,
                        use_case=owner.use_case.name,
                        occasion=None,
                        text=text,
                        usage=usage,
                    ),
                )
            else:
                # The calls carry the answer; a turn whose calls a restart lost has none here.
                delivery = self._fail(
                    owner.use_case,
                    run_id,
                    "the harness completed the run without output" if tally is not None else _LOST,
                )
            task = asyncio.create_task(delivery)
        self._delivering.add(task)
        task.add_done_callback(self._delivering.discard)
        return "recorded"

    async def _deliver(self, use_case: UseCase, output: RunOutput) -> None:
        """Deliver a cron run's text and close its row; nobody waits to hear that it broke."""
        try:
            status, _ = await self._closer.deliver(use_case, output, what=f"run {output.run_id}")
        except Exception:
            logger.exception(
                "%s run %d stopped before its row was closed", use_case.name, output.run_id
            )
            return
        self._metrics.recorded_runs.labels(use_case=use_case.name, status=status).inc()

    async def _fail(self, use_case: UseCase, run_id: int, error: str) -> None:
        """Close a cron run that has no text to deliver, and report it."""
        try:
            await self._closer.fail(
                use_case.name,
                run_id,
                summary=f"{use_case.name} failed on run {run_id}: {error}",
                error=error,
            )
        except Exception:
            logger.exception("%s run %d stopped before its row was closed", use_case.name, run_id)
            return
        self._metrics.recorded_runs.labels(use_case=use_case.name, status="failed").inc()

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
        trigger: TriggerKind = "message" if run.job_id in self._requested else "schedule"
        # Job, then execution: the part before the colon finds every run of the job.
        return _Owner(use_case, trigger, "none", f"{run.job_id}:{run.execution_id}")

    def _hand_over(self, turn: TurnEnded) -> Outcome:
        """Keep an ended API-server turn for the event run that started it."""
        tally = self._tallies.pop(turn.turn_id, None)
        self._finished[turn.session_id] = (_reported(tally, turn), time.monotonic())
        arrived = self._arrivals.get(turn.session_id)
        if arrived is not None:
            arrived.set()
        return "handed_over"

    def _forget_stale(self) -> None:
        """Drop what no turn end came for, and say so: those calls are in no row."""
        cutoff = time.monotonic() - _TALLY_LIFETIME_SECONDS
        for turn_id in [key for key, tally in self._tallies.items() if tally.touched < cutoff]:
            tally = self._tallies.pop(turn_id)
            logger.info(
                "turn %s never ended: %d model calls, %s tokens in, in no row",
                turn_id,
                len(tally.calls),
                tally.usage().tokens_in,
            )
            self._metrics.orphaned_calls.inc(len(tally.calls))
            self._trace_dropped(tally)
        for session_id in [key for key, (_, at) in self._finished.items() if at < cutoff]:
            del self._finished[session_id]
        # A request whose run never came: the job's next turn is its schedule's again.
        for job_id in [key for key, asked in self._requested.items() if asked < cutoff]:
            del self._requested[job_id]

    def _trace_dropped(self, tally: _Tally) -> None:
        """A turn that never ended is a trace of its own, failed as `turn_without_end`.

        Only a chat turn's use case is known without its end.
        """
        chat = chat_use_case(self._use_cases) if tally.platform == _CHAT_PLATFORM else None
        TurnTrace(
            chat.name if chat is not None else None,
            conversation_id=tally.session_id,
            started_at=tally.started_at,
            context=Context(),
        ).finish(
            run_id=None,
            ended_at=tally.reported_until,
            calls=tally.calls,
            tools=tally.tools,
            error_type=TURN_WITHOUT_END,
        )


def _reported(tally: _Tally | None, turn: TurnEnded) -> ReportedTurn:
    """The turn as the hook reported it; one whose calls a restart lost starts at its end."""
    return ReportedTurn(
        calls=tuple(tally.calls) if tally is not None else (),
        tools=tuple(tally.tools) if tally is not None else (),
        started_at=tally.started_at if tally is not None else turn.ended_at,
        ended_at=turn.ended_at,
        completed=turn.completed,
    )


def _call_traces(calls: Sequence[ModelCall]) -> tuple[CallTrace, ...]:
    """The model calls as the row's tool trace holds them: never the reply."""
    return tuple(
        CallTrace(
            number=call.number,
            tokens_in=call.tokens_in,
            tokens_out=call.tokens_out,
            seconds=round(call.ended_at - call.started_at, 1),
            tools=call.tools,
            model_source=call.model_source,
            model=call.model,
            tokens_cached=call.tokens_cached,
        )
        for call in calls
    )


def _plus(total: int | None, part: int | None) -> int | None:
    """A sum that stays unknown once one part was."""
    return None if total is None or part is None else total + part
