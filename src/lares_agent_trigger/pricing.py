"""What a run's model calls cost: each priced by the declared source step that served it.

A call is matched by its provider and model against the steps of its
profile's source in the person declaration: the source itself, then its
fallbacks in order. A subscription step costs nothing per call; a priced step
costs its fresh input, its cache reads and its output at their own rates.

A run's cost is the sum over its calls, and unknown once one call's is: the
row's cost stays NULL, an honest unknown where a guess would not be, and the
call is counted with why it has no cost. `undeclared`: no step covers its
provider and model. `no_usage`: a metered call that reported no usage.
`no_calls`: a run that called the model, and none of its calls came through
the hook.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from .ledger import CallTrace, Usage
from .metrics import Metrics
from .persons import DEFAULT_PROFILE, ModelSource, PersonsFile, SourceStep

logger = logging.getLogger(__name__)

# Which step of its profile's source served a call: none, for a provider and model it does not name.
Step = Literal["primary", "fallback", "undeclared"]
# Why a call or a run has no cost.
Unpriced = Literal["undeclared", "no_usage", "no_calls"]


@dataclass(frozen=True)
class Served:
    """The source a call's profile runs on, and the step of it that served the call."""

    # Empty for a profile that runs on no declared source.
    source: str
    step: Step
    # The declared step that matched; None when none did.
    declared: SourceStep | None


class Pricing:
    """The declared sources, and the counters of what was priced and what could not be."""

    def __init__(self, persons: PersonsFile, metrics: Metrics) -> None:
        self._persons = persons
        self._metrics = metrics

    def served(self, profile: str, provider: str | None, model: str | None) -> Served:
        """Which step of the profile's source this provider and model are."""
        source = self._source_of(profile)
        if source is None:
            return Served("", "undeclared", None)
        if (source.provider, source.model) == (provider, model):
            return Served(source.name, "primary", source)
        for step in source.fallback:
            if (step.provider, step.model) == (provider, model):
                return Served(source.name, "fallback", step)
        return Served(source.name, "undeclared", None)

    def price(self, profile: str, usage: Usage) -> Usage:
        """The usage with each call's cost and their sum; the run's cost when every call has one."""
        calls = tuple(
            replace(call, cost=_cost(self.served(profile, call.model_source, call.model), call))
            for call in usage.calls
        )
        known = [call.cost for call in calls if call.cost is not None]
        total = sum(known, Decimal(0)) if calls and len(known) == len(calls) else None
        return replace(usage, calls=calls, cost=total)

    def count(self, use_case: str, profile: str, usage: Usage, *, answered: bool) -> None:
        """Count what a run's calls cost, what has no cost, and which step answered.

        `usage` is what :meth:`price` returned. An answer is counted under the
        step that served it: the provider and model the run reports, which are
        its last call's.
        """
        for call in usage.calls:
            served = self.served(profile, call.model_source, call.model)
            if call.cost is None:
                reason: Unpriced = "undeclared" if served.declared is None else "no_usage"
                self._unpriced(use_case, call.model_source, call.model, reason)
            else:
                self._metrics.cost.labels(
                    use_case=use_case, source=served.source, step=served.step
                ).inc(float(call.cost))
        # A run that answered or read tokens called the model, whether the hook said so or not.
        if not usage.calls and (answered or usage.tokens_in):
            self._unpriced(use_case, usage.model_source, usage.model, "no_calls")
        if answered and usage.model_source is not None:
            served = self.served(profile, usage.model_source, usage.model)
            self._metrics.answers.labels(
                use_case=use_case, source=served.source, step=served.step
            ).inc()

    def _unpriced(
        self, use_case: str, provider: str | None, model: str | None, reason: Unpriced
    ) -> None:
        logger.warning(
            "%s: a call on %s/%s has no cost (%s); its row's cost stays NULL",
            use_case,
            provider,
            model,
            reason,
        )
        self._metrics.unpriced_calls.labels(
            use_case=use_case, provider=provider or "", model=model or "", reason=reason
        ).inc()

    def _source_of(self, profile: str) -> ModelSource | None:
        # Only the default profile runs on a declared source.
        return self._persons.default_source if profile == DEFAULT_PROFILE else None


def _cost(served: Served, call: CallTrace) -> Decimal | None:
    step = served.declared
    if step is None:
        return None
    if step.bound == "subscription":
        return Decimal(0)
    # The loader refuses a step a profile runs on that is neither priced nor a subscription.
    assert step.price is not None
    tokens_in, cached, tokens_out = call.tokens_in, call.tokens_cached, call.tokens_out
    if tokens_in is None or cached is None or tokens_out is None:
        return None
    return step.price.cost(tokens_in, cached, tokens_out)
