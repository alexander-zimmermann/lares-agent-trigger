"""Loader tests: every field of a use case and a tool server, dormant entries, refused files."""

from __future__ import annotations

from pathlib import Path

import pytest

from lares_agent_trigger.use_cases import (
    BridgeServer,
    GitHubServer,
    ScheduleTrigger,
    load_use_case_file,
    load_use_cases,
)

VALID = """
ledger_hook:
  trigger_url: http://lares-agent-trigger.agents.svc.cluster.local:8080
  secret_env: LARES_AGENT_TRIGGER_HOOK_SECRET

tool_servers:
  - name: lares
    url: http://lares-mcp-bridge.lares-mcp-bridge.svc.cluster.local:8080/mcp
    client: lares-agent
    key_env: LARES_MCP_KEY
    timeout_seconds: 60
    access: read
    tools: [list_*, get_episode, query_*]

  - name: lares-control
    url: http://lares-mcp-bridge.lares-mcp-bridge.svc.cluster.local:8080/mcp
    client: lares-control
    key_env: LARES_CONTROL_KEY
    timeout_seconds: 60
    access: request
    tools: [request_knx_write, get_write_request]

  - name: lares-memory
    url: http://lares-mcp-bridge.lares-mcp-bridge.svc.cluster.local:8080/mcp
    client: lares-memory
    key_env: LARES_MEMORY_KEY
    timeout_seconds: 30
    access: memory
    tools: [get_memory, append_memory]

  - name: lares-wiki
    url: http://lares-mcp-bridge.lares-mcp-bridge.svc.cluster.local:8080/mcp
    client: lares-wiki
    key_env: LARES_WIKI_KEY
    timeout_seconds: 60
    access: write
    tools: [update_wiki_page]

  - name: github
    kind: github
    command: /opt/github-mcp/github-mcp-server
    app_id: 1234567
    installation_id: 89012345
    private_key_file: /etc/github-reader/private-key
    toolsets: [repos, issues, pull_requests]
    timeout_seconds: 60
    access: read
    tools: [get_*, list_*, search_*, issue_read, pull_request_read]

use_cases:
  - name: explain-episode
    sentence: Explains a new or escalated episode on its own event.
    trigger:
      kind: event
      source: episode
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

  - name: propose-faults
    sentence: Proposes changes to the fault list as pull requests.
    trigger:
      kind: schedule
      cron: "0 3 * * 0"
    skill: lares-propose
    tools: [lares, lares-memory]
    output: [github_pr]
    budget:
      tool_calls: 80
      minutes: 20
      runs_per_day: 1
    language: en
    memory: true
    model: gpt-6-sol
    dormant: "Waits for the GitHub App of #2114."

  - name: messenger
    sentence: Answers a question from the phone.
    trigger:
      kind: message
    skill: lares-answer
    tools: [lares, lares-control, github]
    output: [discord]
    budget:
      tool_calls: 40
      minutes: 10
      runs_per_day: 200
    language: de
    memory: true
    enabled: true
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "use-cases.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_every_field_of_an_event_use_case(tmp_path: Path) -> None:
    explain = load_use_cases(_write(tmp_path, VALID))["explain-episode"]

    assert explain.sentence == "Explains a new or escalated episode on its own event."
    assert explain.skill == "lares-explain"
    assert explain.tools == ("lares",)
    assert explain.output == ("stored", "discord", "mail")
    assert explain.budget.tool_calls == 40
    assert explain.budget.minutes == 10
    assert explain.budget.runs_per_day == 10
    assert explain.language == "de"
    assert explain.memory is False
    assert explain.model is None
    assert explain.is_enabled is True

    trigger = explain.trigger
    assert trigger.kind == "event"
    assert trigger.source == "episode"
    assert trigger.filter == {"appeared": 2, "escalated": 2}


def test_schedule_and_message_triggers_carry_their_own_fields(tmp_path: Path) -> None:
    loaded = load_use_cases(_write(tmp_path, VALID))

    propose = loaded["propose-faults"].trigger
    assert isinstance(propose, ScheduleTrigger)
    assert propose.cron == "0 3 * * 0"
    assert loaded["propose-faults"].model == "gpt-6-sol"
    assert loaded["messenger"].trigger.kind == "message"


def test_a_dormant_entry_stays_in_the_file_with_its_reason(tmp_path: Path) -> None:
    propose = load_use_cases(_write(tmp_path, VALID))["propose-faults"]

    assert propose.is_enabled is False
    assert propose.dormant == "Waits for the GitHub App of #2114."


def test_a_use_case_without_a_state_is_refused(tmp_path: Path) -> None:
    broken = VALID.replace("    enabled: true\n", "", 1)

    with pytest.raises(ValueError, match="exactly one of"):
        load_use_cases(_write(tmp_path, broken))


def test_switching_a_use_case_off_needs_a_reason(tmp_path: Path) -> None:
    broken = VALID.replace("    enabled: true\n", "    enabled: false\n", 1)

    with pytest.raises(ValueError, match="dormant"):
        load_use_cases(_write(tmp_path, broken))


def test_an_unknown_event_kind_is_refused(tmp_path: Path) -> None:
    broken = VALID.replace("        escalated: 2", "        resolved: 2")

    with pytest.raises(ValueError):
        load_use_cases(_write(tmp_path, broken))


def test_an_unknown_field_is_refused(tmp_path: Path) -> None:
    broken = VALID.replace("    language: de\n", "    languge: de\n    language: de\n", 1)

    with pytest.raises(ValueError):
        load_use_cases(_write(tmp_path, broken))


def test_a_chat_names_no_skill_and_everything_else_does(tmp_path: Path) -> None:
    chat = VALID.replace("    skill: lares-answer\n", "", 1)
    assert load_use_cases(_write(tmp_path, chat))["messenger"].skill is None

    broken = VALID.replace("    skill: lares-explain\n", "", 1)
    with pytest.raises(ValueError, match="event use case names its skill"):
        load_use_cases(_write(tmp_path, broken))


def test_a_second_enabled_chat_is_refused(tmp_path: Path) -> None:
    """The harness has one chat surface; a turn it reports must belong to one use case."""
    doubled = VALID + (
        "  - name: messenger-2\n"
        "    sentence: A second chat.\n"
        "    trigger: {kind: message}\n"
        "    tools: [lares]\n"
        "    output: [discord]\n"
        "    budget: {tool_calls: 40, minutes: 10, runs_per_day: 200}\n"
        "    language: de\n"
        "    memory: false\n"
        "    enabled: true\n"
    )

    with pytest.raises(ValueError, match="only one enabled message use case"):
        load_use_cases(_write(tmp_path, doubled))


def test_two_use_cases_of_one_name_are_refused(tmp_path: Path) -> None:
    doubled = VALID + VALID.split("use_cases:")[1]

    with pytest.raises(ValueError, match="explain-episode"):
        load_use_cases(_write(tmp_path, doubled))


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="use-cases.yaml"):
        load_use_cases(tmp_path / "use-cases.yaml")


def test_every_field_of_a_tool_server_and_the_ledger_hook(tmp_path: Path) -> None:
    declared = load_use_case_file(_write(tmp_path, VALID))

    lares = declared.tool_servers["lares"]
    assert isinstance(lares, BridgeServer)
    assert lares.url == "http://lares-mcp-bridge.lares-mcp-bridge.svc.cluster.local:8080/mcp"
    assert lares.client == "lares-agent"
    assert lares.key_env == "LARES_MCP_KEY"
    assert lares.timeout_seconds == 60
    assert lares.access == "read"
    assert lares.tools == ("list_*", "get_episode", "query_*")
    assert list(declared.tool_servers) == [
        "lares",
        "lares-control",
        "lares-memory",
        "lares-wiki",
        "github",
    ]

    assert declared.ledger_hook.trigger_url == (
        "http://lares-agent-trigger.agents.svc.cluster.local:8080"
    )
    assert declared.ledger_hook.secret_env == "LARES_AGENT_TRIGGER_HOOK_SECRET"
    assert declared.use_cases == load_use_cases(_write(tmp_path, VALID))


def test_an_enabled_use_case_names_only_declared_tool_servers(tmp_path: Path) -> None:
    broken = VALID.replace("    tools: [lares]\n", "    tools: [lares, cluster]\n", 1)

    with pytest.raises(ValueError, match="explain-episode: tools: no tool server named cluster"):
        load_use_cases(_write(tmp_path, broken))


def test_a_dormant_use_case_may_name_a_tool_server_still_to_be_built(tmp_path: Path) -> None:
    waiting = VALID.replace(
        "    tools: [lares, lares-memory]\n", "    tools: [lares, cluster]\n", 1
    )

    assert load_use_cases(_write(tmp_path, waiting))["propose-faults"].tools == ("lares", "cluster")


def test_a_writing_server_is_granted_to_schedule_use_cases_only(tmp_path: Path) -> None:
    broken = VALID.replace("    tools: [lares]\n", "    tools: [lares, lares-wiki]\n", 1)

    with pytest.raises(ValueError, match="explain-episode: tools: lares-wiki writes"):
        load_use_cases(_write(tmp_path, broken))


# Propose as it would run: enabled, still naming its writing server and its model.
_PROPOSE_ENABLED = VALID.replace(
    '    dormant: "Waits for the GitHub App of #2114."\n', "    enabled: true\n", 1
)


def test_a_schedule_use_case_cannot_hold_a_writing_server_yet(tmp_path: Path) -> None:
    """The Jobs API takes no tool list per job: every cron job sees the cron surface only."""
    broken = _PROPOSE_ENABLED.replace("    model: gpt-6-sol\n", "", 1).replace(
        "    tools: [lares, lares-memory]\n", "    tools: [lares, lares-wiki]\n", 1
    )

    with pytest.raises(
        ValueError, match="propose-faults: tools: lares-wiki writes, and the Jobs API"
    ):
        load_use_cases(_write(tmp_path, broken))


def test_a_schedule_use_case_cannot_pin_a_model(tmp_path: Path) -> None:
    broken = _PROPOSE_ENABLED.replace(
        "    tools: [lares, lares-memory]\n", "    tools: [lares]\n", 1
    )

    with pytest.raises(ValueError, match="propose-faults: model: the Jobs API"):
        load_use_cases(_write(tmp_path, broken))


def test_a_schedule_use_case_reading_through_the_cron_surface_runs(tmp_path: Path) -> None:
    reading = _PROPOSE_ENABLED.replace(
        "    tools: [lares, lares-memory]\n", "    tools: [lares]\n", 1
    ).replace("    model: gpt-6-sol\n", "", 1)

    assert load_use_cases(_write(tmp_path, reading))["propose-faults"].is_schedule


def test_a_memory_server_is_granted_to_use_cases_that_keep_one(tmp_path: Path) -> None:
    broken = VALID.replace("    tools: [lares]\n", "    tools: [lares, lares-memory]\n", 1)

    with pytest.raises(
        ValueError,
        match="explain-episode: tools: lares-memory holds a use case's memory, "
        "and explain-episode keeps none",
    ):
        load_use_cases(_write(tmp_path, broken))


def test_a_schedule_use_case_holds_its_memory_through_the_cron_surface(tmp_path: Path) -> None:
    keeping = _PROPOSE_ENABLED.replace("    model: gpt-6-sol\n", "", 1)

    propose = load_use_cases(_write(tmp_path, keeping))["propose-faults"]
    assert propose.is_schedule
    assert propose.tools == ("lares", "lares-memory")


# A second schedule use case beside Propose: the cron surface is the one both jobs see.
_REPORT = """
  - name: energy-report
    sentence: Mails the monthly energy report.
    trigger:
      kind: schedule
      cron: "0 7 1 * *"
    skill: lares-energy-report
    tools: [lares]
    output: [mail]
    budget:
      tool_calls: 40
      minutes: 10
      runs_per_day: 1
    language: de
    memory: false
    enabled: true
