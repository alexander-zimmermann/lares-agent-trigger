# lares-agent-trigger

The service that decides when the house explains itself.

It reads the episode events [lares-diagnostics-engine](https://github.com/alexander-zimmermann/lares-diagnostics-engine) publishes on NATS, decides against a declared use-case file whether an event deserves an answer, starts a run on the [Hermes](https://github.com/NousResearch/hermes-agent) harness, waits for it, writes the result into one row of the agent ledger, and delivers it where the use case says: a Discord message, a mail, a wiki page, a pull request, an issue or a comment on GitHub.

Besides the chat and the harness's own cron, this is the only thing that starts an agent run — and the only writer of `agent_runs` and `agent_memory`. The runs it does not start still get their row: the harness reports every finished chat turn and cron execution to the trigger's hook receiver. The cron jobs themselves it keeps in line with the declaration, and a person in chat can have it start a use case now.

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

`tools` names tool servers, which the same file declares beside the use cases, together with where the harness reports every finished turn, the share of traces every service of the platform keeps, the gateway every tool server sits behind, and the connectors a person signs in to on it:

```yaml
ledger_hook:
  trigger_url: http://lares-agent-trigger.agents.svc.cluster.local:8080
  secret_env: LARES_AGENT_TRIGGER_HOOK_SECRET

tracing:
  sampling_ratio: 1.0     # one number for the trigger, the bridge, the engine and the gateway

gateway:
  url: http://agentgateway.agents.svc.cluster.local:8080                  # where the harness reaches it
  otlp_endpoint: http://alloy-alloy-receiver.alloy.svc.cluster.local:4318  # where it sends a span of every call

tool_servers:
  - name: lares
    kind: http                  # the one kind: an MCP endpoint behind its route
    url: http://lares-mcp-bridge.lares-mcp-bridge.svc.cluster.local:8080/mcp  # which the gateway calls
    credential:                 # what the gateway attaches on every call
      secret: agentgateway-keys # a Secret beside the gateway
      key: lares-mcp-bridge
      header: Authorization     # a bearer token; any other header takes the value as it is
    key_env: LARES_GATEWAY_KEY  # where the harness holds its key for the route
    timeout_seconds: 60
    access: read
    tools: [list_*, get_*, query_*, correlate_events, search_wiki]
```

Every tool server is an HTTP endpoint behind a route of its own on one gateway, [agentgateway](https://agentgateway.dev): the bridge under each of its servers, and any other MCP server such as the official [GitHub MCP server](https://github.com/github/github-mcp-server). The harness calls the route with a key valid there only; the gateway serves the server's `tools` and nothing else, attaches the credential the server expects, and names the caller to a server in the cluster. The harness holds no server's credential. `tools` is the route's tool list and the harness's include list on top. `access` is where it may be granted, and the loader refuses an enabled use case that names a server it may not hold:

| `access`  | Granted to                                                                         |
| --------- | ---------------------------------------------------------------------------------- |
| `read`    | any use case                                                                       |
| `write`   | schedule use cases only, and none while the Jobs API gives a cron job no tool list of its own (see [The cron jobs](#the-cron-jobs)) |
| `request` | the chat only: what a person asks for in conversation — a write request approved elsewhere, a run started now |
| `memory`  | use cases with `memory: true` only: their own memory, read and appended through the bridge. A surface the harness shares — all event runs, all cron jobs — carries it only when every enabled use case on it keeps a memory |

A tool sits on one server only: the loader refuses a declaration where an entry of one server matches an entry of another on the same `url`, so `get_*` on the read server and `get_write_request` on the gate's request server cannot stand side by side — the read server then lists its `get_` tools by name. GitHub's `get_*` is GitHub's own. No two servers share a `key_env`.

A connector is a person's own MCP client, such as the claude.ai connector, signed in through Authentik on a public path of the gateway:

```yaml
connectors:
  - name: claude-ai
    path: /mcp                                               # where the client connects
    issuer: ${AUTHENTIK_URL}/application/o/claude-ai/ # the application's issuer; the gateway expands the variable
    client_id: claude-ai                                     # its pre-registered client, the tokens' audience
    url: http://lares-mcp-bridge.lares-mcp-bridge.svc.cluster.local:8080/mcp
    credential: {secret: agentgateway-keys, key: lares-mcp-bridge, header: Authorization}
    tools: ["*"]
```

A person holds it by name (`connectors:` in [the persons](#the-persons)). A connector's name and path are a route's own: the loader refuses one named like a tool server or another connector, or on a path another route takes (`/mcp/<server>`).

A dormant use case may name tool servers and event sources (`alert`, `pull_request`, `ets_export`, `new_device`) that are not built yet; enabling it is refused until they are.

Schema and loader live in this package because the generator renders the harness configuration from the same models: one schema for rendering and for runtime.

## The model sources

`persons.yaml` sits beside the use-case file in lares. It declares the model sources a profile may run on, names the default profile's, which every event and cron run uses, and declares the persons (next section):

```yaml
default_profile:
  model: owner-subscription

model_sources:
  - name: owner-subscription
    provider: openai-codex
    model: gpt-6-sol
    account: shared-with-default   # the default profile's login; per-person is a person's alone
    bound: subscription            # a flat plan: nothing per call
    fallback:                      # tried in order when the source fails
      - provider: openai-api
        model: gpt-6-luna
        bound: capped              # metered against a hard limit
        price: {input: 0.10, cached_input: 0.01, output: 0.50}   # USD per million tokens
```

The generator renders the default profile's `model` and `fallback_providers` from its source. The loader refuses the file at pre-commit and at startup when:

- the default profile names no declared source, or its source has no `bound`;
- a `capped` step names no `price`, or a `subscription` step names one;
- a fallback of a source a profile runs on is neither priced nor a subscription;
- a fallback runs on `openai-codex`: it would draw on the Codex login it stands in for;
- a step of a source a profile runs on names a provider whose key variable is not known: `openai-api` reads `OPENAI_API_KEY`, and `openai-codex` is a login on the data volume that reads none;
- two sources share a name.

A source nobody runs on may leave its bound and its fallbacks' prices open, and name any provider; it renders nothing.

Every model call is priced at the row's write by the step of its profile's source that served it, matched on the call's provider and model. A subscription step costs 0. A priced step costs fresh input, cache reads and output at their own rates; a cache write counts as fresh input. The row's `cost` is the sum over its calls, on every path: chat, cron and event; an event attempt that is retried keeps no row of its own, and what it spent counts in the metrics only. A row's `cost` stays NULL, and `agent_trigger_unpriced_calls_total` counts it, when a call matches no declared step (`undeclared`), a metered call reported no usage (`no_usage`), or a run answered or read tokens and none of its calls came through the hook (`no_calls`). The session record's cost estimate is never read.

## The persons

A person is a household member known in Git by a handle, never by a legal name, an e-mail or a Discord id: Git holds their rights, the sign-in provider their identity. Each enabled person gets a Hermes profile of their own.

```yaml
persons:
  - name: kim                      # the handle: the profile's and the sign-in's, and whom the gateway names
    discord: admin                 # true: the bot routes their messages to their profile; admin: every slash command too
    tools: [lares, lares-runs, github]   # a subset of the chat use case's tools
    connectors: [claude-ai]        # the connectors they sign in to; default none
    model: owner-subscription      # a declared model source
    keep_answers: true             # whether the person's rows keep the answers' text; default false
    enabled: true                  # or `dormant: <reason>`
```

The loader refuses the file at pre-commit and at startup when:

- two persons share a handle, a handle is no Hermes profile name of lower-case letters, digits and `-` up to 32 characters (it names the profile's volumes too), it is one Hermes keeps (`hermes`, `test`, `tmp`, `root`, `sudo`), or it is `default`, which belongs to the machines;
- an enabled person names a connector the use-case file does not declare;
- an enabled person names a tool the chat use case does not hold, or no chat use case is enabled;
- an enabled person's source is not declared, has no `bound`, or names `anthropic` in any step: Hermes keeps that provider's login state in the default profile's credentials;
- more than one enabled person runs on a `shared-with-default` source;
- enabled persons are on Discord and none is `discord: admin`;
- a person sets both `enabled` and `dormant`, `enabled: false`, or a `dormant` without a reason.

A dormant person renders nothing, so it may name tools and sources still to come. At runtime a turn of their profile is one of a profile nobody runs on (see [Chat and cron runs](#chat-and-cron-runs)).

## The generator

`lares-agent-trigger generate` renders the use-case file and the person declaration into what the harness, the gateway and the platform's services read. lares's pre-commit hook runs it at the trigger's deployed tag (`task agents:create-harness-config`) and writes the files in place; a commit whose files it changed stops once, to be added.

```bash
lares-agent-trigger generate \
  --use-cases use-cases.yaml --persons persons.yaml --settings settings.yaml \
  --hermes-config config.yaml --cron-jobs cron-jobs.yaml \
  --tracing tracing.env --env-template env-template.yaml \
  --routes routes.yaml --routes-values routes.values.yaml --profiles profiles
```

| Output             | What it holds                                                                                                     |
| ------------------ | ----------------------------------------------------------------------------------------------------------------- |
| `config.yaml`      | the default profile's: the hand-written `settings.yaml` copied whole, then `model`, `fallback_providers` and `terminal` from its source and directory, then `platform_toolsets`, `hooks.outbound` and `mcp_servers`; with persons on Discord, their routes and rights inside the settings' `gateway` and `discord` blocks |
| `profiles/<handle>/config.yaml` | each enabled person's: `settings.yaml` copied whole, then `model`, `fallback_providers` and `terminal` from their source and profile directory, then their tool servers on `discord` and `api_server`, Hermes's own `skills` toolset first on `api_server` (Discord's composite holds it already), the same hook, and their `mcp_servers` |
| `profiles/kustomization.yaml` | the ConfigMap `hermes-profiles`, one key per person's config, named by the handle |
| `profiles/profiles.values.yaml` | an additional values file of Hermes's chart: per person a read-only subPath mount of their config, their `.env`, the persons' `SOUL.md`, `AGENTS.md` and `.no-bundled-skills` into `/opt/data/profiles/<handle>/`, and a container `agent-log-<handle>` that streams the profile's `logs/agent.log` for Loki, beside the default profile's `agent-log`; its image is the name `agent-log`, which lares's kustomization pins to the image every log container runs |
| `cron-jobs.yaml`   | one job per enabled schedule use case, named `lares:<use case>`, with its schedule, skill, prompt and delivery     |
| `tracing.env`      | `TRACING_SAMPLING_RATIO`, the declared `tracing.sampling_ratio`, which the trigger, the bridge and the engine's CronJobs read |
| `env-template.yaml` | a patch of the harness's SealedSecret `hermes-credentials`: `default.env` and each person's `<handle>.env`, as templates of its sealed keys |
| `routes.yaml` | the gateway's routes file, a key of the ConfigMap `agentgateway-routes`; it names every key as a variable and every credential as a file, and holds none (see [The gateway's routes](#the-gateways-routes)) |
| `routes.values.yaml` | an additional values file of the gateway's chart: `config.localXdsPath`, each key the routes name as a variable from the SealedSecret `agentgateway-keys`, the reloader's annotation for that Secret, and the routes and every Secret a route attaches from, mounted whole and read-only under `/etc/agentgateway/` |

Every file opens with ``# generated by `task agents:create-harness-config`, do not edit`` and is written whole; a person not rendered has no `profiles/<handle>/`. The settings file may not carry a key the generator owns, nor a key it renders into the settings' `discord` or `gateway` block. A dormant use case renders nothing, and neither does a dormant person, a tool server only dormant use cases name or a model source no profile runs on.

### The default profile and the persons on Discord

Without a person on Discord, the default profile serves the chat. With one, the bot answers through the default profile, which:

- routes each Discord person's messages to their profile: one `gateway.profile_routes` entry per person, `user_id: ${env:DISCORD_USER_<HANDLE>}`;
- lets only them write (`discord.allow_from`), from the same references, so a missing id leaves its person ignored, never misrouted to the default profile;
- gives the `discord: admin` persons every slash command (`allow_admin_from`, `group_allow_admin_from`) and everyone else `reset`, `stop`, `retry` and `undo` beside Hermes's `/help` and `/whoami` (`user_allowed_commands`, `group_user_allowed_commands`);
- holds no tool server on `discord`: the chat is the persons'.

A person reaches every server they hold on the server's one route, under a key of their own that names them to the gateway.

### The keys

The harness reads every key from its profile's `.env`, never from the pod's environment, and the gateway its routes' keys from variables set from `agentgateway-keys` and the keys it attaches from files. Each value is sealed once per SealedSecret, under its own key, and the template composes the `.env` files from those keys: kubeseal rewrites a SealedSecret whole, so the template is a patch beside it. A variable's value is sealed under the variable's name in lower case and dashed (`LARES_GATEWAY_KEY` under `lares-gateway-key`), a person's own value under the handle and that name (`kim-lares-gateway-key`):

```yaml
spec:
  template:
    data:
      default.env: |
        API_SERVER_KEY='{{ index . "api-server-key" }}'
        DISCORD_BOT_TOKEN='{{ index . "discord-bot-token" }}'
        …
```

The default profile's `.env` names, once each and in this order:

- `API_SERVER_KEY`: the Runs and Jobs API and the probes;
- `DISCORD_BOT_TOKEN`, when a chat use case is enabled;
- `DISCORD_USER_<HANDLE>` of each enabled person on Discord;
- the hook's `secret_env`;
- the `key_env` of each tool server the profile holds: its key for the server's route;
- the key variable of each provider its source and their fallbacks run on.

A person's `<handle>.env` names their own `API_SERVER_KEY`, the shared hook secret, their own key for the route of each tool server they hold, and their own key of each provider their source runs on. No `.env` holds a server's credential. A profile's key for a route is sealed twice, under the same name: in the harness's SealedSecret and in the gateway's. So is the key the gateway calls the bridge with: in the gateway's and in the bridge's.

Sealed Secrets renders a key the SealedSecret lacks as empty, without an error. lares therefore checks in pre-commit that every key a template names is sealed (`task k8s:validate-sealed-templates`).

What each surface may hold follows how Hermes resolves its toolsets:

| Surface      | Rendered list                                                     | Why                                                                                         |
| ------------ | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `discord`    | `hermes-discord` and the chat's servers; once persons are on Discord, none, and in each person's profile their own | without its composite the chat would lose memory and skills; the settings disable `discord`, which any explicit Discord list switches on |
| `api_server` | the event use cases' servers; in a person's profile their own     | an event run reads, nothing else; a person's web chat holds what their Discord chat holds   |
| `cron`       | `hermes-cron`, every read server in use and the schedule use cases' memory servers | what every managed cron job sees, the Jobs API taking no list per job; never a writing server |

A list that would name no server is `no_mcp`: Hermes reads a list without a server name as "every server".

Every `mcp_servers` entry has the same shape: the profile's route on the gateway as `url`, `Authorization: Bearer ${env:<key_env>}`, the `timeout`, and the server's `tools` as `include`, with resources and prompts off — the gateway serves tools only.

### The gateway's routes

The routes file holds one route per tool server a profile holds, at `/mcp/<server>`, and one per connector an enabled person holds, at its `path`. Each route:

- signs the caller in: a tool server's route takes the key of each profile that reaches the server there (`apiKey`, strict) and no other, a person's key carrying their handle as `enduser_id`; a connector's runs Authentik's sign-in (`mcpAuthentication` with the `authentik` provider) and answers the metadata paths beside its path, a registration with the pre-registered `client_id`;
- lets through only its `tools`, one `mcpAuthorization` rule per entry: a name by itself, a trailing glob by its prefix, any other `*` and `?` as an anchored pattern; an entry with `[` is refused. A connector's route lets through only the persons holding it, by the token's `preferred_username`: anyone else Authentik signs in is refused at the gateway, before the call to the server;
- attaches the server's `credential` from the file `/etc/agentgateway/credentials/<secret>/<key>`;
- names the caller to a server in the cluster (a host under `.svc.cluster.local`) on every call: `X-Client-Id`, the route's name, and `X-Enduser-Id`, the key's `enduser_id` or the token's `preferred_username`. The gateway sets both after copying the caller's headers, so a value the caller sent is overwritten, and one with no person behind it is removed. A server outside the cluster, such as a vendor's hosted one, is told nobody;
- keeps no session upstream (`statefulMode: stateless`): a session would keep the credential it started with past a renewal.

A route's keys are variables the gateway expands when it loads the file (`${LARES_GATEWAY_KEY}`, the sealed key's name upper case and underscored), set from `agentgateway-keys` at start; a resealed key rolls the gateway through the reloader. A connector's `issuer` is expanded the same way: lares sets the cluster's `AUTHENTIK_URL` beside the rendered values. Every credential is a file the gateway watches, and kubelet swaps a changed Secret in whole, so a renewed token or bridge key reaches the next call without a restart. Every call is a span to `gateway.otlp_endpoint`, kept at `tracing.sampling_ratio`.

## The cron jobs

The harness runs the schedules; its own cron ledger, incidents and overdue gauges are the signal that one did not run. The trigger owns the job set. lares mounts the rendered `cron-jobs.yaml` beside the use-case file, and at startup the trigger lists the harness's jobs through the Jobs API (`/api/jobs`, paused ones included) and brings the managed ones in line with it:

| In the harness                              | What the reconcile does                                                  |
| ------------------------------------------- | ------------------------------------------------------------------------ |
| a declared job is missing                   | creates it                                                               |
| a declared job's schedule, prompt, skills or delivery differ | updates those fields in place; the job keeps its id     |
| a declared job is as declared               | nothing — rewriting a schedule drops an occurrence not yet run           |
| a `lares:` job nothing declares, or a second job of one managed name | deletes it                                      |
| any other job, and whether a managed job is paused | nothing: pausing a job is a person's lever                        |

The pod rolls when the rendering changes, so startup is the only moment the set changes. The harness rolls on the same commit, so a reconcile the harness does not answer is tried again every `RECONCILE_RETRY_SECONDS` until one goes through. One it answers with a refusal — a schedule it cannot parse, say — is logged as an error and counted as `refused`, and not tried again: the declaration changes only with a new commit, which rolls the pod. A job set rendered from another declaration than the mounted use-case file stops the pod at startup.

The Jobs API creates a job from its name, schedule, prompt, skills and delivery, and takes no tool list and no model per job. Every managed job therefore sees the cron surface — every read server in use and the memory servers the schedule use cases name — and runs on the harness's default model. The loader refuses an enabled schedule use case that would need more: one naming a writing server, or pinning a model. Such a use case stays dormant until the harness can carry it. A memory server on the cron surface reaches every job, so the loader also refuses an enabled schedule use case without `memory: true` beside one that names it.

## Starting a use case from chat

The trigger's own API sits on the receiver's port. The bridge forwards its `start_run` tool here; the tool sits on a tool server of its own with `access: request`, so the chat holds it and no event or cron run can start further runs. Every request carries the key both pods share as `Authorization: Bearer <key>`.

`POST /api/runs` with `{"use_case": …, "subject": …}` or, for a schedule, `{"use_case": …, "focus": …}`:

| Use case                    | What happens                                                                                                  | Answer |
| --------------------------- | ------------------------------------------------------------------------------------------------------------- | ------ |
| an event use case           | runs on the episode `subject` names, through the Runs API, with the filter skipped and everything else as on an event: the claim, the daily cap, the retry, the delivery | `202` with `run_id`, `subject_key` and the `output` targets, once the row is claimed |
| a schedule use case         | its managed job runs now (`POST /api/jobs/{id}/run`); it takes no subject. `focus`, what the person asked it to look at (at most 500 characters), goes into that one run's prompt, never into the job's; an event use case takes none. Its rows of the day count against its cap, and a job a person paused is not run: the harness's run-now would resume it for good | `202` with `job_id`; `429`, `409`; `400` when the harness blocks the focus |
| the chat, a dormant use case, one not declared | nothing                                                                    | `400`, `409`, `404` |

A run on an episode records `trigger = message` and the subject key `<episode id>:message:<when it was asked, UTC>`, so every request is a run of its own — the skill then says what got worse since the last one. A request while a run asked for on the same episode is still queued or running is that run, answered `409` with its id: the model sending its call twice, or a person asking before the answer came. The day's spent runs answer `429` with the `capped` row's id. The run's input is the episode as the engine recorded it (`episode_id`, `fault`, `subject`, `severity`, `requested_at`), read from `episodes`; an episode the table does not hold answers `404`.

A job run now has no execution id yet, so the trigger notes the request in memory, and the job's first cron turn to end after it is written with `trigger = message` — a scheduled turn already under way when the request came takes it instead, and the requested one says `schedule`. A pod that restarts in between leaves that row on `schedule`. A requested run on an episode that a stopped pod left open is closed at the next start as `trigger_restarted` and reported like any failed run: nothing redelivers a request.

`POST /api/runs` with `{"use_case": …, "output": …}` instead feeds a run by hand: the text is taken as what a run of that use case wrote, kept in a row of its own (`trigger = manual`, subject kind `none`, key `manual:<UTC>`), and delivered to every target the use case declares exactly as a run's text would be — a dormant use case's included, since no model is asked. It is how a delivery is tried live before a skill writes for it, and the bridge's `start_run` never sends it. The answer comes once the text is delivered:

```json
{"use_case": "propose-faults", "run_id": 42, "status": "completed",
 "output_ref": ["https://github.com/alexander-zimmermann/lares/pull/2250"]}
```

`failed` with an `error` when a target refused, and `AgentRunFailed` raised as for any run. A hand-fed run takes no subject, spends none of the day's runs, and is refused `409` when the use case declares a target this trigger is not set up to deliver.

`POST /api/memory` with `{"use_case": …, "text": …}` appends the note to the use case's row in `agent_memory`, if the use case declares `memory: true`. The row is then cut from the front to 8192 bytes, whole lines at a time: the oldest notes go first, the newest stays whole. A note larger than that alone is refused.

Every refusal is `{"error": "<reason>"}`, so the chat can say why; `503` when the ledger or the harness did not answer.

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
                          cost, duration, tool_trace (count + calls)
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
| `wiki_page` | Writes the page the run's page block names into Wiki.js, under the key whose group may write pages: creates it, or replaces content and title of the page at that path. | `wiki:<locale>/<path>`          |
| `github_pr` | Opens each `~~~github_pr` block as a pull request, as the write App: a fresh branch off the default branch, the diff applied in one commit, the label `agent/proposal` and the block's labels. | the pull request's URL, state `open` |
| `github_issue` | Opens each `~~~github_issue` block as an issue with the block's labels.                                                          | the issue's URL                 |
| `github_comment` | Posts each `~~~github_comment` block as a comment on the issue or pull request it names.                                      | the comment's URL               |

Discord takes 2000 characters a message. A text that fits goes as it is; a longer one goes as the cause and its proof lines (the `-# ` lines the skill writes), then the rest in a second message. A part still too long is cut on a line and ends in `… (run <id>)`: the row holds the whole text.

The mail's subject names what was measured and where — `[Explain] <fault sentence up to its dash> · <channel>` — and its body is the explanation with its proof lines as a plain list, a footer with model, tokens, cost (left out when the run cost nothing, as on a subscription, or its cost is unknown) and duration, and the link to the episode on the dashboard:

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

A run that delivers to the wiki opens with its sentence like every run — it is the row's `tldr` — and then names its page in a block, the page itself following to the end of the text:

```
Wartungsplan auf den Stand vom Oktober gebracht.

---
path: haus/wartungsplan
title: Wartungsplan
---
# Wartungsplan
…
```

The block is checked before the wiki is called: exactly `path` and `title`, both non-empty, and a page after it. One that does not hold is a refusal like any other, with the reason as the row's error. The path is looked up in `WIKIJS_LOCALE`; a new page is created published and without tags, an existing one keeps its description, tags and publish flag, because Wiki.js resets whatever an update leaves out; a publish window set in the editor is not in the page list and is cleared. Every update leaves the previous revision in the page's history.

### GitHub

The model never holds a GitHub write: it names what it wants opened in one fenced block per pull request, issue or comment at the end of its text, and the trigger opens it as the write App ([ADR 0005 in lares](https://github.com/alexander-zimmermann/lares/blob/main/docs/adr/0005-github-reads-through-the-official-server-writes-as-deliveries.md)). A block opens with `~~~` and its target and closes with a line `~~~` of its own, so the backtick fences a body quotes never end it:

````
Two proposals for the laundry room.

~~~github_pr
repository: lares
path: kubernetes/applications/lares-diagnostics-engine/base/config/faults.yaml
title: Let the dryer run five hours before appliance_runtime fires
labels: [topic/smart-home]
body: |
  Five of six judged dryer episodes in eight weeks were `nonsense`: eco runs take 4.2 to 4.6 h.

  ```diff
  -        max_run_hours: 4
  +        max_run_hours: 5
  ```
diff: |
  --- a/kubernetes/applications/lares-diagnostics-engine/base/config/faults.yaml
  +++ b/kubernetes/applications/lares-diagnostics-engine/base/config/faults.yaml
  @@ -191,2 +191,2 @@
         KG.Hauswirtschaftsraum.K3-L1.Trockner:
  -        max_run_hours: 4
  +        max_run_hours: 5
~~~
````

| Target           | Fields                                                                   |
| ---------------- | ------------------------------------------------------------------------ |
| `github_pr`      | `repository`, `path`, `title`, `body`, `diff`, `labels` (may be left out) |
| `github_issue`   | `repository`, `title`, `body`, `labels` (may be left out)                 |
| `github_comment` | `repository`, `number` (the issue or pull request), `body`                |

`repository` is a bare name under `GITHUB_OWNER`; the App's installation decides which it may write to. A title is one line. Every block is checked before anything leaves — for any target of the run, a wiki page or a message included — and one that does not hold refuses its target and keeps every other one from being written to:

- its fields, as above; a path inside the repository, without `./` or `../`;
- every label against the repository — GitHub would create an unknown one on the fly, and the next label sync in lares would delete it again;
- for a pull request, its diff against the file on the default branch's head: every hunk's context and removed lines must stand there exactly as written, at the line its header names or, when the model miscounted, at the one place they fit after the previous hunk — never at one of several. Header line counts are not checked, the lines are, so a hunk that only adds lines needs a context line to show where they go, unless the file is empty. One diff touches one file, the one `path` names; a new file starts from `/dev/null`. A diff that changes nothing is refused too;
- for a comment, that the issue or pull request exists.

A run has at most three blocks for one target. The blocks are cut out of the text the other targets carry: a wiki page, a message or a mail never shows them. One with nothing for a target this time says so in a single block, `none: <why>`; a text without any block for a declared target fails the run, so a model that forgot the format is never silence. A pull request's branch is `agent/<use case>/run-<run id>-<n>`, its commit message the title, and its body, like an issue's or a comment's, ends with the use case, the run and the model, so it leads back to its row. A write that breaks halfway keeps what it opened in the row: a pull request whose labels failed is still named there.

A target that refuses does not stop the next one from trying. The run then closes `failed` with each refusal's raw text as the error, keeps its text and the refs of everything that was created — the first message of a split post included — and raises `AgentRunFailed` like any failed run. The model is not asked again: running it twice would not change what Discord or the relay make of the answer. A run the harness reports completed but without any output fails before delivery, as `unknown`.

The targets the enabled event and schedule use cases declare are built, and one of them without its settings, or one this trigger does not deliver (`alert`), stops the pod at startup. Every other target is built when its settings are there, for a hand-fed run of a dormant use case.

## The read-back

A pull request starts `open` in its row. Once a day — at startup, then every `READ_BACK_INTERVAL_SECONDS` — the trigger asks GitHub about every output the ledger holds as `open` and writes `merged` or `closed` into its position of `output_state`, so the merged share of Propose is a count over the ledger. A pull request GitHub does not answer for stays `open`, is counted as `failed` and asked about again the next day; a round that breaks altogether is logged and waits for the next.

## When a run fails

Nothing fails silently. A failed run is retried once when the failure fixes itself, and reported when it does not:

| Class                | Retry | What it is                                                              |
| -------------------- | ----- | ------------------------------------------------------------------------ |
| `rate_limited`       | yes   | 429, a quota throttle.                                                   |
| `timeout`            | yes   | The model source or the harness took too long to answer one call.        |
| `network_error`      | yes   | The harness could not reach the model source.                            |
| `hermes_unreachable` | yes   | The harness itself did not answer, or answered 5xx.                      |
| `interrupted`        | yes   | Hermes restarted before the run settled.                                 |
| `credits_exhausted`  | no    | The account is empty; xAI sends it as a 403, so it outranks auth.        |
| `auth_failed`        | no    | A key or token the source refused.                                       |
| `budget_exhausted`   | no    | The run spent its tool calls or its minutes.                             |
| `invalid_config`     | no    | A model, provider or request the harness does not accept.                |
| `unknown`            | no    | Nothing above matched; the raw error in the alert says what it was.      |
| `trigger_restarted`  | no    | A redelivered event found its row still open: this pod died mid-run.     |
| `delivery_failed`    | no    | The run completed, but a declared target refused its output.             |

The harness reports a failed run as free text, so the class is read off the text by rules that follow the harness's own cron classifier. The retry starts after `RETRY_DELAY_SECONDS` on the same row, with `attempt = 2` and its own idempotency key — the ledger key with `/2` appended, because the harness replays the run it holds for a key it has seen, failed or not.

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

A row that a dead pod left `queued` or `running` is not started again when its event is redelivered — the harness may still be working on it — but closed as `trigger_restarted` and reported the same way. A row no event comes back for — a run asked for in chat, a cron or hand-fed run whose text was still being delivered — is closed so at the next start.

## Chat and cron runs

The harness starts two kinds of run on its own: a person's turn in Discord or the web chat, and a cron job coming due. The trigger hears of them as they happen, through the harness's outbound hook (`hooks.outbound` in its configuration), which posts four of its lifecycle hooks to `POST /hooks/hermes`, signed with HMAC-SHA256 over the raw body (`X-Hermes-Signature-256: sha256=<hex>`) under a secret both pods read:

- `post_api_request`, once per call to the model: its tokens, the model and its source, when it started and ended, the tools it asked for with their arguments, and its reply. Each call is added to its turn's tally in memory; a background review's is booked at once (below).
- `post_tool_call`, once per tool the model asked for, as the tool returns: its name, its call id, how long it took and whether it failed. The tool's arguments and result come along and are dropped on arrival. A tool joins the tally its model call opened; one of no tally is left alone.
- `api_request_error`, once per failed attempt at a call, whether the harness tries again, falls back or gives up: the model and source it asked, the status they answered and the harness's class of the failure (`auth`, `rate_limit`, `model_not_found`, …). The request it sent, the person's message included, and the error in the source's words come along and are dropped on arrival. The attempt joins its turn's tally.
- `on_session_end`, which despite its name fires once per turn, after that turn's calls: whether it completed, and why it stopped. It writes the turn's row from the tally.

Whose row a turn is, the end's top-level `profile` and its platform decide. The default profile is the machines'; every other profile is the person of that handle:

| Profile   | Platform      | Use case                                                 | Row                                                                    |
| --------- | ------------- | -------------------------------------------------------- | ----------------------------------------------------------------------- |
| `default` | `api_server`  | —                                                        | none: the event run it belongs to takes its calls (see below)           |
| `default` | `cron`        | the enabled `schedule` use case its job is named after   | trigger `schedule` (`message` when the chat asked it to run now), subject kind `none`, key `<job id>:<execution id>` |
| `default` | `discord`     | the one enabled `message` use case                       | an odd turn while a person is on Discord (below); without one, the chat's row of nobody, text kept |
| a person  | `discord`     | the one enabled `message` use case                       | trigger `message`, subject kind `chat`, key `<session id>:<turn>`, `enduser_id` the handle, `surface = discord` |
| a person  | `api_server`  | the one enabled `message` use case                       | the same, `surface = web`: the web chat                                 |
| any       | anything else | —                                                        | none                                                                    |

A machine's row leaves `enduser_id` and `surface` NULL, a run a person asked for through `start_run` included. A person who does not `keep_answers` gets rows without `text`, `tldr` and the tools' arguments; the model, its source, the tokens, the cost and the tools' names stay.

Three turns should not come at all. Each is counted in `agent_trigger_odd_turns_total` with its kind, for an alert to fire on, and logged as a warning:

| Kind                 | The turn                                                       | Row                                                         |
| -------------------- | -------------------------------------------------------------- | ------------------------------------------------------------ |
| `undeclared_profile` | a chat turn of a profile no enabled person runs on: undeclared, or a dormant person's | the chat's, `enduser_id` the profile's name, numbers only |
| `discord_on_default` | a Discord turn on the default profile while a person is on Discord, so it got past the routes | the chat's, `enduser_id` NULL, `surface = discord`, numbers only |
| `cron_on_person`     | a cron turn on a person's profile (on an undeclared one it is `undeclared_profile`) | none: only the default profile runs the declared jobs |

A cron run whose use case delivers somewhere other than `stored` — a pull request, a page, a mail — is delivered by the trigger, as an event run is: the hook is answered first, the row is written `running` with the turn's answer as its text, and the delivery closes it, `failed` with `AgentRunFailed` when a target refuses. A turn that completed without an answer fails the same way. A chat turn is answered by the harness in its own conversation and delivered nowhere.

The harness mints cron job ids itself, so a managed job carries its use case in its name: `lares:propose-faults`. A job without that prefix, or one naming a use case the file does not enable as a schedule, is left alone.

The row holds what the turn's calls added up to: the tokens the model read (cache included) and wrote, the tools it asked for, the time from the first call's start to the last call's end, the model and model source of the last call — a fallback shows up there — and the last call's reply as the answer. Nothing is read off the session record, whose counters run over a whole conversation. Each call is priced by the declared source step of its profile's source that served it, the default profile's or the person's (see [The model sources](#the-model-sources)): a turn on the subscription costs 0, one that moved to a priced fallback costs what its calls on it did. A turn whose calls came in before a restart and whose end came after still gets its row, without the tally.

`tool_trace` records which tools a run asked for, never what they returned:

```json
{"tool_count": 1,
 "calls": [{"call": 1, "tokens_in": 42000, "tokens_out": 300, "seconds": 3.0,
            "tools": [{"name": "list_episodes", "arguments": "{\"state\":\"open\",\"days\":7}"}]},
           {"call": 2, "tokens_in": 48000, "tokens_out": 300, "seconds": 6.5, "tools": []}]}
```

Arguments are cut at 300 characters.

### Failed turns

A turn whose last call failed for good sends no `on_session_end`: the harness returns before it finishes the turn, and the person reads an error. Such a turn's tally ends with a failed attempt. Once it has sent nothing more for `FAILED_TURN_GRACE_SECONDS`, longer than the 10 minutes the harness may wait before it tries a call again, the sweep closes it as its end would have, `failed`. A turn whose end comes after a failed last call is closed by its end the same way. Its row names the source that failed it in `model_source` and `model`, and says in `error` what it answered:

```
the last model call to openai-api failed with 401 (auth)
```

A source that failed without an HTTP status (Codex names a quota inside a 200 answer as a word) is named without one. A chat turn that failed so counts in `agent_trigger_failed_chat_turns_total` by the person and the harness's class of the failure (`error_type`), for `AgentChatFailed` to fire on: the person got an error instead of an answer. A turn answered after a failed attempt, by a retry or the fallback, is not failed; the attempt shows in its trace. A failed attempt of a background review answers nobody and is left alone. A failure before the first call, with no login or an empty key, fires no hook at all; lares reads it from the harness's log.

An event run gets its calls the same way: its turn comes in on the `api_server` platform under the session the Runs API gave the run, and the event path collects them by that session once the run is over, waiting up to `TURN_WAIT_SECONDS` for the turn's end to arrive. Past the wait the row keeps the count alone.

### Background reviews

After a chat answer the harness may review the person's profile notes and skills in the background: the notes every `memory.nudge_interval`-th message of a session, the skills once enough tool rounds have built up. It forks the review into the answer's session, without a task id, so the review's calls run under a task of their own, and the fork never sends `on_session_end`. A chat call whose task is not its session is such a review. Each of its calls is added at once to the session's newest chat row:

| Column              | What it holds                                                                    |
| ------------------- | -------------------------------------------------------------------------------- |
| `review_tokens_in`  | What the review's calls read, cache included.                                    |
| `review_tokens_out` | What they wrote.                                                                 |
| `review_cost`       | What they cost, each call priced like any other.                                 |
| `review_text`       | The review's last reply ("Nothing to save."); empty for a person who does not `keep_answers`. |

An unknown part leaves its sum unknown, as on the row's own columns. The row stays one answered message, so the day's runs are unchanged, and a person's spend is `cost + review_cost`. A redelivered call is booked once. A review in a session no row holds, because its answer's end never came through, is in no row, and its calls are counted in `agent_trigger_orphaned_calls_total`.

The web chat has to send its session header (`X-Hermes-Session-Id`): without it an API-server turn runs under a task of its own and reads as a review.

Every `SWEEP_INTERVAL_SECONDS`, a sweep closes the failed turns past their grace and gives up what no end came for within an hour. The calls of a turn that is no review and never ended are counted in `agent_trigger_orphaned_calls_total` by the profile's person, for an alert to fire on.

What the receiver answers is what the harness acts on — it sends a delivery at most twice, the second time only after a connection error or a 5xx:

| Answer | When                                                                                 |
| ------ | ------------------------------------------------------------------------------------ |
| `200`  | The call is counted or booked or the row written, either was already (a replay), or the turn is not ours. |
| `401`  | The signature is missing or wrong. Counted, never parsed.                            |
| `400`  | The body is none of the four hooks.                                                  |
| `503`  | The ledger or the Jobs API did not answer; the second try may succeed.              |

## Traces

Every turn and every event run is one trace, and its row names it in `trace_id` when the trace was sampled. The harness emits no spans of its own for a turn, so the trigger writes them from the hooks' own times once the turn is over:

```
invoke_agent messenger            gen_ai.conversation.id, lares.run.id, enduser.id, lares.surface
├─ chat gpt-6-sol                 model source, model, tokens in and out
├─ execute_tool list_episodes     tool name, call id; error.type when it failed
└─ chat gpt-6-sol
```

A failed attempt at a call is a `chat` span too, failed with the harness's class of the failure as `error.type`; every attempt at one call starts when its first did. A turn that failed ends its root failed with `error.type = turn_failed`.

The root runs from the first call's start to the turn's end, and is started before the row is written so the row can carry its id. A person's turn names them in `enduser.id` and where they wrote in `lares.surface`, as their row does; a machine's names neither. A cron turn is the same trace under its use case. A turn that never ended is given up after an hour as a trace of its own, failed with `error.type = turn_without_end`; a person's still names them. A background review's calls and tools join its answer's trace as they come, under the root, after the answer's end; a review that follows a restart is booked, but not traced, as the answer's root is gone with the process.

An event run continues the trace of the engine's run that published the event, from the `traceparent` in the message's NATS headers:

```
detect-faults (engine) ─► send episode.appeared ─► process episode.appeared
                                                     └─ run explain-episode          one per attempt
                                                        ├─ POST                      the Runs API
                                                        ├─ await_run                 the polling
                                                        │  └─ GET …
                                                        ├─ invoke_agent explain-episode   the turn, from the hook
                                                        ├─ UPDATE agent_runs         the row write
                                                        └─ deliver explain-episode   every declared output
```

`POST /api/runs` is a server span that continues the caller's `traceparent`; the run it starts stays in that trace after the answer. Log lines written inside a span carry its `trace_id` and `span_id`, and those of a person's turn their handle as `enduser.id`.

A span carries names and numbers only: never a prompt, an answer, or what a tool was asked or returned. A new trace is kept at `TRACING_SAMPLING_RATIO`, decided by its id; one that came in keeps the caller's decision.

## Configuration

Environment variables; every secret can arrive as a mounted file instead of a literal.

| Variable                                    | Default                                            | What it is                                        |
| ------------------------------------------- | -------------------------------------------------- | -------------------------------------------------- |
| `USE_CASES_FILE`                            | `/etc/lares-agent-trigger/use-cases.yaml`          | The declared use cases.                            |
| `PERSONS_FILE`                              | `/etc/lares-agent-trigger/persons.yaml`            | The model sources and their prices, and the persons. |
| `CRON_JOBS_FILE`                            | `/etc/lares-agent-trigger/cron-jobs.yaml`          | The job set rendered from them.                    |
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
| `RECONCILE_RETRY_SECONDS`                   | `60.0`                                             | How long an unanswered reconcile waits for the next try. |
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
| `WIKIJS_URL`                                | —                                                  | Wiki.js base URL; needed by the `wiki_page` output. |
| `WIKIJS_TOKEN_FILE`                         | —                                                  | A Wiki.js API key whose group has `read:pages` and `write:pages`. |
| `WIKIJS_LOCALE`                             | `en`                                               | The locale a page is looked up and created in.     |
| `WIKIJS_REQUEST_TIMEOUT_SECONDS`            | `15.0`                                             | Per-request timeout against Wiki.js.               |
| `GITHUB_APP_ID` / `GITHUB_APP_INSTALLATION_ID` | —                                               | The write App and its installation; needed by the GitHub outputs. |
| `GITHUB_APP_PRIVATE_KEY_FILE`               | —                                                  | The App's private key (PEM). Half an App stops the pod. |
| `GITHUB_OWNER`                              | `alexander-zimmermann`                             | Whose repositories a block's `repository` names.   |
| `GITHUB_API_URL`                            | `https://api.github.com`                           | GitHub's REST API.                                 |
| `GITHUB_REQUEST_TIMEOUT_SECONDS`            | `30.0`                                             | Per-request timeout against GitHub.                |
| `READ_BACK_INTERVAL_SECONDS`                | `86400.0`                                          | How often the open pull requests are looked up.    |
| `HTTP_PORT`                                 | `8080`                                             | The hook receiver and the API.                     |
| `HOOK_SECRET_FILE`                          | —                                                  | The HMAC secret the harness signs deliveries with. |
| `API_KEY_FILE`                              | —                                                  | The key the API takes, at least 32 characters; the bridge holds it too. |
| `TURN_WAIT_SECONDS`                         | `5.0`                                              | How long a finished event run waits for its calls. |
| `SWEEP_INTERVAL_SECONDS`                    | `60.0`                                             | How often the turns whose end never came are given up. |
| `FAILED_TURN_GRACE_SECONDS`                 | `900.0`                                            | How long a turn whose last call failed may send nothing more before it is closed as failed. |
| `METRICS_PORT`                              | `9090`                                             | `/metrics` and `/healthz`.                         |
| `LOG_LEVEL` / `LOG_FORMAT`                  | `INFO` / `json`                                    | Logging.                                           |
| `TRACING_ENDPOINT`                          | —                                                  | OTLP/HTTP collector base URL; unset keeps it off.  |
| `TRACING_SAMPLING_RATIO`                    | `0.1`                                              | The share of new traces kept; lares renders it from the declaration. |

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
| `agent_trigger_hook_events_total`       | `outcome`          | Hook deliveries: `counted` (a model call or a tool), `recorded`, `booked` (a background review's call), `handed_over` (an event run's turn ended, kept for it), `duplicate`, `orphaned` (a review's call no row holds), `ignored`, `refused`, `invalid`, `error`. |
| `agent_trigger_recorded_runs_total`     | `use_case`, `status`, `enduser_id` | Chat and cron runs written from the hook, by the status of their row and the person it answered (empty for the machines). |
| `agent_trigger_failed_chat_turns_total` | `enduser_id`, `error_type` | Chat turns whose last model call failed for good, by the person and the harness's class of the failure, as on the failed call's span (see [Failed turns](#failed-turns)). |
| `agent_trigger_odd_turns_total`         | `kind`, `enduser_id` | Turns that should not have come: `undeclared_profile`, `discord_on_default`, `cron_on_person` (see [Chat and cron runs](#chat-and-cron-runs)). |
| `agent_trigger_cost_usd_total`          | `use_case`, `source`, `step`, `enduser_id` | USD the runs' calls cost at the declared prices, background reviews' included, by model source, step (`primary`, `fallback`) and the person the run answered (empty for the machines). |
| `agent_trigger_unpriced_calls_total`    | `use_case`, `provider`, `model`, `reason` | Calls without a cost, `undeclared`, `no_usage` or `no_calls`; their row's cost is NULL. |
| `agent_trigger_answers_total`           | `use_case`, `source`, `step` | Answered runs, by the step of the model source that gave the answer: `primary`, `fallback`, or `undeclared`. |
| `agent_trigger_orphaned_calls_total`    | `enduser_id`       | Model calls spent and in no row: of a turn that never reported its end within an hour, or of a background review in a session no row holds, by the profile's person (empty for the machines). |
| `agent_trigger_reconciles_total`        | `outcome`          | Reconciles of the cron jobs: `done`, `failed` (not answered, tried again) or `refused`. |
| `agent_trigger_cron_jobs_total`         | `action`           | Managed jobs the reconcile `created`, `updated` or `deleted`. |
| `agent_trigger_api_requests_total`      | `route`, `code`    | Requests to the API (`runs`, `memory`), by the status code they were answered. |
| `agent_trigger_read_backs_total`        | `use_case`, `state` | Open pull requests looked up by the read-back: `open`, `merged`, `closed`, or `failed` when GitHub did not answer for one. |

`/healthz` is NATS- and ledger-gated. A harness outage is deliberately not part of it: that is a failed run with its own alert, never a restart loop.

## Tests

The generator is pure and tested apart: the declarations and settings in `tests/rendering/` go in, and every rendered file must equal its expected file in `tests/rendering/expected/`. After a deliberate change, `UPDATE_RENDERING=1 uv run pytest tests/test_generate.py` rewrites them for review. The rendered routes are also run as the harness runs them: agentgateway in a container, started as its chart starts it with the rendered values, every variable set from its sealed key, the routes and every credential laid into a volume the way kubelet lays a Secret, with the real GitHub server, a stand-in bridge and a stand-in Authentik behind it under the names the declaration gives them. A key opens its own route only, a tool outside a server's `tools` is neither listed nor callable, each route names its client and the key's person to the bridge and a caller's own such headers never reach it, GitHub answers only with the attached token, a renewed credential reaches a session the harness keeps open without a restart, and the connector points a client without a token at its sign-in, hands out the pre-registered client, serves a declared person's token, refuses anyone else's, and names the token's person whatever headers the client sends.

Everything else meets one seam, at the service's edges. An episode event goes in on a real NATS container with a real durable consumer, and a hook delivery or an API request goes in over HTTP to the app in process, signed or keyed as the harness and the bridge send it; the ledger rows land in a real TimescaleDB container and the spans in an in-memory exporter, sampled at a ratio a test may set; the harness, Alertmanager, Discord, Wiki.js and GitHub are fakes over `respx` — the harness because a run is a model call, its Jobs API keeping a job list with the harness's rules (it mints the ids, takes a create's five fields and an update's whitelist, refuses what the harness refuses, resumes a paused job it runs, hides paused jobs from a plain list), the others so the alert, the message, the page and the pull request are asserted as they would arrive; GitHub keeps its own rules on the App's JWT and tokens, refs, contents writes, pull requests and labels — and the mail lands at an SMTP server in the test process that keeps the relay's one rule, its accepted sender. The failure classes have a table test of their own against the harness's error texts, and so has the diff a pull request carries against the file it changes.

```bash
uv sync --extra dev
uv run pytest
```

Docker is required for the container fixtures.

## Licence

GPL-2.0-or-later. See [LICENSE](LICENSE).
