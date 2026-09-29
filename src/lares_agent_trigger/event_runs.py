"""One episode event, from the filter to a closed ledger row.

The order is the contract: match the filter, claim the row (that is the
dedupe), check the day's cap, only then start a run. Everything the outside
sees — a row, an API call, a counter — happens here; the consumer below only
hands events in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .events import EpisodeEvent
from .hermes import HermesClient, HermesError, instructions_for
from .ledger import Ledger
from .metrics import Metrics
from .use_cases import UseCase


@dataclass(frozen=True)
class EventRuns:
    """The event path, wired to the three things it talks to."""

    use_cases: dict[str, UseCase]
    ledger: Ledger
    hermes: HermesClient
    metrics: Metrics

    def matching(self, event: EpisodeEvent) -> list[UseCase]:
        """The enabled event use cases whose filter wants this event."""
        return [
            use_case
            for use_case in self.use_cases.values()
            if (trigger := use_case.event_trigger) is not None
            and trigger.source == "episode"
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
            # The unique key already holds this subject: the event was seen
            # before, and one event never starts two runs. A redelivery after
            # the pod died mid-run therefore leaves that row on `running` —
            # resuming or failing it is the retry path of #2108.
            self.metrics.runs.labels(use_case=use_case.name, status="duplicate").inc()
            return

        spent = await self.ledger.runs_today(use_case.name, excluding=run_id)
        if spent >= use_case.budget.runs_per_day:
            await self.ledger.mark_capped(run_id)
            self.metrics.capped.labels(use_case=use_case.name).inc()
            return

        await self._start_and_await(use_case, event, run_id)

    async def _start_and_await(self, use_case: UseCase, event: EpisodeEvent, run_id: int) -> None:
        budget = use_case.budget
        try:
            harness_run_id = await self.hermes.start_run(
                idempotency_key=ledger_key(use_case.name, "episode", event.subject_key),
                run_input=event.as_input(),
                instructions=instructions_for(
                    use_case.skill, use_case.language, budget.tool_calls, budget.minutes
                ),
            )
        except (HermesError, OSError) as exc:
            # No retry here: one retry and the AgentRunFailed alert are #2108.
            await self.ledger.finish(run_id, status="failed", error=str(exc))
            self.metrics.runs.labels(use_case=use_case.name, status="failed").inc()
            return

        await self.ledger.mark_running(run_id, harness_run_id)
        try:
            outcome = await self.hermes.await_run(
                harness_run_id, deadline_seconds=budget.minutes * 60
            )
        except (HermesError, OSError) as exc:
            await self.ledger.finish(run_id, status="failed", error=str(exc))
            self.metrics.runs.labels(use_case=use_case.name, status="failed").inc()
            return

        status: Literal["completed", "failed"] = "completed" if outcome.completed else "failed"
        await self.ledger.finish(
            run_id,
            status=status,
            text=outcome.output,
            error=outcome.error,
            usage=outcome.usage,
        )
        self.metrics.runs.labels(use_case=use_case.name, status=status).inc()
        if outcome.usage.duration_seconds is not None:
            self.metrics.run_duration.labels(use_case=use_case.name).observe(
                outcome.usage.duration_seconds
            )


def ledger_key(use_case: str, subject_kind: str, subject_key: str) -> str:
    """The ledger's unique key as one string — also the harness's idempotency key."""
    return f"{use_case}/{subject_kind}/{subject_key}"
