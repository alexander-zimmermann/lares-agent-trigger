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
            # counted | recorded | handed_over | duplicate | ignored
            # | refused | invalid | error
            ["outcome"],
            registry=self.registry,
        )
        # Calls spent and in no row: of a turn whose end never came.
        # `enduser_id` is the profile's person; empty for the machines.
        self.orphaned_calls = Counter(
            "agent_trigger_orphaned_calls_total",
            "Model calls in no row: of a turn that never reported its end within an hour, "
            "by the profile's person.",
            ["enduser_id"],
            registry=self.registry,
        )
        # What the model calls of every run cost, by the declared source step
        # that served them and the person the run answered (empty for the
        # machines), and the calls without a cost.
        self.cost = Counter(
            "agent_trigger_cost_usd_total",
            "Model spend in USD from the declared prices, by use case, model source, step "
            "and person.",
            # step: primary | fallback
            ["use_case", "source", "step", "enduser_id"],
            registry=self.registry,
        )
        self.unpriced_calls = Counter(
            "agent_trigger_unpriced_calls_total",
            "Model calls without a cost, by use case and who served them: no declared step, no "
            "usage, or calls that never came through the hook; their row's cost stays NULL.",
            ["use_case", "provider", "model"],
            registry=self.registry,
        )
        self.answers = Counter(
            "agent_trigger_answers_total",
            "Answered runs, by use case, model source and the step of it that gave the answer.",
            ["use_case", "source", "step"],  # primary | fallback | undeclared
            registry=self.registry,
        )
        # `enduser_id` is the person a chat turn answered; empty for the machines.
        self.recorded_runs = Counter(
            "agent_trigger_recorded_runs_total",
            "Chat and cron runs the harness ran on its own, by use case, row status and person.",
            # status: completed | failed
            ["use_case", "status", "enduser_id"],
            registry=self.registry,
        )
        # The person got an error instead of an answer; `error_type` as on the failed call's span.
        self.failed_chat_turns = Counter(
            "agent_trigger_failed_chat_turns_total",
            "Chat turns whose last model call failed for good, by person and the harness's class "
            "of the failure.",
            ["enduser_id", "error_type"],
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
            "Reconciles of the managed cron jobs, by whether the harness let them through.",
            ["outcome"],  # done | failed | refused
            registry=self.registry,
        )
        self.cron_jobs = Counter(
            "agent_trigger_cron_jobs_total",
            "Managed cron jobs the reconcile changed in the harness, by what it did.",
            ["action"],  # created | updated | deleted
            registry=self.registry,
        )
        # The daily read-back of the pull requests the ledger holds as open.
        self.read_backs = Counter(
            "agent_trigger_read_backs_total",
            "Open pull requests looked up on GitHub, by use case and the state they were found in.",
            ["use_case", "state"],  # open | merged | closed | failed
            registry=self.registry,
        )
        # The owner's corrections of a repo skill, as pull requests.
        self.skill_pull_requests = Counter(
            "agent_trigger_skill_pull_requests_total",
            "The owner's corrections of a repo skill on GitHub, by what became of them.",
            ["outcome"],  # opened | committed | failed
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
