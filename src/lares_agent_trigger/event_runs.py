"""One episode event, from the filter to a closed ledger row.

The order is the contract: match the filter, claim the row (that is the
dedupe), check the day's cap, only then start a run. A run that fails for a
reason that fixes itself is started once more after the retry delay; one that
is still failed after that, or failed for a reason that does not, closes its
row and is posted to Alertmanager. Everything the outside sees — a row, an API
call, a counter, an alert — happens here; the consumer below only hands events
in.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from .alerts import Alertmanager
from .events import EpisodeEvent
from .failures import TRANSIENT, FailureClass, classify_error, classify_exception
from .hermes import HermesClient, HermesError, instructions_for
from .ledger import Ledger, Usage
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


@dataclass(frozen=True)
class EventRuns:
    """The event path, wired to the four things it talks to."""

    use_cases: dict[str, UseCase]
    ledger: Ledger
    hermes: HermesClient
    alertmanager: Alertmanager
    metrics: Metrics
    retry_delay_seconds: float

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
        """Start one run and wait for it; None when it completed and its row is closed."""
        budget = use_case.budget
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
            return _Failure(classify_exception(exc), _describe(exc))

        await self.ledger.mark_running(run_id, harness_run_id)
        try:
            outcome = await self.hermes.await_run(
                harness_run_id, deadline_seconds=budget.minutes * 60
            )
        except (HermesError, httpx.HTTPError, OSError) as exc:
            return _Failure(classify_exception(exc), _describe(exc))

        if outcome.status == "failed":
            error = outcome.error or "the harness failed the run without a reason"
            return _Failure(classify_error(error), error, outcome.usage)

        await self.ledger.finish(
            run_id,
            status="completed",
            text=outcome.output,
            usage=outcome.usage,
        )
        self._count(use_case, "completed", outcome.usage)
        return None

    async def _report(
        self,
        use_case: UseCase,
        event: EpisodeEvent,
        run_id: int,
        attempt: int,
        failure: _Failure,
    ) -> None:
        """Close the row as failed and raise AgentRunFailed with the raw error."""
        logger.error(
            "%s %s failed after %d attempt(s) (%s): %s",
            use_case.name,
            event.subject_key,
            attempt,
            failure.failure_class,
            failure.error,
        )
        await self.ledger.finish(run_id, status="failed", error=failure.error, usage=failure.usage)
        self._count(use_case, "failed", failure.usage)
        await self.alertmanager.run_failed(
            use_case=use_case.name,
            summary=(
                f"{use_case.name} failed on episode {event.subject_key}"
                f" after {attempt} attempt{'s' if attempt > 1 else ''} ({failure.failure_class})"
            ),
            error=failure.error,
        )

    def _count(self, use_case: UseCase, status: str, usage: Usage | None) -> None:
        self.metrics.runs.labels(use_case=use_case.name, status=status).inc()
        if usage is not None and usage.duration_seconds is not None:
            self.metrics.run_duration.labels(use_case=use_case.name).observe(usage.duration_seconds)


def ledger_key(use_case: str, subject_kind: str, subject_key: str) -> str:
    """The ledger's unique key as one string — also the first attempt's idempotency key."""
    return f"{use_case}/{subject_kind}/{subject_key}"


def _describe(exc: Exception) -> str:
    """The exception's own text; some transport errors carry none."""
    return str(exc) or type(exc).__name__
