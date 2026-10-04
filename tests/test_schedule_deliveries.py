"""Deliveries after a cron run: a schedule use case's output carried where it declares.

The harness runs a schedule use case's job on its own and reports the turn
through the hook. The turn's end writes the row; once the receiver has
answered, the trigger carries the answer to every declared target — the same
deliveries an event run gets, refusals and AgentRunFailed included. The use
case here is `propose-faults` of `USE_CASES`, declared with the targets the
tests need; a chat turn, answered by the harness itself, is delivered nowhere.
"""

from __future__ import annotations

import asyncio
import email
import json
from email.message import EmailMessage
from email.policy import default as default_policy
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from lares_agent_trigger.config import Settings
from lares_agent_trigger.deliveries import build_deliveries
from lares_agent_trigger.ledger import Ledger
from lares_agent_trigger.metrics import Metrics
from lares_agent_trigger.use_cases import load_use_cases

from .conftest import DISCORD_CHANNEL, MAIL_TO, USE_CASES, Relay, Service
from .fakes import (
    CRON_SESSION,
    CRON_TASK,
    EXECUTION_ID,
    JOB_ID,
    WIKI_TOKEN,
    WIKI_URL,
    fake_alertmanager,
    fake_job,
    model_call,
    sample,
    sign,
    turn_ended,
    wiki_answer,
    wiki_listed,
)

pytestmark = pytest.mark.respx(assert_all_called=False)

HOOK = "/hooks/hermes"
PROPOSE = "    skill: lares-propose\n    tools: [lares]\n    output: [stored]"

PAGE = (
    "Wartungsplan auf den Stand vom Oktober gebracht.\n"
    "\n"
    "---\n"
    "path: haus/wartungsplan\n"
    "title: Wartungsplan\n"
    "---\n"
    "# Wartungsplan\n"
    "\n"
    "- KWL-Filter: fällig 2026-11\n"
)


def _declare(settings: Settings, tmp_path: Path, output: str) -> Settings:
    declared = tmp_path / "schedule-use-cases.yaml"
    declared.write_text(
        USE_CASES.replace(PROPOSE, PROPOSE.replace("[stored]", output)), encoding="utf-8"
    )
    return settings.model_copy(
        update={
            "use_cases_file": declared,
            "wikijs_url": WIKI_URL,
            "wikijs_token": WIKI_TOKEN,
            "wikijs_locale": "en",
        }
    )


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    return _declare(settings, tmp_path, "[stored, wiki_page, discord, mail]")


async def _post(service: Service, *bodies: bytes) -> None:
    for body in bodies:
        response = await service.client.post(HOOK, content=body, headers=sign(body))
        assert response.status_code == 200


def _cron_answer(text: str) -> bytes:
    return model_call(1, session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK, content=text)


def _cron_end(**fields: Any) -> bytes:
    return turn_ended(session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK, **fields)


async def _cron_run(service: Service, text: str) -> None:
    """One cron turn of the job, as the harness reports it, and its deliveries done."""
    await _post(service, _cron_answer(text), _cron_end())
    await service.turns.drain()


def _mail(relay: Relay) -> EmailMessage:
    (envelope,) = relay.messages
    assert isinstance(envelope.content, bytes)
    message = email.message_from_bytes(
        envelope.content.replace(b"\r\n", b"\n"), policy=default_policy
    )
    assert isinstance(message, EmailMessage)
    return message


