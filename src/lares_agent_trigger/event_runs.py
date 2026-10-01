"""One episode event, from the filter to a closed ledger row.

The order is the contract: match the filter, claim the row (that is the
dedupe), check the day's cap, only then start a run. A run that fails for a
reason that fixes itself is started once more after the retry delay; one that
is still failed after that, or failed for a reason that does not, closes its
row and is posted to Alertmanager. A run that completed stores its text on the
row and is then carried to every target its use case declares; a target that
refuses fails the run the same way, without asking the model again.
Everything the outside sees — a row, an API call, a message, a mail, a
counter, an alert — happens here; the consumer below only hands events in.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Protocol

import httpx

from .alerts import Alertmanager
from .deliveries import Deliveries, RunOutput
from .events import EpisodeEvent
from .failures import TRANSIENT, FailureClass, classify_error, classify_exception, describe
from .hermes import HermesClient, HermesError, RunOutcome, instructions_for
from .ledger import CallTrace, Ledger, Usage
from .metrics import Metrics
from .use_cases import UseCase

logger = logging.getLogger(__name__)

# The first run and one retry.
_ATTEMPTS = 2


@dataclass(frozen=True)
class _Failure:
    """Why one attempt did not produce an answer, and what it spent getting there."""

    failure_class: FailureClass
    error: str
    usage: Usage | None = None


class CallTraces(Protocol):
    """Where the model calls of a run come from: the hook, by the run's session."""

    async def calls_of(self, session_id: str, *, wait_seconds: float) -> tuple[CallTrace, ...]: ...