"""


def test_a_shared_surface_carries_a_memory_only_when_every_use_case_on_it_keeps_one(
    tmp_path: Path,
) -> None:
    """Every cron job sees the cron surface, so a job that keeps no memory would see Propose's."""
    broken = _PROPOSE_ENABLED.replace("    model: gpt-6-sol\n", "", 1) + _REPORT

    with pytest.raises(
        ValueError,
        match="energy-report keeps no memory, and every cron job sees lares-memory, "
        "which propose-faults holds",
    ):
        load_use_cases(_write(tmp_path, broken))


def test_a_shared_surface_whose_every_use_case_keeps_a_memory_carries_it(tmp_path: Path) -> None:
    keeping = _PROPOSE_ENABLED.replace("    model: gpt-6-sol\n", "", 1) + _REPORT.replace(
        "    memory: false\n", "    memory: true\n"
    )

    assert load_use_cases(_write(tmp_path, keeping))["energy-report"].memory


def test_the_event_surface_is_shared_the_same_way(tmp_path: Path) -> None:
    second_event = """
  - name: explain-episode-again
    sentence: Explains an episode a second way, with a memory of its own.
    trigger:
      kind: event
      source: episode
      filter:
        escalated: 3
    skill: lares-explain-again
    tools: [lares, lares-memory]
    output: [stored]
    budget:
      tool_calls: 40
      minutes: 10
      runs_per_day: 10
    language: de
    memory: true
    enabled: true
"""
    with pytest.raises(
        ValueError,
        match="explain-episode keeps no memory, and every event run sees lares-memory, "
        "which explain-episode-again holds",
    ):
        load_use_cases(_write(tmp_path, VALID + second_event))


