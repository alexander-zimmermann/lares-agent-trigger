"""The last step of every run the trigger closes: deliver its text, then close its row.

An event run, a cron run the hook reported, a hand-fed run: once the text is in
the row, it goes to every target the use case declares, and the row is closed
with what they created. A target that refuses fails the run — the stored text
stays, the refs of what was created stay, and AgentRunFailed goes out with
each refusal's raw answer. Nobody asks the model again for it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .alerts import Alertmanager
from .deliveries import Delivered, Deliveries, RunOutput
from .ledger import ClosedStatus, Ledger
from .metrics import Metrics
from .use_cases import OutputTarget, UseCase

# What a run that failed before its targets were tried delivered.
_NOTHING = Delivered(refs=(), states=(), refusals=())


@dataclass(frozen=True)
class Closer:
    """Delivery and the row it closes, with the alert for a run that failed."""

    ledger: Ledger
    deliveries: Deliveries
    alertmanager: Alertmanager
    metrics: Metrics

    async def deliver(
        self, use_case: UseCase, output: RunOutput, *, what: str
    ) -> tuple[ClosedStatus, Delivered]:
        """Carry the stored text to every declared target and close the row with what they made.

        `what` names the run in the alert's summary: `episode 15510:appeared`, `run 42`.
        """
        delivered = await self.deliveries.deliver(use_case.output, output)
        if not delivered.refusals:
            await self.ledger.finish(
                output.run_id,
                status="completed",
                output_ref=delivered.refs,
                output_state=delivered.states,
            )
            return "completed", delivered
        self.metrics.failures.labels(use_case.name, "delivery_failed").inc()
        await self.fail(
            use_case.name,
            output.run_id,
            summary=f"{use_case.name} could not deliver {what} to {delivered.refused}",
            error=delivered.error,
            delivered=delivered,
        )
        return "failed", delivered

    async def fail(
        self,
        use_case: str,
        run_id: int,
        *,
        summary: str,
        error: str,
        delivered: Delivered = _NOTHING,
    ) -> None:
        """Close the row as failed, keeping what was delivered, and raise AgentRunFailed."""
        await self.ledger.finish(
            run_id,
            status="failed",
            error=error,
            output_ref=delivered.refs,
            output_state=delivered.states,
        )
        await self.report(use_case, summary=summary, error=error)

    async def report(self, use_case: str, *, summary: str, error: str) -> None:
        """Raise AgentRunFailed for a row already closed."""
        await self.alertmanager.run_failed(use_case=use_case, summary=summary, error=error)

    def serves(self, target: OutputTarget) -> bool:
        """True when the trigger is set up to deliver to the target."""
        return self.deliveries.serves(target)