@dataclass(frozen=True)
class EventRuns:
    """The event path, wired to the things it talks to."""

    use_cases: dict[str, UseCase]
    ledger: Ledger
    hermes: HermesClient
    alertmanager: Alertmanager
    deliveries: Deliveries
    metrics: Metrics
    retry_delay_seconds: float
    traces: CallTraces
    trace_wait_seconds: float

    def matching(self, event: EpisodeEvent) -> list[UseCase]:
        """The enabled event use cases whose filter wants this event."""
        return [
            use_case
            for use_case in self.use_cases.values()
            if (trigger := use_case.event_trigger) is not None
            and trigger.wants(event.kind, event.severity)
        ]

    async def handle(self, event: EpisodeEvent) -> None:
        """Run every use case this event matches, in declaration order."""
        wanted = self.matching(event)
        self.metrics.events.labels(
            kind=event.kind, outcome="matched" if wanted else "unmatched"
        ).inc()
        for use_case in wanted:
            await self._run(use_case, event)

    async def _run(self, use_case: UseCase, event: EpisodeEvent) -> None:
        run_id = await self.ledger.claim(
            use_case=use_case.name,
            trigger="event",
            subject_kind="episode",
            subject_key=event.subject_key,
            language=use_case.language,
        )
        if run_id is None:
            await self._seen_before(use_case, event)
            return

        spent = await self.ledger.runs_today(use_case.name, excluding=run_id)
        if spent >= use_case.budget.runs_per_day:
            await self.ledger.mark_capped(run_id)
            self.metrics.capped.labels(use_case=use_case.name).inc()
            return

        await self._run_with_retry(use_case, event, run_id)

    async def _seen_before(self, use_case: UseCase, event: EpisodeEvent) -> None:
        """The unique key already holds this subject: a duplicate, or a run a dead pod left open.

        One event never starts two runs, and that holds here too: the harness
        may still be working on the interrupted one, so it is closed and
        reported rather than started again.
        """
        left_open = await self.ledger.open_row(
            use_case=use_case.name, subject_kind="episode", subject_key=event.subject_key
        )
        if left_open is None:
            self.metrics.duplicates.labels(use_case=use_case.name).inc()
            return
        failure = _Failure(
            "trigger_restarted",
            f"the trigger stopped at attempt {left_open.attempt} before the row was closed;"
            " the redelivered event found it still open",
        )
        self.metrics.failures.labels(use_case.name, failure.failure_class).inc()
        await self._report(use_case, event, left_open.id, left_open.attempt, failure)

    async def _run_with_retry(self, use_case: UseCase, event: EpisodeEvent, run_id: int) -> None:
        key = ledger_key(use_case.name, "episode", event.subject_key)
        for attempt in range(1, _ATTEMPTS + 1):
            # The gateway replays the run it already holds for a key, failed or
            # not, so a retry needs a key of its own.
            idempotency_key = key if attempt == 1 else f"{key}/{attempt}"
            failure = await self._attempt(use_case, event, run_id, idempotency_key)
            if failure is None:
                return
            self.metrics.failures.labels(use_case.name, failure.failure_class).inc()
            if attempt < _ATTEMPTS and failure.failure_class in TRANSIENT:
                logger.warning(
                    "%s %s attempt %d failed (%s), retrying in %.0f s: %s",
                    use_case.name,
                    event.subject_key,
                    attempt,
                    failure.failure_class,
                    self.retry_delay_seconds,
                    failure.error,
                )
                await self.ledger.mark_retrying(run_id, attempt=attempt + 1, error=failure.error)
                await asyncio.sleep(self.retry_delay_seconds)
                continue
            await self._report(use_case, event, run_id, attempt, failure)
            return

    async def _attempt(
        self, use_case: UseCase, event: EpisodeEvent, run_id: int, idempotency_key: str
    ) -> _Failure | None:
        """Start one run and wait for it; None when it completed and its row is closed.

        A run that completed is closed here whatever its targets make of it: a
        refused delivery is not a failure the harness could fix by running
        again.
        """
        budget = use_case.budget
        # The loader refuses an event use case that names no skill.
        assert use_case.skill is not None
        try:
            harness_run_id = await self.hermes.start_run(
                idempotency_key=idempotency_key,
                run_input=event.as_input(),
                instructions=instructions_for(
                    use_case.skill, use_case.language, budget.tool_calls, budget.minutes
                ),
                model=use_case.model,
            )
        except (HermesError, httpx.HTTPError, OSError) as exc:
            return _Failure(classify_exception(exc), describe(exc))

        await self.ledger.mark_running(run_id, harness_run_id)
        try:
            outcome = await self.hermes.await_run(
                harness_run_id, deadline_seconds=budget.minutes * 60
            )
        except (HermesError, httpx.HTTPError, OSError) as exc:
            return _Failure(classify_exception(exc), describe(exc))
        outcome = await self._with_calls(outcome)

        if outcome.status == "failed":
            error = outcome.error or "the harness failed the run without a reason"
            return _Failure(classify_error(error), error, outcome.usage)

        if not outcome.output:
            return _Failure(
                "unknown", "the harness completed the run without output", outcome.usage
            )

        await self.ledger.record(run_id, text=outcome.output, usage=outcome.usage)
        await self._deliver(use_case, event, run_id, outcome.output, outcome.usage)
        return None

    async def _with_calls(self, outcome: RunOutcome) -> RunOutcome:
        """The outcome with the model calls its turn reported through the hook, if they came."""
        if outcome.session_id is None:
            return outcome
        calls = await self.traces.calls_of(outcome.session_id, wait_seconds=self.trace_wait_seconds)
        return replace(outcome, usage=replace(outcome.usage, calls=calls))

    async def _deliver(
        self, use_case: UseCase, event: EpisodeEvent, run_id: int, text: str, usage: Usage
    ) -> None:
        """Carry the stored text to every declared target and close the row with what they made."""
        delivered = await self.deliveries.deliver(
            use_case.output,
            RunOutput(run_id=run_id, use_case=use_case.name, event=event, text=text, usage=usage),
        )
        if not delivered.refusals:
            await self.ledger.finish(run_id, status="completed", output_ref=delivered.refs)
            self._count(use_case, "completed", usage)
            return
        self.metrics.failures.labels(use_case.name, "delivery_failed").inc()
        await self._close_failed(
            use_case,
            run_id,
            summary=(
                f"{use_case.name} could not deliver episode {event.subject_key}"
                f" to {delivered.refused}"
            ),
            error=delivered.error,
            usage=usage,
            output_ref=delivered.refs,
        )

    async def _report(
        self,
        use_case: UseCase,
        event: EpisodeEvent,
        run_id: int,
        attempt: int,
        failure: _Failure,
    ) -> None:
        """Record what a failed run spent, if the harness said, and close it as failed.

        A row a dead pod left open keeps whatever it already holds.
        """
        logger.error(
            "%s %s failed after %d attempt(s) (%s): %s",
            use_case.name,
            event.subject_key,
            attempt,
            failure.failure_class,
            failure.error,
        )
        if failure.usage is not None:
            await self.ledger.record(run_id, text=None, usage=failure.usage)
        await self._close_failed(
            use_case,
            run_id,
            summary=(
                f"{use_case.name} failed on episode {event.subject_key}"
                f" after {attempt} attempt{'s' if attempt > 1 else ''} ({failure.failure_class})"
            ),
            error=failure.error,
            usage=failure.usage,
        )

    async def _close_failed(
        self,
        use_case: UseCase,
        run_id: int,
        *,
        summary: str,
        error: str,
        usage: Usage | None,
        output_ref: tuple[str, ...] = (),
    ) -> None:
        """Close the row as failed and raise AgentRunFailed with the raw error."""
        await self.ledger.finish(run_id, status="failed", error=error, output_ref=output_ref)
        self._count(use_case, "failed", usage)
        await self.alertmanager.run_failed(use_case=use_case.name, summary=summary, error=error)

    def _count(self, use_case: UseCase, status: str, usage: Usage | None) -> None:
        self.metrics.runs.labels(use_case=use_case.name, status=status).inc()
        if usage is not None and usage.duration_seconds is not None:
            self.metrics.run_duration.labels(use_case=use_case.name).observe(usage.duration_seconds)


def ledger_key(use_case: str, subject_kind: str, subject_key: str) -> str:
    """The ledger's unique key as one string — also the first attempt's idempotency key."""
    return f"{use_case}/{subject_kind}/{subject_key}"