async def test_a_cron_run_is_delivered_to_every_declared_target(
    service: Service,
    rows: Any,
    relay: Relay,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    wiki = respx_mock.post(f"{WIKI_URL}/graphql").mock(
        side_effect=wiki_answer(wiki_listed(), "create", "haus/wartungsplan", 31)
    )

    await _cron_run(service, PAGE)

    (row,) = rows()
    assert row["use_case"] == "propose-faults"
    assert row["trigger"] == "schedule"
    assert row["status"] == "completed"
    assert row["text"] == PAGE
    assert row["error"] is None
    # One ref per target, in declaration order; `stored` is the row itself.
    (message_call,) = discord.calls
    message_id = json.loads(message_call.response.content)["id"]
    mail = _mail(relay)
    assert row["output_ref"] == [
        "wiki:en/haus/wartungsplan",
        f"discord:{DISCORD_CHANNEL}/{message_id}",
        f"mail:{mail['Message-ID'].strip('<>')}",
    ]
    assert wiki.call_count == 2  # the lookup, then the create
    assert json.loads(message_call.request.content)["content"] == PAGE.strip()
    # A cron run has no episode: the mail is named by its use case and its first line.
    assert mail["To"] == MAIL_TO
    assert mail["Subject"] == ("[propose-faults] Wartungsplan auf den Stand vom Oktober gebracht.")
    body = mail.get_content()
    assert body.startswith(PAGE.strip())
    assert "gpt-6-sol (openai-codex)" in body
    assert "grafana" not in body
    for target in ("wiki_page", "discord", "mail"):
        assert (
            sample(
                service.metrics,
                "agent_trigger_deliveries_total",
                use_case="propose-faults",
                target=target,
                outcome="sent",
            )
            == 1.0
        )


async def test_a_target_that_refuses_fails_the_cron_run_and_raises_the_alert(
    service: Service,
    rows: Any,
    relay: Relay,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    alerted = fake_alertmanager(respx_mock)
    respx_mock.post(f"{WIKI_URL}/graphql").mock(
        side_effect=wiki_answer(
            wiki_listed(), "create", "haus/wartungsplan", 0, refused="You are not authorized."
        )
    )

    await _cron_run(service, PAGE)

    (row,) = rows()
    assert row["status"] == "failed"
    assert row["text"] == PAGE
    assert row["error"].startswith("wiki_page: ")
    assert "You are not authorized." in row["error"]
    # The other targets were still tried, and what they created is on the row.
    assert [ref.split(":")[0] for ref in row["output_ref"]] == ["discord", "mail"]
    (alert,) = json.loads(alerted.calls.last.request.content)
    assert alert["labels"]["alertname"] == "AgentRunFailed"
    assert alert["labels"]["use_case"] == "propose-faults"
    assert alert["annotations"]["summary"] == (
        f"propose-faults could not deliver cron run {JOB_ID}:{EXECUTION_ID} to wiki_page"
    )
    assert (
        sample(
            service.metrics,
            "agent_trigger_failures_total",
            use_case="propose-faults",
            **{"class": "delivery_failed"},
        )
        == 1.0
    )


async def test_a_redelivered_turn_end_is_delivered_once(
    service: Service,
    rows: Any,
    relay: Relay,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    respx_mock.post(f"{WIKI_URL}/graphql").mock(
        side_effect=wiki_answer(wiki_listed(), "create", "haus/wartungsplan", 31)
    )

    await _post(service, _cron_answer(PAGE), _cron_end(), _cron_end())
    await service.turns.drain()

    assert len(rows()) == 1
    assert discord.call_count == 1
    assert len(relay.messages) == 1


async def test_a_cron_run_that_failed_is_delivered_nowhere(
    service: Service,
    rows: Any,
    relay: Relay,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    wiki = respx_mock.post(f"{WIKI_URL}/graphql")

    await _post(
        service,
        _cron_answer(PAGE),
        _cron_end(completed=False, failed=True, exit_reason="error(model_not_found)"),
    )
    await service.turns.drain()

    (row,) = rows()
    assert row["status"] == "failed"
    assert row["error"] == "error(model_not_found)"
    assert not wiki.called
    assert not discord.called
    assert relay.messages == []


async def test_a_completed_cron_run_without_an_answer_fails_and_is_delivered_nowhere(
    service: Service,
    rows: Any,
    relay: Relay,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    alerted = fake_alertmanager(respx_mock)

    # The turn's calls were lost, a restart in between: its end alone arrives.
    await _post(service, _cron_end())
    await service.turns.drain()

    (row,) = rows()
    assert row["status"] == "failed"
    assert row["error"] == "the run ended without an answer to deliver"
    assert not discord.called
    assert relay.messages == []
    (alert,) = json.loads(alerted.calls.last.request.content)
    assert alert["annotations"]["summary"] == (
        f"propose-faults could not deliver cron run {JOB_ID}:{EXECUTION_ID}: no answer"
    )


async def test_a_chat_turn_is_answered_by_the_harness_and_delivered_nowhere(
    service: Service, rows: Any, relay: Relay, discord: Any
) -> None:
    await _post(service, model_call(1, content="Die Tür ist zu."), turn_ended())
    await service.turns.drain()

    (row,) = rows()
    assert row["use_case"] == "messenger"
    assert row["status"] == "completed"
    assert not discord.called
    assert relay.messages == []


def test_a_schedule_target_this_trigger_cannot_deliver_refuses_to_start(
    settings: Settings, tmp_path: Path
) -> None:
    undeliverable = _declare(settings, tmp_path, "[stored, github_pr]")

    with pytest.raises(ValueError, match="propose-faults declares output github_pr"):
        build_deliveries(undeliverable, load_use_cases(undeliverable.use_cases_file), Metrics())


def test_a_schedule_target_without_its_settings_refuses_to_start(settings: Settings) -> None:
    unconfigured = settings.model_copy(update={"wikijs_token": ""})

    with pytest.raises(ValueError, match="WIKIJS_TOKEN"):
        build_deliveries(unconfigured, load_use_cases(settings.use_cases_file), Metrics())


async def test_a_wiki_that_cannot_be_reached_fails_the_cron_run(
    service: Service,
    rows: Any,
    relay: Relay,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    fake_alertmanager(respx_mock)
    respx_mock.post(f"{WIKI_URL}/graphql").mock(side_effect=httpx.ConnectError("refused"))

    await _cron_run(service, PAGE)

    (row,) = rows()
    assert row["status"] == "failed"
    assert "Wiki.js could not be reached" in row["error"]


async def test_the_row_stays_running_until_its_targets_answered(
    service: Service,
    rows: Any,
    relay: Relay,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    released = asyncio.Event()
    answer = wiki_answer(wiki_listed(), "create", "haus/wartungsplan", 31)

    async def slow_wiki(request: httpx.Request) -> httpx.Response:
        await released.wait()
        return answer(request)

    respx_mock.post(f"{WIKI_URL}/graphql").mock(side_effect=slow_wiki)

    # The hook is answered while the page is still on its way.
    try:
        await _post(service, _cron_answer(PAGE), _cron_end())
        (row,) = rows()
        assert row["status"] == "running"
        assert row["finished_at"] is None
        assert row["text"] == PAGE
    finally:
        released.set()
    await service.turns.drain()
    (row,) = rows()
    assert row["status"] == "completed"
    assert row["finished_at"] is not None
    assert (
        sample(
            service.metrics,
            "agent_trigger_recorded_runs_total",
            use_case="propose-faults",
            status="completed",
        )
        == 1.0
    )


async def test_an_empty_answer_is_no_answer(
    service: Service,
    rows: Any,
    relay: Relay,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    fake_alertmanager(respx_mock)

    await _cron_run(service, "")

    (row,) = rows()
    assert row["status"] == "failed"
    assert row["error"] == "the run ended without an answer to deliver"
    assert not discord.called
    assert relay.messages == []


async def test_a_delivery_that_breaks_is_logged_and_alerted(
    service: Service,
    rows: Any,
    relay: Relay,
    discord: Any,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    alerted = fake_alertmanager(respx_mock)
    respx_mock.post(f"{WIKI_URL}/graphql").mock(
        side_effect=wiki_answer(wiki_listed(), "create", "haus/wartungsplan", 31)
    )

    async def ledger_gone(*_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError("the ledger went away")

    monkeypatch.setattr(Ledger, "finish", ledger_gone)

    await _cron_run(service, PAGE)

    # The row stays open for the next pod to close; the failure is loud now.
    (row,) = rows()
    assert row["status"] == "running"
    (alert,) = json.loads(alerted.calls.last.request.content)
    assert alert["annotations"]["summary"] == (
        f"propose-faults could not close cron run {JOB_ID}:{EXECUTION_ID}"
    )
    assert "the ledger went away" in alert["annotations"]["description"]


async def test_a_cron_row_a_stopped_pod_left_running_is_closed_and_reported(
    service: Service, rows: Any, execute: Any, respx_mock: respx.MockRouter
) -> None:
    """No redelivery finds a cron run's row, so the next pod closes what the last one left."""
    alerts = fake_alertmanager(respx_mock)
    execute(
        "INSERT INTO agent_runs (use_case, trigger, subject_kind, subject_key, status, language)"
        " VALUES ('propose-faults', 'schedule', 'none', %s, 'running', 'en')",
        f"{JOB_ID}:{EXECUTION_ID}",
    )

    await service.runs.close_abandoned()

    (row,) = rows()
    assert row["status"] == "failed"
    assert "restarted" in row["error"]
    assert row["finished_at"] is not None
    (alert,) = json.loads(alerts.calls.last.request.content)
    assert alert["labels"]["use_case"] == "propose-faults"
    assert "trigger_restarted" in alert["annotations"]["summary"]
