# lares-agent-trigger

The service that decides when the house explains itself.

It reads the episode events [lares-diagnostics-engine](https://github.com/alexander-zimmermann/lares-diagnostics-engine) publishes on NATS, decides against a declared use-case file whether an event deserves an answer, starts a run on the [Hermes](https://github.com/NousResearch/hermes-agent) harness, waits for it, writes the result into one row of the agent ledger, and delivers it where the use case says: a Discord message, a mail.

Besides the chat and the harness's own cron, this is the only thing that starts an agent run — and the only writer of `agent_runs`. The runs it does not start still get their row: the harness reports every finished chat turn and cron execution to the trigger's hook receiver.

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
       UPDATE agent_runs: tldr (first line), text, model, tokens,
                          cost, duration, tool_trace = {"tool_count": n}
                                │
                                ▼
                   deliver to each declared output (Discord, mail)
                                │
                                ▼
       UPDATE agent_runs: status, output_ref = one entry per delivery
```

The message is acknowledged once the row is closed, which is why the consumer's `ackWait` has to outlast two of the longest declared budgets plus the retry delay.

## Delivery

The model never delivers. On an API run the harness posts nothing itself; once a run has completed, the trigger carries its text to every target the use case declares under `output`, in that order:

| Target    | What it does                                                                                                                         | `output_ref`                    |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------- |
| `stored`  | The row itself: the text is written before any other target sees it.                                                                 | —                               |
| `discord` | Posts to the home channel through the bot's REST API, with the token the harness chats with. No mention in the text can ping anyone. | `discord:<channel>/<message>`, one per message |
| `mail`    | Sends a plaintext mail through the cluster's relay, from its one accepted sender to the owner.                                        | `mail:<Message-ID>`             |

Discord takes 2000 characters a message. A text that fits goes as it is; a longer one goes as the cause and its proof lines (the `-# ` lines the skill writes), then the rest in a second message. A part still too long is cut on a line and ends in `… (run <id>)`: the row holds the whole text.

The mail's subject names what was measured and where — `[Explain] <fault sentence up to its dash> · <channel>` — and its body is the explanation with its proof lines as a plain list, a footer with model, tokens, cost (left out when the run cost nothing, as on a subscription) and duration, and the link to the episode on the dashboard:

```
Subject: [Explain] Ein Gerät zieht ununterbrochen länger Strom, als seine je Gerät erlaubte Laufzeit zulässt · 2/1/197

Die Waschmaschine hängt seit 14:20 im Spülgang.

• Subject: appliance_runtime auf 2/1/197, seit 25.09. 14:20, Stufe 2
…

--
gpt-5.5 (openai-codex) · 4200 + 310 Tokens · 0.0210 USD · 11 s
https://grafana.zimmermann.sh/d/knx-episodes?var-fault=appliance_runtime
```

The fault sentence comes from the engine's own `faults.yaml`, mounted unchanged; an event whose fault the list no longer holds is named by the fault's name.

A target that refuses does not stop the next one from trying. The run then closes `failed` with each refusal's raw text as the error, keeps its text and the refs of everything that was created — the first message of a split post included — and raises `AgentRunFailed` like any failed run. The model is not asked again: running it twice would not change what Discord or the relay make of the answer. A run the harness reports completed but without any output fails before delivery, as `unknown`.

Only targets an enabled event use case declares are built, and a declared target without its settings, or one this trigger does not deliver (`alert`, the GitHub targets, `wiki_page`), stops the pod at startup.

## When a run fails

Nothing fails silently. A failed run is retried once when the failure fixes itself, and reported when it does not:

| Class                | Retry | What it is                                                              |
| -------------------- | ----- | ------------------------------------------------------------------------ |
| `rate_limited`       | yes   | 429, a quota throttle.                                                   |
| `timeout`            | yes   | The model source or the harness took too long to answer one call.        |
| `network_error`      | yes   | The harness could not reach the model source.                            |
| `hermes_unreachable` | yes   | The harness itself did not answer, or answered 5xx.                      |
| `interrupted`        | yes   | The gateway restarted before the run settled.                            |
| `credits_exhausted`  | no    | The account is empty; xAI sends it as a 403, so it outranks auth.        |
| `auth_failed`        | no    | A key or token the source refused.                                       |
| `budget_exhausted`   | no    | The run spent its tool calls or its minutes.                             |
| `invalid_config`     | no    | A model, provider or request the gateway does not accept.                |
| `unknown`            | no    | Nothing above matched; the raw error in the alert says what it was.      |
| `trigger_restarted`  | no    | A redelivered event found its row still open: this pod died mid-run.     |
| `delivery_failed`    | no    | The run completed, but a declared target refused its output.             |

The harness reports a failed run as free text, so the class is read off the text by rules that follow the gateway's own cron classifier. The retry starts after `RETRY_DELAY_SECONDS` on the same row, with `attempt = 2` and its own idempotency key — the ledger key with `/2` appended, because the gateway replays the run it holds for a key it has seen, failed or not.

A run still failed after that closes its row with `status = failed`, `attempt` and the raw `error`, and the trigger posts `AgentRunFailed` straight to Alertmanager's `/api/v2/alerts`:

```json
[{
  "labels": {"alertname": "AgentRunFailed", "use_case": "explain-episode", "severity": "warning"},
  "annotations": {
    "summary": "explain-episode failed on episode 15510:appeared after 2 attempts (rate_limited)",
    "description": "<the raw error, cut at 1024 characters>"
  }
}]
```

There is no `endsAt`, so Alertmanager resolves it after its resolve timeout. An Alertmanager that does not take the post is logged and counted, never retried into a redelivery loop.

A row that a dead pod left `queued` or `running` is not started again when its event is redelivered — the harness may still be working on it — but closed as `trigger_restarted` and reported the same way.

## Chat and cron runs

The harness starts two kinds of run on its own: a turn in Discord, and a cron job coming due. The trigger hears of them as they happen, through the harness's outbound hook (`hooks.outbound` in its configuration), which posts two of its lifecycle hooks to `POST /hooks/hermes`, signed with HMAC-SHA256 over the raw body (`X-Hermes-Signature-256: sha256=<hex>`) under a secret both pods read:

- `post_api_request`, once per call to the model: its tokens, the model and its source, when it started and ended, how many tools it asked for, and its reply. Each call is added to its turn's tally in memory.
- `on_session_end`, which despite its name fires once per turn, after that turn's calls: whether it completed, and why it stopped. It writes the turn's row from the tally.

| Platform      | Use case                                                        | Row                                                                    |
| ------------- | --------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `discord`     | the one enabled `message` use case                              | trigger `message`, subject kind `chat`, key `<session id>:<turn>`       |
| `cron`        | the enabled `schedule` use case its job is named after          | trigger `schedule`, subject kind `none`, key `<job id>:<execution id>`  |
| `api_server`  | —                                                               | none: the runs this service started hold their row already              |
| anything else | —                                                               | none                                                                    |

The gateway mints cron job ids itself, so a managed job carries its use case in its name: `lares:propose-faults`. A job without that prefix, or one naming a use case the file does not enable as a schedule, is left alone.

The row holds what the turn's calls added up to: the tokens the model read (cache included) and wrote, the tools it asked for, the time from the first call's start to the last call's end, the model and model source of the last call — a fallback shows up there — and the last call's reply as the answer. Nothing is read off the session record, whose counters run over a whole conversation. A flat subscription bills nothing per call, so `cost` stays empty rather than guessed. A turn whose calls came in before a restart and whose end came after still gets its row, without the tally.

What the receiver answers is what the gateway acts on — it sends a delivery at most twice, the second time only after a connection error or a 5xx:

| Answer | When                                                                                 |
| ------ | ------------------------------------------------------------------------------------ |
| `200`  | The call is counted or the row written, either was already (a replay), or the turn is not ours. |
| `401`  | The signature is missing or wrong. Counted, never parsed.                            |
| `400`  | The body is neither hook.                                                            |
| `503`  | The ledger or the Jobs API did not answer; the second try may succeed.              |

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
| `RETRY_DELAY_SECONDS`                       | `300.0`                                            | How long a transient failure waits for its retry.  |
| `ALERTMANAGER_URL`                          | `http://prometheus-alertmanager.prometheus.svc.cluster.local:9093` | Where `AgentRunFailed` is posted. |
| `ALERTMANAGER_REQUEST_TIMEOUT_SECONDS`      | `10.0`                                             | Per-request timeout against Alertmanager.          |
| `DISCORD_BOT_TOKEN_FILE`                    | —                                                  | The bot token; needed by the `discord` output.     |
| `DISCORD_HOME_CHANNEL`                      | —                                                  | The channel id the `discord` output posts into.    |
| `DISCORD_REQUEST_TIMEOUT_SECONDS`           | `10.0`                                             | Per-request timeout against Discord.               |
| `SMTP_HOST` / `SMTP_PORT`                   | — / `25`                                           | The relay the `mail` output sends through.         |
| `SMTP_TIMEOUT_SECONDS`                      | `30.0`                                             | Socket timeout against the relay.                  |
| `MAIL_FROM` / `MAIL_TO`                     | —                                                  | The relay's accepted sender, and the owner.        |
| `FAULTS_FILE`                               | `/etc/lares-agent-trigger/faults.yaml`             | The engine's fault list, for the mail subject.     |
| `DASHBOARD_EPISODE_URL`                     | —                                                  | The episode on the dashboard; `{episode_id}` and `{fault}` are filled in. |
| `HTTP_PORT`                                 | `8080`                                             | The hook receiver, `POST /hooks/hermes`.           |
| `HOOK_SECRET_FILE`                          | —                                                  | The HMAC secret the harness signs deliveries with. |
| `METRICS_PORT`                              | `9090`                                             | `/metrics` and `/healthz`.                         |
| `LOG_LEVEL` / `LOG_FORMAT`                  | `INFO` / `json`                                    | Logging.                                           |
| `TRACING_ENDPOINT`                          | —                                                  | OTLP/HTTP collector base URL; unset keeps it off.  |

## Metrics

| Metric                                  | Labels             | What it counts                                              |
| --------------------------------------- | ------------------ | ------------------------------------------------------------ |
| `agent_trigger_events_total`            | `kind`, `outcome`  | Events read, and whether the filter wanted them.             |
| `agent_trigger_runs_total`              | `use_case`, `status` | Runs started, by the status their row closed with.         |
| `agent_trigger_failures_total`          | `use_case`, `class`  | Failed attempts, by failure class — a retry that went through still shows here. |
| `agent_trigger_deliveries_total`        | `use_case`, `target`, `outcome` | Outputs carried to a target, `sent` or `failed`. |
| `agent_trigger_alerts_total`            | `outcome`          | `AgentRunFailed` posts, `sent` or `failed`.                  |
| `agent_trigger_capped_total`            | `use_case`         | Events refused because the day's budget was spent.           |
| `agent_trigger_duplicate_events_total`  | `use_case`         | Events whose subject the ledger already held.                |
| `agent_trigger_run_duration_seconds`    | `use_case`         | Wall-clock time from start to terminal state.                |
| `agent_trigger_hook_events_total`       | `outcome`          | Hook deliveries: `counted`, `recorded`, `duplicate`, `ignored`, `refused`, `invalid`, `error`. |
| `agent_trigger_recorded_runs_total`     | `use_case`, `status` | Chat and cron runs written from the hook, by the status of their row. |

`/healthz` is NATS- and ledger-gated. A harness outage is deliberately not part of it: that is a failed run with its own alert, never a restart loop.

## Tests

One seam, at the service's edges. An episode event goes in on a real NATS container with a real durable consumer, and a hook delivery goes in over HTTP to the receiver's ASGI app in process, signed as the gateway signs it; the ledger rows land in a real TimescaleDB container; the harness, Alertmanager and Discord are fakes over `respx` — the harness because a run is a model call, the other two so the alert and the message are asserted as they would arrive — and the mail lands at an SMTP server in the test process that keeps the relay's one rule, its accepted sender. The failure classes have a table test of their own against the gateway's error texts.

```bash
uv sync --extra dev
uv run pytest
```

Docker is required for the container fixtures.

## Licence

GPL-2.0-or-later. See [LICENSE](LICENSE).
