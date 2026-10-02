"""Prometheus metrics: what the consumer and the hook receiver saw, and what became of it."""

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
        self.deliveries = Counter(
            "agent_trigger_deliveries_total",
            "Outputs carried to a declared target, by use case, target and whether it took them.",
            ["use_case", "target", "outcome"],  # sent | failed
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
        # The hook path: what became of each delivery, and the rows it wrote.
        self.hook_events = Counter(
            "agent_trigger_hook_events_total",
            "Deliveries of the harness's hook, by what the receiver made of them.",
            # counted | recorded | traced | duplicate | ignored | refused | invalid | error
            ["outcome"],
            registry=self.registry,
        )
        # Calls of turns that never sent their end, e.g. the harness's own
        # background work after an answer: spent, and in no row.
        self.orphaned_calls = Counter(
            "agent_trigger_orphaned_calls_total",
            "Model calls whose turn never reported its end within an hour.",
            registry=self.registry,
        )
        self.recorded_runs = Counter(
            "agent_trigger_recorded_runs_total",
            "Chat and cron runs the harness ran on its own, by use case and row status.",
            ["use_case", "status"],  # completed | failed
            registry=self.registry,
        )
        self.api_requests = Counter(
            "agent_trigger_api_requests_total",
            "Requests to the trigger's own API, by route and the status code they were answered.",
            ["route", "code"],  # runs | memory
            registry=self.registry,
        )
        # The cron job set brought into the harness at startup.
        self.reconciles = Counter(
            "agent_trigger_reconciles_total",
            "Reconciles of the managed cron jobs, by whether the gateway let them through.",
            ["outcome"],  # done | failed | refused
            registry=self.registry,
        )
        self.cron_jobs = Counter(
            "agent_trigger_cron_jobs_total",
            "Managed cron jobs the reconcile changed in the harness, by what it did.",
            ["action"],  # created | updated | deleted
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