def test_a_request_server_is_granted_to_the_chat_only(tmp_path: Path) -> None:
    broken = VALID.replace("    tools: [lares]\n", "    tools: [lares, lares-control]\n", 1)

    with pytest.raises(ValueError, match="explain-episode: tools: lares-control places requests"):
        load_use_cases(_write(tmp_path, broken))


def test_two_tool_servers_on_one_bridge_client_are_refused(tmp_path: Path) -> None:
    """One key, one client, one allowlist: two servers sharing a client would share a ceiling."""
    broken = VALID.replace("    client: lares-memory\n", "    client: lares-agent\n", 1)

    with pytest.raises(ValueError, match="lares-memory: client lares-agent is already"):
        load_use_cases(_write(tmp_path, broken))


def test_a_glob_reaching_another_servers_tool_is_refused(tmp_path: Path) -> None:
    """A tool sits on one server: `get_*` on the read key would hand it the gate's read-back."""
    broken = VALID.replace("[list_*, get_episode, query_*]", "[list_*, get_*, query_*]", 1)

    with pytest.raises(
        ValueError,
        match="lares: get_\\* also grants get_write_request of tool server lares-control",
    ):
        load_use_cases(_write(tmp_path, broken))


def test_one_tool_named_by_two_servers_is_refused(tmp_path: Path) -> None:
    broken = VALID.replace(
        "    tools: [get_memory, append_memory]\n",
        "    tools: [get_memory, append_memory, get_episode]\n",
    )

    with pytest.raises(ValueError, match="also grants get_episode"):
        load_use_cases(_write(tmp_path, broken))


