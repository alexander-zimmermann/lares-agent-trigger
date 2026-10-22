"""The chat and cron runs of the harness, from their hook deliveries to a closed ledger row.

The harness runs these on its own — a person writing in Discord, a cron job
coming due — so the trigger hears of them only as they happen. Each call to
the model inside a turn, and each tool it ran, is added to that turn's tally;
the turn's end writes the row with what the tally holds: the tokens, the model
and its source, the tools asked for, the time from the first call to the last,
and the last call's reply as the answer.

Whose run a turn was is decided at its end, by the profile it ran in and the
platform it came in on. The default profile is the machines': its cron
execution belongs to the schedule use case its managed job is named after,
started by its schedule — or by a person, when the chat asked for the job to
run now and this is the job's first turn since
(:meth:`TurnRuns.expect_requested`); its API-server turn is a run this service
started itself and whose row it writes on the event path, so its tally is kept
for that path to collect by session id (:meth:`TurnRuns.turn_of`). A person's
profile is their chat: a Discord turn is the chat use case's row on surface
`discord`, an API-server turn one on surface `web`, the web chat. Each names
the person by the handle, and keeps the answer's text only for a person who
keeps answers. A machine's row names nobody.

Three turns should not come at all, and each is counted and said: a turn of
a profile no enabled person runs on is a chat row under the profile's name,
with the numbers only; a Discord turn on the default profile, while the chat
is the persons', is one of nobody, with the numbers only; a cron turn on any
profile but the default gets no row, as no use case owns it. Without a person
on Discord the default profile serves the chat, and its turns are rows of
nobody that keep their text. The rest is left alone.

After a chat answer the harness may fork a background review of the
profile notes and skills into the answer's session. Its calls run under a task
of their own and its end never comes, so each is booked at once on the
session's newest chat row, in the review columns, priced like any call; its
calls and tools join that answer's trace. The row stays one answered message.

Each turn with a row is one trace (`spans.py`), its root started before the
row is written so the row can name it. A turn that never ended is a trace too:
a sweep gives its tally up after an hour and counts its calls as orphaned.

A chat turn is answered by the harness in its conversation. A cron turn of a
use case that delivers somewhere — a pull request, a page — is delivered here,
after the hook has been answered: its row is written `running` with the text,
and the delivery closes it as an event run's would, with AgentRunFailed when a
target refuses.

The tallies live in memory. A turn whose calls came in before a restart and
whose end came after gets its row without them, never with a guess; a review
that follows a restart is booked, but not traced.

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
from .ledger import (
    CallTrace,
    ChatRow,
    ClosedStatus,
    Ledger,
    SubjectKind,
    Surface,
    TriggerKind,
    Usage,
)
from .metrics import Metrics
from .persons import DEFAULT_PROFILE, PersonsFile, enduser_of
from .pricing import Pricing
from .spans import TURN_FAILED, TURN_WITHOUT_END, TurnTrace
from .use_cases import UseCase, chat_use_case, scheduled_use_case

logger = logging.getLogger(__name__)

Outcome = Literal[
    "counted", "recorded", "booked", "handed_over", "duplicate", "orphaned", "ignored"
]
# What was odd about a turn that should not have come the way it did.
OddTurn = Literal["undeclared_profile", "discord_on_default", "cron_on_person"]

# The surface a person writes on, the one the harness's cron runs as, and the
# one this service's own runs come in through.
_CHAT_PLATFORM = "discord"
_CRON_PLATFORM = "cron"
_API_PLATFORM = "api_server"
_TALLIED_PLATFORMS = (_CHAT_PLATFORM, _CRON_PLATFORM, _API_PLATFORM)
# Where a person wrote, by the platform their turn came in on.
_SURFACES: dict[str, Surface] = {_CHAT_PLATFORM: "discord", _API_PLATFORM: "web"}
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
    def profile(self) -> str:
        return self.calls[0].profile

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
        return _usage(self.calls)


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
class _Whom:
    """Whom a run answered, where they wrote, and what its row keeps."""

    # The person's handle, an undeclared profile's name, or nobody.
    enduser_id: str | None = None
    surface: Surface | None = None
    # Whether the row keeps the answer's text and the tools' arguments.
    keep_answers: bool = False
    # What was odd about the turn, if it should not have come this way.
    odd: OddTurn | None = None


# A machine's run: it answered nobody and keeps its text.
_MACHINES = _Whom(keep_answers=True)


@dataclass(frozen=True)
class _Answer:
    """A chat row this process wrote, and the trace it is in."""

    run_id: int
    trace: TurnTrace
    written: float = field(default_factory=time.monotonic)


@dataclass
class _Review:
    """A background review: each of its calls booked on the row its answer was written to."""

    # None when no row holds the session.
    row: ChatRow | None
    keep_answers: bool
    # The answer's trace; None when its row was written before a restart.
    trace: TurnTrace | None
    # The calls already seen by number, and the tools traced by call id:
    # what tells a redelivery from the next one.
    seen: set[int] = field(default_factory=set)
    tools: set[str] = field(default_factory=set)
    touched: float = field(default_factory=time.monotonic)

    def holds(self, call: ModelCall) -> bool:
        """Whether the call was seen already: a redelivery."""
        return call.number in self.seen

    def add(self, call: ModelCall) -> None:
        self.seen.add(call.number)
        self.touched = time.monotonic()

    def add_tool(self, call: ToolCall) -> bool:
        """Note a tool for the trace; False when it is a redelivery."""
        if call.call_id is not None:
            if call.call_id in self.tools:
                return False
            self.tools.add(call.call_id)
        self.touched = time.monotonic()
        return True


@dataclass(frozen=True)
class _Owner:
    """Whose run a turn was, and how the ledger names its subject."""

    use_case: UseCase
    trigger: TriggerKind
    subject_kind: SubjectKind
    subject_key: str
    whom: _Whom = _MACHINES


class TurnRuns:
    """The hook path, wired to the things it talks to and the prices it charges."""

    def __init__(
        self,
        use_cases: dict[str, UseCase],
        persons: PersonsFile,
        ledger: Ledger,
        hermes: HermesClient,
        closer: Closer,
        pricing: Pricing,
        metrics: Metrics,
    ) -> None:
        self._use_cases = use_cases
        self._persons = persons
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
        # The newest chat row of each session, by session id, and the
        # background reviews going on, by their turn id.
        self._answers: dict[str, _Answer] = {}
        self._reviews: dict[str, _Review] = {}

    async def handle(self, hook: ModelCall | ToolCall | TurnEnded) -> Outcome:
        """Add a call or a tool to its turn's tally, or write the row of a turn that ended."""
        if isinstance(hook, ModelCall):
            session_id = hook.session_id
            # A chat turn's task is its session; the harness forks a background
            # review into the session without one, so its calls run under their own.
            if session_id is not None and hook.task_id not in (None, session_id):
                whom = self._chat_of(hook.profile, hook.platform)
                if whom is not None:
                    return await self._book(hook, session_id, whom)
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

    async def keep_sweeping(self, interval_seconds: float) -> None:
        """Give up what no turn end came for, once every interval."""
        while True:
            await asyncio.sleep(interval_seconds)
            self._forget_stale()

    def _count(self, call: ModelCall) -> Outcome:
        if call.platform not in _TALLIED_PLATFORMS:
            return "ignored"
        tally = self._tallies.get(call.turn_id)
        if tally is None:
            self._tallies[call.turn_id] = _Tally([call])
            return "counted"
        return "counted" if tally.add(call) else "duplicate"

    async def _book(self, call: ModelCall, session_id: str, whom: _Whom) -> Outcome:
        """Add a review's call at once to the row of the answer that set the review off.

        A review in a session no row holds — its answer's end never came
        through — is in no row, and its calls are counted as orphaned.
        """
        review = self._reviews.get(call.turn_id)
        if review is None:
            review = self._reviews[call.turn_id] = await self._open_review(call, session_id, whom)
        if review.holds(call):
            return "duplicate"
        if review.row is None:
            review.add(call)
            self._metrics.orphaned_calls.labels(enduser_id=enduser_of(call.profile) or "").inc()
            return "orphaned"
        usage = self._pricing.price(call.profile, _usage([call]))
        await self._ledger.add_review(
            review.row.id,
            tokens_in=call.tokens_in,
            tokens_out=call.tokens_out,
            cost=usage.cost,
            text=call.content if review.keep_answers else None,
            continued=bool(review.seen),
        )
        review.add(call)
        self._pricing.count(review.row.use_case, call.profile, usage, answered=False)
        if review.trace is not None:
            review.trace.join(calls=[call])
        return "booked"

    async def _open_review(self, call: ModelCall, session_id: str, whom: _Whom) -> _Review:
        """The review a first call opens: on the session's newest chat row, in its trace."""
        row = await self._ledger.newest_chat_row(session_id)
        if row is None:
            logger.warning(
                "turn %s is a background review in session %s, which no row holds:"
                " its calls are in no row",
                call.turn_id,
                session_id,
                extra={"enduser.id": whom.enduser_id} if whom.enduser_id is not None else None,
            )
            return _Review(None, whom.keep_answers, None)
        answer = self._answers.get(session_id)
        # After a restart the answer's root is gone: the calls are booked, never traced.
        joined = answer.trace if answer is not None and answer.run_id == row.id else None
        with joined.current() if joined is not None else contextlib.nullcontext():
            logger.info("turn %s is a background review of run %d", call.turn_id, row.id)
        return _Review(row, whom.keep_answers, joined)

    def _count_tool(self, call: ToolCall) -> Outcome:
        """Add a tool to the tally its turn's model call opened, or a review's to its trace.

        A tool of neither is not ours.
        """
        if call.turn_id is None:
            return "ignored"
        tally = self._tallies.get(call.turn_id)
        if tally is not None:
            return "counted" if tally.add_tool(call) else "duplicate"
        review = self._reviews.get(call.turn_id)
        if review is None or review.trace is None:
            return "ignored"
        if not review.add_tool(call):
            return "duplicate"
        review.trace.join(tools=[call])
        return "counted"

    async def _record(self, turn: TurnEnded) -> Outcome:
        if turn.profile == DEFAULT_PROFILE and turn.platform == _API_PLATFORM:
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
            enduser_id=owner.whom.enduser_id,
            surface=owner.whom.surface,
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
            enduser_id=owner.whom.enduser_id,
            surface=owner.whom.surface,
            keep_answers=owner.whom.keep_answers,
        )
        self._tallies.pop(turn.turn_id, None)
        if turn.cron_run is not None:
            self._requested.pop(turn.cron_run.job_id, None)
        if run_id is None:
            return "duplicate"
        turn_trace.finish(
            run_id=run_id,
            ended_at=reported.ended_at,
            calls=reported.calls,
            tools=reported.tools,
            error_type=None if turn.completed else TURN_FAILED,
        )
        if owner.subject_kind == "chat":
            # What a background review after this answer is booked on and traced under.
            self._answers[turn.session_id] = _Answer(run_id, turn_trace)
        # Inside the turn's trace, so the pricing's and the delivery's log lines carry it too.
        with turn_trace.current():
            self._pricing.count(owner.use_case.name, turn.profile, usage, answered=turn.completed)
            if owner.whom.odd is not None:
                self._odd(owner.whom.odd, turn, run_id)
            logger.info(
                "%s turn %s: run %d %s",
                owner.use_case.name,
                turn.turn_id,
                run_id,
                "recorded, delivering" if delivers else status,
            )
            if not delivers:
                self._metrics.recorded_runs.labels(
                    use_case=owner.use_case.name,
                    status=status,
                    enduser_id=owner.whom.enduser_id or "",
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
        self._metrics.recorded_runs.labels(
            use_case=use_case.name, status=status, enduser_id=""
        ).inc()

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
        self._metrics.recorded_runs.labels(
            use_case=use_case.name, status="failed", enduser_id=""
        ).inc()

    async def _owner(self, turn: TurnEnded) -> _Owner | None:
        """Whose run a turn was, by its profile and its platform."""
        if turn.platform == _CRON_PLATFORM:
            if turn.profile == DEFAULT_PROFILE:
                return await self._cron_owner(turn)
            # Only the default profile runs the declared jobs, so no use case owns it.
            person = self._persons.enabled_person(turn.profile)
            self._odd("cron_on_person" if person is not None else "undeclared_profile", turn)
            return None
        whom = self._chat_of(turn.profile, turn.platform)
        if whom is None:
            logger.debug(
                "left a %s turn of profile %s alone: not a chat and not a cron run",
                turn.platform,
                turn.profile,
            )
            return None
        return self._chat_owner(turn, whom)

    def _chat_of(self, profile: str, platform: str) -> _Whom | None:
        """Whose chat a turn of this profile on this platform is; None for one that is no chat."""
        if profile == DEFAULT_PROFILE:
            if platform != _CHAT_PLATFORM:
                return None
            if self._persons.on_discord:
                # The bot routes every message of a person to their profile: this
                # one reached the machines' profile past the routes.
                return _Whom(surface="discord", odd="discord_on_default")
            # Nobody is on Discord: the default profile serves the chat.
            return _Whom(keep_answers=True)
        surface = _SURFACES.get(platform)
        if surface is None:
            return None
        person = self._persons.enabled_person(profile)
        if person is None:
            # Under its name, so whoever it answered can be found, and with the
            # numbers only, so no other person reads it.
            return _Whom(enduser_id=profile, surface=surface, odd="undeclared_profile")
        return _Whom(enduser_id=person.name, surface=surface, keep_answers=person.keep_answers)

    def _chat_owner(self, turn: TurnEnded, whom: _Whom) -> _Owner | None:
        """The chat's row of this turn; an odd turn's keeps the numbers only."""
        use_case = chat_use_case(self._use_cases)
        if use_case is None:
            logger.warning("a %s turn arrived, and no message use case is enabled", turn.platform)
            return None
        return _Owner(
            use_case=use_case,
            trigger="message",
            subject_kind="chat",
            # Session, then turn: the part before the colon finds the conversation.
            subject_key=f"{turn.session_id}:{turn.turn}",
            whom=whom,
        )

    def _odd(self, odd: OddTurn, turn: TurnEnded, run_id: int | None = None) -> None:
        """Count a turn that should not have come the way it did, and say what became of it.

        Outside a turn's trace the line names the profile's person itself.
        """
        enduser_id = enduser_of(turn.profile)
        logger.warning(
            "%s turn %s of profile %s is odd (%s): %s",
            turn.platform,
            turn.turn_id,
            turn.profile,
            odd,
            f"run {run_id}, numbers only" if run_id is not None else "no row",
            extra={"enduser.id": enduser_id} if enduser_id is not None else None,
        )
        self._metrics.odd_turns.labels(kind=odd, enduser_id=enduser_id or "").inc()

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
        """Drop what no turn end came for, and say so: those calls are in no row.

        An answer no review followed and a review that stopped are dropped
        silently: their calls are already booked.
        """
        cutoff = time.monotonic() - _TALLY_LIFETIME_SECONDS
        for turn_id in [key for key, tally in self._tallies.items() if tally.touched < cutoff]:
            tally = self._tallies.pop(turn_id)
            logger.info(
                "turn %s never ended: %d model calls, %s tokens in, in no row",
                turn_id,
                len(tally.calls),
                tally.usage().tokens_in,
            )
            self._metrics.orphaned_calls.labels(enduser_id=enduser_of(tally.profile) or "").inc(
                len(tally.calls)
            )
            self._trace_dropped(tally)
        for session_id in [key for key, (_, at) in self._finished.items() if at < cutoff]:
            del self._finished[session_id]
        for session_id in [key for key, answer in self._answers.items() if answer.written < cutoff]:
            del self._answers[session_id]
        for turn_id in [key for key, review in self._reviews.items() if review.touched < cutoff]:
            del self._reviews[turn_id]
        # A request whose run never came: the job's next turn is its schedule's again.
        for job_id in [key for key, asked in self._requested.items() if asked < cutoff]:
            del self._requested[job_id]

    def _trace_dropped(self, tally: _Tally) -> None:
        """A turn that never ended is a trace of its own, failed as `turn_without_end`.

        Only a chat turn's use case is known without its end, and a person's
        names them as their row would.
        """
        whom = self._chat_of(tally.profile, tally.platform)
        chat = chat_use_case(self._use_cases) if whom is not None else None
        named = whom or _MACHINES
        TurnTrace(
            chat.name if chat is not None else None,
            conversation_id=tally.session_id,
            started_at=tally.started_at,
            context=Context(),
            enduser_id=named.enduser_id,
            surface=named.surface,
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


def _usage(calls: Sequence[ModelCall]) -> Usage:
    """The tokens, the time from the first call's start to the last's end, and who served.

    The last call names who served: a fallback shows up there.
    """
    last = calls[-1]
    tokens_in: int | None = 0
    tokens_out: int | None = 0
    for call in calls:
        tokens_in = _plus(tokens_in, call.tokens_in)
        tokens_out = _plus(tokens_out, call.tokens_out)
    return Usage(
        model_source=last.model_source,
        model=last.model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        duration_seconds=max(call.ended_at for call in calls)
        - min(call.started_at for call in calls),
        tool_count=sum(call.tool_calls for call in calls),
        calls=_call_traces(calls),
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
