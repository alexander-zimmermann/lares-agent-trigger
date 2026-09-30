"""Prometheus metrics: what the consumer saw, and what became of it."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram


class Metrics:
    """One registry per process; the shared metrics server exposes it."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()

        self.events = Counter(
            "agent_trigger_events_total",
            "Episode events read off the stream, by event kind and what the filter made of them.",
            ["kind", "outcome"],  # matched | unmatched | invalid
            registry=self.registry,
        )
        self.runs = Counter(
            "agent_trigger_runs_total",
            "Runs the trigger started, by use case and the status their row closed with.",
            ["use_case", "status"],  # completed | failed
            registry=self.registry,
        )
        # Every failed attempt, so a retry that went through still shows here.
        self.failures = Counter(
            "agent_trigger_failures_total",
            "Failed run attempts, by use case and failure class.",
            ["use_case", "class"],
            registry=self.registry,
        )
        self.alerts = Counter(
            "agent_trigger_alerts_total",
            "AgentRunFailed posts to Alertmanager, by whether it took them.",
            ["outcome"],  # sent | failed
            registry=self.registry,
        )
        # The two ways a matched event does not become a run get their own
        # counters rather than a `status` of runs_total, which would count runs
        # that never started. `agent_trigger_capped_total` is the name the
        # spec's alert set uses (`AgentTriggerCapped` routes to mail).
        self.capped = Counter(
            "agent_trigger_capped_total",
            "Events refused because the use case had spent its runs for the day.",
            ["use_case"],
            registry=self.registry,
        )
        self.duplicates = Counter(
            "agent_trigger_duplicate_events_total",
            "Events whose subject the ledger already held, by use case.",
            ["use_case"],
            registry=self.registry,
        )
        self.run_duration = Histogram(
            "agent_trigger_run_duration_seconds",
            "Wall-clock time from starting a harness run to its terminal state.",
            ["use_case"],
            # A run's budget is minutes, not seconds; the default buckets stop at 10 s.
            buckets=(5, 15, 30, 60, 120, 300, 600, 1200),
            registry=self.registry,
        )