def test_every_field_of_the_github_server(tmp_path: Path) -> None:
    """The official server, signing in as a read-only App: no bridge client."""
    github = load_use_case_file(_write(tmp_path, VALID)).tool_servers["github"]

    assert isinstance(github, GitHubServer)
    assert github.command == "/opt/github-mcp/github-mcp-server"
    assert github.app_id == 1234567
    assert github.installation_id == 89012345
    assert github.private_key_file == "/etc/github-reader/private-key"
    assert github.toolsets == ("repos", "issues", "pull_requests")
    assert github.timeout_seconds == 60
    assert github.access == "read"
    assert github.tools == ("get_*", "list_*", "search_*", "issue_read", "pull_request_read")
    assert load_use_cases(_write(tmp_path, VALID))["messenger"].tools == (
        "lares",
        "lares-control",
        "github",
    )


def test_the_github_server_only_reads(tmp_path: Path) -> None:
    """A GitHub write is a delivery of the trigger, never a tool the model holds."""
    broken = VALID.replace(
        "    access: read\n    tools: [get_*, list_*",
        "    access: write\n    tools: [get_*, list_*",
        1,
    )

    with pytest.raises(ValueError, match="tool server github: the GitHub server only reads"):
        load_use_cases(_write(tmp_path, broken))


