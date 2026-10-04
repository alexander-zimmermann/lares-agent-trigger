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

`tools` names tool servers, which the same file declares beside the use cases, together with where the harness reports every finished turn:

```yaml
ledger_hook:
  trigger_url: http://lares-agent-trigger.agents.svc.cluster.local:8080
  secret_env: LARES_AGENT_TRIGGER_HOOK_SECRET

tool_servers:
  - name: lares
    url: http://lares-mcp-bridge.lares-mcp-bridge.svc.cluster.local:8080/mcp
    client: lares-agent       # the bridge's machine client the key maps to
    key_env: LARES_MCP_KEY    # where the harness holds that key
    timeout_seconds: 60
    access: read
    tools: [list_*, get_*, query_*, correlate_events, search_wiki]
```

A tool server is the bridge under one machine-client key; `tools` is both the bridge's allowlist for that client and the harness's include list on top of it. `access` is where it may be granted, and the loader refuses an enabled use case that names a server it may not hold:

| `access`  | Granted to                                                                         |
| --------- | ---------------------------------------------------------------------------------- |
| `read`    | any use case                                                                       |
| `write`   | schedule use cases only, and none while the Jobs API gives a cron job no tool list of its own (see [The cron jobs](#the-cron-jobs)) |
| `request` | the chat only: what a person asks for in conversation — a write request approved elsewhere, a run started now |
| `memory`  | use cases with `memory: true` only: their own memory, read and appended through the bridge. A surface the harness shares — all event runs, all cron jobs — carries it only when every enabled use case on it keeps a memory |

A tool sits on one server only: the loader refuses a declaration where an entry of one bridge server matches an entry of another, so `get_*` on the read server and `get_write_request` on the gate's request server cannot stand side by side — the read server then lists its `get_` tools by name.

One more kind of server reads GitHub: the official [GitHub MCP server](https://github.com/github/github-mcp-server), which the harness starts as a process of its own, not the bridge:

```yaml
  - name: github
    kind: github                                      # a bridge entry needs no kind
    command: /opt/github-mcp/github-mcp-server        # the binary, inside the harness container
    app_id: 1234567                                   # the GitHub App it signs in as
    installation_id: 89012345
    private_key_file: /etc/github-reader/private-key  # the App's key, mounted from a secret
    toolsets: [repos, issues, pull_requests]          # the server's own --toolsets
    timeout_seconds: 60
    access: read
    tools: [get_*, list_*, search_*, issue_read, pull_request_read]
```

It has no bridge client and no allowlist on the bridge. It signs in as a GitHub App that may only read, minting and renewing the installation tokens itself, runs `--read-only`, and `tools` is the harness's include list on top. Its `access` is `read` and nothing else — a GitHub write is a delivery of the trigger, never a tool the model holds.

A dormant use case may name tool servers and event sources (`alert`, `pull_request`, `ets_export`, `new_device`) that are not built yet; enabling it is refused until they are.

Schema and loader live in this package because the generator renders the harness configuration from the same models: one schema for rendering and for runtime.

## The generator

`lares-agent-trigger generate` renders the use-case file into what the harness and the bridge read. lares runs it at the trigger's deployed tag (`task agents:create-harness-config`) and checks in pre-commit that the committed files match a fresh rendering (`task agents:validate-use-cases`).

```bash
lares-agent-trigger generate \
  --use-cases use-cases.yaml --settings settings.yaml \
  --hermes-config config.yaml --cron-jobs cron-jobs.yaml --client-tools client-tools.env
```

| Output             | What it holds                                                                                                     |
| ------------------ | ----------------------------------------------------------------------------------------------------------------- |
| `config.yaml`      | the hand-written `settings.yaml` copied whole, then `platform_toolsets`, `hooks.outbound` and `mcp_servers`      |
| `cron-jobs.yaml`   | one job per enabled schedule use case, named `lares:<use case>`, with its schedule, skill, prompt and delivery     |
| `client-tools.env` | `MCP_AUTH_CLIENT_TOOLS`, the bridge's allowlist per machine client of a bridge server in use                      |

Every file opens with ``# generated by `task agents:create-harness-config`, do not edit`` and is written whole. The settings file may not carry a key the generator owns. A dormant use case renders nothing, and neither does a tool server only dormant use cases name.

What each surface may hold follows how Hermes resolves its toolsets:

| Surface      | Rendered list                                                     | Why                                                                                         |
| ------------ | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `discord`    | `hermes-discord` and the chat's servers                           | without its composite the chat would lose memory and skills; the settings disable `discord`, which any explicit Discord list switches on |
| `api_server` | the event use cases' servers                                      | an event run reads, nothing else                                                            |
| `cron`       | `hermes-cron`, every read server in use and the schedule use cases' memory servers | what every managed cron job sees, the Jobs API taking no list per job; never a writing server |

A list that would name no server is `no_mcp`: Hermes reads a list without a server name as "every server".

The GitHub server's entry starts the binary with `stdio --read-only` and the declared toolsets, and hands it the App in `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID` and `GITHUB_APP_PRIVATE_KEY_PATH`. A key file that is missing stops the server before it serves; signed in as an App, it never falls back to an interactive login. Its prompts and resources stay off: the prompts walk the model towards writes it does not hold.

## The cron jobs

The harness runs the schedules; its own cron ledger, incidents and overdue gauges are the signal that one did not run. The trigger owns the job set. lares mounts the rendered `cron-jobs.yaml` beside the use-case file, and at startup the trigger lists the harness's jobs through the Jobs API (`/api/jobs`, paused ones included) and brings the managed ones in line with it:

| In the harness                              | What the reconcile does                                                  |
| ------------------------------------------- | ------------------------------------------------------------------------ |
| a declared job is missing                   | creates it                                                               |
| a declared job's schedule, prompt, skills or delivery differ | updates those fields in place; the job keeps its id     |
| a declared job is as declared               | nothing — rewriting a schedule drops an occurrence not yet run           |
| a `lares:` job nothing declares, or a second job of one managed name | deletes it                                      |
| any other job, and whether a managed job is paused | nothing: pausing a job is a person's lever                        |

The pod rolls when the rendering changes, so startup is the only moment the set changes. The harness rolls on the same commit, so a reconcile the gateway does not answer is tried again every `RECONCILE_RETRY_SECONDS` until one goes through. One it answers with a refusal — a schedule it cannot parse, say — is logged as an error and counted as `refused`, and not tried again: the declaration changes only with a new commit, which rolls the pod. A job set rendered from another declaration than the mounted use-case file stops the pod at startup.

The Jobs API creates a job from its name, schedule, prompt, skills and delivery, and takes no tool list and no model per job. Every managed job therefore sees the cron surface — every read server in use and the memory servers the schedule use cases name — and runs on the harness's default model. The loader refuses an enabled schedule use case that would need more: one naming a writing server, or pinning a model. Such a use case stays dormant until the harness can carry it. A memory server on the cron surface reaches every job, so the loader also refuses an enabled schedule use case without `memory: true` beside one that names it.

## Starting a use case from chat

The trigger's own API sits on the receiver's port. The bridge forwards its `start_run` tool here; the tool sits on a tool server of its own with `access: request`, so the chat holds it and no event or cron run can start further runs. Every request carries the key both pods share as `Authorization: Bearer <key>`.

`POST /api/runs` with `{"use_case": …, "subject": …}`:

| Use case                    | What happens                                                                                                  | Answer |
| --------------------------- | ------------------------------------------------------------------------------------------------------------- | ------ |
| an event use case           | runs on the episode `subject` names, through the Runs API, with the filter skipped and everything else as on an event: the claim, the daily cap, the retry, the delivery | `202` with `run_id`, `subject_key` and the `output` targets, once the row is claimed |
| a schedule use case         | its managed job runs now (`POST /api/jobs/{id}/run`); it takes no subject. Its rows of the day count against its cap, and a job a person paused is not run: the gateway's run-now would resume it for good | `202` with `job_id`; `429`, `409` |
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

A row that a dead pod left `queued` or `running` is not started again when its event is redelivered — the harness may still be working on it — but closed as `trigger_restarted` and reported the same way. A row no event comes back for — a run asked for in chat, a cron or hand-fed run whose text was still being delivered — is closed so at the next start.

## Chat and cron runs

The harness starts two kinds of run on its own: a turn in Discord, and a cron job coming due. The trigger hears of them as they happen, through the harness's outbound hook (`hooks.outbound` in its configuration), which posts two of its lifecycle hooks to `POST /hooks/hermes`, signed with HMAC-SHA256 over the raw body (`X-Hermes-Signature-256: sha256=<hex>`) under a secret both pods read:

- `post_api_request`, once per call to the model: its tokens, the model and its source, when it started and ended, the tools it asked for with their arguments, and its reply. Each call is added to its turn's tally in memory.
- `on_session_end`, which despite its name fires once per turn, after that turn's calls: whether it completed, and why it stopped. It writes the turn's row from the tally.

| Platform      | Use case                                                        | Row                                                                    |
| ------------- | --------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `discord`     | the one enabled `message` use case                              | trigger `message`, subject kind `chat`, key `<session id>:<turn>`       |
| `cron`        | the enabled `schedule` use case its job is named after          | trigger `schedule` (`message` when the chat asked it to run now), subject kind `none`, key `<job id>:<execution id>` |
| `api_server`  | —                                                               | none: the event run it belongs to takes its calls (see below)           |
| anything else | —                                                               | none                                                                    |

A cron run whose use case delivers somewhere other than `stored` — a pull request, a page, a mail — is delivered by the trigger, as an event run is: the hook is answered first, the row is written `running` with the turn's answer as its text, and the delivery closes it, `failed` with `AgentRunFailed` when a target refuses. A turn that completed without an answer fails the same way. A chat turn is answered by the harness in its own conversation and delivered nowhere.

The gateway mints cron job ids itself, so a managed job carries its use case in its name: `lares:propose-faults`. A job without that prefix, or one naming a use case the file does not enable as a schedule, is left alone.

The row holds what the turn's calls added up to: the tokens the model read (cache included) and wrote, the tools it asked for, the time from the first call's start to the last call's end, the model and model source of the last call — a fallback shows up there — and the last call's reply as the answer. Nothing is read off the session record, whose counters run over a whole conversation. A flat subscription bills nothing per call, so `cost` stays empty rather than guessed. A turn whose calls came in before a restart and whose end came after still gets its row, without the tally.

`tool_trace` records which tools a run asked for, never what they returned:

```json
{"tool_count": 1,
 "calls": [{"call": 1, "tokens_in": 42000, "tokens_out": 300, "seconds": 3.0,
            "tools": [{"name": "list_episodes", "arguments": "{\"state\":\"open\",\"days\":7}"}]},
           {"call": 2, "tokens_in": 48000, "tokens_out": 300, "seconds": 6.5, "tools": []}]}
```

Arguments are cut at 300 characters. An event run gets its calls the same way: its turn comes in on the `api_server` platform under the session the Runs API gave the run, and the event path collects them by that session once the run is over, waiting up to `TRACE_WAIT_SECONDS` for the turn's end to arrive. Past the wait the row keeps the count alone.

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
| `TRACE_WAIT_SECONDS`                        | `5.0`                                              | How long a finished event run waits for its calls. |
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
| `agent_trigger_hook_events_total`       | `outcome`          | Hook deliveries: `counted`, `recorded`, `traced` (an event run's turn ended), `duplicate`, `ignored`, `refused`, `invalid`, `error`. |
| `agent_trigger_recorded_runs_total`     | `use_case`, `status` | Chat and cron runs written from the hook, by the status of their row. |
| `agent_trigger_orphaned_calls_total`    | —                  | Model calls of turns that never reported their end within an hour — the harness's own work after an answer, spent and in no row. |
| `agent_trigger_reconciles_total`        | `outcome`          | Reconciles of the cron jobs: `done`, `failed` (not answered, tried again) or `refused`. |
| `agent_trigger_cron_jobs_total`         | `action`           | Managed jobs the reconcile `created`, `updated` or `deleted`. |
| `agent_trigger_api_requests_total`      | `route`, `code`    | Requests to the API (`runs`, `memory`), by the status code they were answered. |
| `agent_trigger_read_backs_total`        | `use_case`, `state` | Open pull requests looked up by the read-back: `open`, `merged`, `closed`, or `failed` when GitHub did not answer for one. |

`/healthz` is NATS- and ledger-gated. A harness outage is deliberately not part of it: that is a failed run with its own alert, never a restart loop.

## Tests

The generator is pure and tested apart: the declaration and settings in `tests/rendering/` go in, and the three rendered files must equal `tests/rendering/expected/`. After a deliberate change, `UPDATE_RENDERING=1 uv run pytest tests/test_generate.py` rewrites them for review. The rendered GitHub entry is also run as the harness would run it: the real server binary, copied out of its image, starts in a container with a key file in place, answers MCP over stdio with the network off, and offers read tools only; without the key it never starts.

Everything else meets one seam, at the service's edges. An episode event goes in on a real NATS container with a real durable consumer, and a hook delivery or an API request goes in over HTTP to the app in process, signed or keyed as the gateway and the bridge send it; the ledger rows land in a real TimescaleDB container; the harness, Alertmanager, Discord, Wiki.js and GitHub are fakes over `respx` — the harness because a run is a model call, its Jobs API keeping a job list with the gateway's rules (it mints the ids, takes a create's five fields and an update's whitelist, refuses what the gateway refuses, resumes a paused job it runs, hides paused jobs from a plain list), the others so the alert, the message, the page and the pull request are asserted as they would arrive; GitHub keeps its own rules on the App's JWT and tokens, refs, contents writes, pull requests and labels — and the mail lands at an SMTP server in the test process that keeps the relay's one rule, its accepted sender. The failure classes have a table test of their own against the gateway's error texts, and so has the diff a pull request carries against the file it changes.

```bash
uv sync --extra dev
uv run pytest
```

Docker is required for the container fixtures.

## Licence

GPL-2.0-or-later. See [LICENSE](LICENSE).
