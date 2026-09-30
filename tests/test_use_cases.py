"""Loader tests: every field of a use case, dormant entries, and a refused file."""

from __future__ import annotations

from pathlib import Path

import pytest

from lares_agent_trigger.use_cases import ScheduleTrigger, load_use_cases

VALID = """
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
    tools: [lares, lares-control]
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
