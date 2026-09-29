# lares-agent-trigger

The service that decides when the house explains itself.

It reads the episode events [lares-diagnostics-engine](https://github.com/alexander-zimmermann/lares-diagnostics-engine) publishes on NATS, decides against a declared use-case file whether an event deserves an answer, starts a run on the [Hermes](https://github.com/NousResearch/hermes-agent) harness, waits for it, and writes the result into one row of the agent ledger.

Besides the chat and the harness's own cron, this is the only thing that starts an agent run — and the only writer of `agent_runs`.

## Why a service and not a job

An LLM run is expensive, slow and easy to start twice. Three decisions have to be made before a model is ever asked, and none of them belongs in a prompt:

- **Is this worth explaining?** The declared filter answers it: an episode that opens at severity 2 or rises is explained, one that ended is not.
- **Has it been explained already?** The ledger's unique key on `(use_case, subject_kind, subject_key)` answers it, with the event kind inside the subject key after the colon (`15510:escalated`). The row is written *before* the run is started, so a redelivered message finds the key taken and stops there. One event can never become two runs.
- **Has the day's budget been spent?** Ten runs a day for `explain-episode`; the eleventh becomes a row with status `capped` and a counter, never a silent drop.

Everything the model may see is decided outside it too. The Runs API takes no toolset list, so an API run sees exactly what `platform_toolsets.api_server` allows — the read-only MCP bridge, never the write path.

## The declared use case

One entry of `use-cases.yaml`, mounted from [lares](https://github.com/alexander-zimmermann/lares):

```yaml
use_cases:
  - name: explain-episode
    sentence: Erklärt eine neu aufgetretene oder eskalierte Episode von selbst.
    trigger:
      kind: event
      source: episode
      # event kind → the lowest severity worth a run; a kind left out never runs
      filter:
        appeared: 2
        escalated: 2
    skill: lares-explain
    tools: [lares]
    output: [stored, discord, mail]
    budget:
      tool_calls: 40
      minutes: 10
      runs_per_day: 10
    language: de
    memory: false
    enabled: true
```

A use case either runs (`enabled: true`) or says why it does not (`dormant: <reason>`). There is no `enabled: false` — switching one off without a reason turns the catalogue into a guessing game. A file that does not validate stops the process at startup rather than running a half-read catalogue.

Schema and loader live in this package because the generator renders the harness configuration from the same models: one schema for rendering and for runtime.

## The event path

```
episode.appeared ─┐
episode.escalated ┼─► durable pull consumer `agent-trigger` on stream EPISODE
episode.ended ────┘             │
                                ▼
                          filter (per use case)
                                │ matched
                                ▼
                     INSERT agent_runs … ON CONFLICT DO NOTHING   ← the dedupe
                                │ got the row
                                ▼
                          runs today < budget?  ──no──►  status = capped
                                │ yes
                                ▼
                   POST /v1/runs   (Idempotency-Key = the ledger key)
                                │
                                ▼
                   GET  /v1/runs/{id} until terminal, or the budget expires
                                │
                                ▼
       UPDATE agent_runs: status, tldr (first line), text, model, tokens,
                          cost, duration, tool_trace = {"tool_count": n}
```

The message is acknowledged once the row is closed, which is why the consumer's `ackWait` has to outlast the longest declared budget.

## Configuration

Environment variables; every secret can arrive as a mounted file instead of a literal.

| Variable                                    | Default                                            | What it is                                        |
| ------------------------------------------- | -------------------------------------------------- | -------------------------------------------------- |
| `USE_CASES_FILE`                            | `/etc/lares-agent-trigger/use-cases.yaml`          | The declared use cases.                            |
| `NATS_SERVERS`                              | `nats://localhost:4222`                            | Comma-separated server list.                       |
| `NATS_NKEY_SEED_FILE`                       | —                                                  | The nkey seed; needs `$JS.ACK.>` to acknowledge.   |
| `NATS_STREAM_NAME` / `CONSUMER_NAME`        | `EPISODE` / `agent-trigger`                        | The stream and the durable consumer bound to.      |
| `DB_HOST` / `DB_PORT` / `DB_NAME`           | — / `5432` / `homelab`                             | Where the ledger lives.                            |
| `DB_USERNAME_FILE` / `DB_PASSWORD_FILE`     | —                                                  | The ledger-writer role.                            |
| `TIMEZONE`                                  | `Europe/Berlin`                                    | Which midnight the daily cap counts from.          |
| `HERMES_URL`                                | `http://hermes.agents.svc.cluster.local:8642`      | The harness's API server.                          |
| `HERMES_API_KEY_FILE`                       | —                                                  | Its API key.                                       |
| `HERMES_POLL_SECONDS`                       | `5.0`                                              | How often a running run is asked about.            |
| `HERMES_REQUEST_TIMEOUT_SECONDS`            | `30.0`                                             | Per-request timeout against the API server.        |
| `METRICS_PORT`                              | `9090`                                             | `/metrics` and `/healthz`.                         |
| `LOG_LEVEL` / `LOG_FORMAT`                  | `INFO` / `json`                                    | Logging.                                           |
| `TRACING_ENDPOINT`                          | —                                                  | OTLP/HTTP collector base URL; unset keeps it off.  |

## Metrics

| Metric                                  | Labels             | What it counts                                              |
| --------------------------------------- | ------------------ | ------------------------------------------------------------ |
| `agent_trigger_events_total`            | `kind`, `outcome`  | Events read, and whether the filter wanted them.             |
| `agent_trigger_runs_total`              | `use_case`, `status` | Runs started, by terminal status.                          |
| `agent_trigger_capped_total`            | `use_case`         | Events refused because the day's budget was spent.           |
| `agent_trigger_duplicate_events_total`  | `use_case`         | Events whose subject the ledger already held.                |
| `agent_trigger_run_duration_seconds`    | `use_case`         | Wall-clock time from start to terminal state.                |

`/healthz` is NATS- and ledger-gated. A harness outage is deliberately not part of it: that is a failed run with its own alert, never a restart loop.

## Tests

One seam, at the service's edges. An episode event goes in on a real NATS container with a real durable consumer; the ledger rows land in a real TimescaleDB container; only the harness is a fake, over `respx`, because a run is a model call.

```bash
uv sync --extra dev
uv run pytest
```

Docker is required for the container fixtures.

## Licence

GPL-2.0-or-later. See [LICENSE](LICENSE).