def test_the_github_server_takes_no_bridge_client(tmp_path: Path) -> None:
    broken = VALID.replace(
        "    installation_id: 89012345\n",
        "    installation_id: 89012345\n    client: lares-github\n",
        1,
    )

    with pytest.raises(ValueError, match="client"):
        load_use_cases(_write(tmp_path, broken))


def test_the_one_server_rule_holds_among_the_bridge_servers_only(tmp_path: Path) -> None:
    """GitHub's `get_*` cannot reach the bridge's `get_write_request`: different servers."""
    declared = load_use_case_file(_write(tmp_path, VALID))

    assert "get_*" in declared.tool_servers["github"].tools
    assert "get_write_request" in declared.tool_servers["lares-control"].tools


def test_an_unknown_kind_of_tool_server_is_refused(tmp_path: Path) -> None:
    broken = VALID.replace("    kind: github\n", "    kind: gitlab\n", 1)

    with pytest.raises(ValueError, match="gitlab"):
        load_use_cases(_write(tmp_path, broken))


def test_a_file_without_the_ledger_hook_is_refused(tmp_path: Path) -> None:
    broken = VALID.replace("ledger_hook:", "unused:", 1)

    with pytest.raises(ValueError, match="ledger_hook"):
        load_use_cases(_write(tmp_path, broken))


ALERT_USE_CASE = """
  - name: investigate-alert
    sentence: Investigates a firing alert.
    trigger:
      kind: event
      source: alert
    skill: lares-investigate
    tools: [lares, cluster]
    output: [stored, alert]
    budget: {tool_calls: 40, minutes: 10, runs_per_day: 10}
    language: de
    memory: false
"""


def test_a_dormant_use_case_may_wait_for_an_event_source_still_to_be_built(
    tmp_path: Path,
) -> None:
    dormant = VALID + ALERT_USE_CASE + "    dormant: Waits for the alert path.\n"

    trigger = load_use_cases(_write(tmp_path, dormant))["investigate-alert"].trigger
    assert trigger.kind == "event"
    assert trigger.source == "alert"


def test_an_enabled_use_case_on_an_event_source_not_consumed_is_refused(tmp_path: Path) -> None:
    """Nothing reads the alert path yet; enabling it would declare runs that never start."""
    enabled = VALID + ALERT_USE_CASE.replace("[lares, cluster]", "[lares]") + "    enabled: true\n"

    with pytest.raises(ValueError, match="investigate-alert: .*episode events only"):
        load_use_cases(_write(tmp_path, enabled))


def test_an_episode_trigger_names_its_filter(tmp_path: Path) -> None:
    broken = VALID.replace("      filter:\n        appeared: 2\n        escalated: 2\n", "", 1)

    with pytest.raises(ValueError, match="filter"):
        load_use_cases(_write(tmp_path, broken))
