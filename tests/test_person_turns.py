"""Turns of the persons' profiles: whose row a turn is, by its profile and its platform."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lares_agent_trigger import turn_runs
from lares_agent_trigger.config import Settings
from lares_agent_trigger.consumer import EpisodeConsumer
from lares_agent_trigger.metrics import Metrics
from lares_agent_trigger.spans import TraceIds

from .conftest import API_KEY
from .fakes import (
    ANSWER,
    CHAT_SESSION,
    CHAT_TURN,
    CRON_SESSION,
    CRON_TASK,
    EXPLANATION,
    fake_hermes,
    fake_job,
    model_call,
    sample,
    sign,
    turn_ended,
)

Rows = Callable[[], list[dict[str, Any]]]
Receiver = tuple[httpx.AsyncClient, Metrics]
Consumer = tuple[EpisodeConsumer, Metrics]
Publish = Callable[..., Awaitable[None]]

HOOK = "/hooks/hermes"

# A cron test never reaches the Runs API routes, an event test never the Jobs API.
pytestmark = pytest.mark.respx(assert_all_called=False)

# The owner on the machines' subscription, one more person on an account of
# their own, and one who moved out.
PERSONS = """
default_profile:
  model: owner-subscription

model_sources:
  - name: owner-subscription
    provider: openai-codex
    model: gpt-6-sol
    account: shared-with-default
    bound: subscription
    fallback:
      - provider: openai-api
        model: gpt-6-luna
        bound: capped
        price: {input: 0.10, cached_input: 0.01, output: 0.50}
  - name: free-account
    provider: openai-codex
    model: gpt-6-luna
    account: per-person
    bound: subscription
    fallback:
      - provider: openai-api
        model: gpt-6-luna
        bound: capped
        price: {input: 0.20, cached_input: 0.02, output: 1.00}

persons:
  - name: kim
    discord: admin
    tools: [lares]
    model: owner-subscription
    keep_answers: true
    enabled: true
  - name: lou
    discord: true
    tools: [lares]
    model: free-account
    enabled: true
  - name: max
    discord: true
    tools: [lares]
    model: free-account
    dormant: Moved out.
"""


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The declaration with persons in it."""
    settings.persons_file.write_text(PERSONS, encoding="utf-8")
    return settings


async def _deliver(client: httpx.AsyncClient, *bodies: bytes) -> None:
    for body in bodies:
        response = await client.post(HOOK, content=body, headers=sign(body))
        assert response.status_code == 200, response.text


async def test_a_persons_discord_turn_is_their_chat_row(receiver: Receiver, rows: Rows) -> None:
    client, metrics = receiver

    await _deliver(
        client,
        model_call(1, profile="kim", tool_calls=1),
        model_call(2, profile="kim", content=ANSWER),
        turn_ended(profile="kim"),
    )

    (row,) = rows()
    assert row["use_case"] == "messenger"
    assert row["trigger"] == "message"
    assert row["subject_key"] == f"{CHAT_SESSION}:{CHAT_TURN}"
    assert row["enduser_id"] == "kim"
    assert row["surface"] == "discord"
    # kim keeps answers: the text and what the tools were asked.
    assert row["text"] == ANSWER
    assert row["tool_trace"]["calls"][0]["tools"] == [
        {"name": "list_episodes", "arguments": '{"state":"open","days":7}'}
    ]
    assert (
        sample(
            metrics,
            "agent_trigger_recorded_runs_total",
            use_case="messenger",
            status="completed",
            enduser_id="kim",
        )
        == 1.0
    )


async def test_a_persons_api_server_turn_is_their_web_chat_row(
    receiver: Receiver, rows: Rows
) -> None:
    """The web chat reaches the person's profile through the API server, like an event run."""
    client, metrics = receiver
    session = "web_7d1e4a"

    await _deliver(
        client,
        model_call(1, profile="lou", session_id=session, platform="api_server", content=ANSWER),
        turn_ended(profile="lou", session_id=session, platform="api_server"),
    )

    (row,) = rows()
    assert row["use_case"] == "messenger"
    assert row["subject_key"] == f"{session}:{CHAT_TURN}"
    assert row["enduser_id"] == "lou"
    assert row["surface"] == "web"
    assert sample(metrics, "agent_trigger_hook_events_total", outcome="recorded") == 1.0
    assert sample(metrics, "agent_trigger_hook_events_total", outcome="handed_over") == 0.0


async def test_an_event_run_names_nobody(
    consumer: Consumer,
    receiver: Receiver,
    publish: Publish,
    rows: Rows,
    respx_mock: respx.MockRouter,
) -> None:
    """The default profile's API-server turn is the trigger's own run, whoever is declared."""
    fake_hermes(respx_mock)
    episode_consumer, _ = consumer
    client, _ = receiver

    await _deliver(
        client,
        model_call(1, session_id="sess_1", platform="api_server", content=EXPLANATION),
        turn_ended(session_id="sess_1", platform="api_server"),
    )
    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["use_case"] == "explain-episode"
    assert row["status"] == "completed"
    assert row["enduser_id"] is None
    assert row["surface"] is None


async def test_a_cron_run_names_nobody(
    receiver: Receiver, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    client, _ = receiver

    await _deliver(
        client,
        model_call(1, session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK, content="Ok."),
        turn_ended(session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK),
    )

    (row,) = rows()
    assert row["use_case"] == "propose-faults"
    assert row["enduser_id"] is None
    assert row["surface"] is None


async def test_a_run_a_person_asked_for_names_nobody(
    api: Receiver, rows: Rows, episode: int, respx_mock: respx.MockRouter
) -> None:
    """start_run comes from a person's chat; the run it starts is the machines'."""
    fake_hermes(respx_mock)
    client, _ = api

    response = await client.post(
        "/api/runs",
        json={"use_case": "explain-episode", "subject": str(episode)},
        headers={"Authorization": f"Bearer {API_KEY}"},
    )

    assert response.status_code == 202
    for _ in range(500):
        (row,) = rows()
        if row["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert row["status"] == "completed"
    assert row["enduser_id"] is None
    assert row["surface"] is None


async def test_a_person_who_keeps_no_answers_gets_a_row_of_numbers_and_tool_names(
    receiver: Receiver, rows: Rows
) -> None:
    client, _ = receiver

    await _deliver(
        client,
        model_call(1, profile="lou", tool_calls=1, model="gpt-6-luna"),
        model_call(
            2,
            profile="lou",
            content=ANSWER,
            model="gpt-6-luna",
            provider="openai-api",
            input_tokens=10000,
            cache_read_tokens=4000,
            output_tokens=500,
        ),
        turn_ended(profile="lou", model="gpt-6-luna"),
    )

    (row,) = rows()
    assert row["enduser_id"] == "lou"
    assert row["text"] is None
    assert row["tldr"] is None
    # The last call names who answered: lou's fallback.
    assert row["model_source"] == "openai-api"
    assert row["model"] == "gpt-6-luna"
    assert row["tokens_in"] == 42000 + 14000
    assert row["tokens_out"] == 300 + 500
    # lou's fallback at its own price: 10k fresh at 0.20, 4k cached at 0.02, 500 out at 1.00.
    assert row["cost"] == Decimal("0.002580")
    assert row["tool_trace"]["tool_count"] == 1
    assert row["tool_trace"]["calls"][0]["tools"] == [{"name": "list_episodes", "arguments": ""}]


def _odd_turns(metrics: Metrics, kind: str, enduser_id: str) -> float:
    return sample(metrics, "agent_trigger_odd_turns_total", kind=kind, enduser_id=enduser_id)


@pytest.mark.parametrize(
    ("profile", "platform", "surface"),
    [("rex", "discord", "discord"), ("max", "discord", "discord"), ("rex", "api_server", "web")],
    ids=["undeclared", "dormant", "undeclared-web"],
)
async def test_a_turn_of_a_profile_no_person_runs_on_is_numbers_under_its_name(
    receiver: Receiver, rows: Rows, profile: str, platform: str, surface: str
) -> None:
    """A profile left on the volume, or someone who reached it without a declaration."""
    client, metrics = receiver

    await _deliver(
        client,
        model_call(1, profile=profile, platform=platform, tool_calls=1),
        model_call(2, profile=profile, platform=platform, content=ANSWER),
        turn_ended(profile=profile, platform=platform),
    )

    (row,) = rows()
    assert row["use_case"] == "messenger"
    assert row["enduser_id"] == profile
    assert row["surface"] == surface
    assert row["text"] is None
    assert row["tool_trace"]["calls"][0]["tools"] == [{"name": "list_episodes", "arguments": ""}]
    assert row["tokens_in"] == 2 * 42000
    assert _odd_turns(metrics, "undeclared_profile", profile) == 1.0


async def test_a_discord_turn_on_the_default_profile_is_numbers_of_nobody(
    receiver: Receiver, rows: Rows
) -> None:
    """With persons on Discord the bot routes every message to a profile; this one slipped by."""
    client, metrics = receiver

    await _deliver(client, model_call(1, content=ANSWER), turn_ended())

    (row,) = rows()
    assert row["use_case"] == "messenger"
    assert row["enduser_id"] is None
    assert row["surface"] == "discord"
    assert row["text"] is None
    assert row["tokens_in"] == 42000
    assert _odd_turns(metrics, "discord_on_default", "") == 1.0


@pytest.mark.parametrize(
    ("profile", "kind"),
    [("kim", "cron_on_person"), ("rex", "undeclared_profile")],
    ids=["person", "undeclared"],
)
async def test_a_cron_turn_on_a_profile_of_no_machine_gets_no_row(
    receiver: Receiver, rows: Rows, profile: str, kind: str
) -> None:
    """Only the default profile runs the declared jobs; no use case owns this one."""
    client, metrics = receiver
    calls = model_call(
        1, profile=profile, session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK
    )
    end = turn_ended(profile=profile, session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK)

    await _deliver(client, calls, end)

    assert rows() == []
    assert _odd_turns(metrics, kind, profile) == 1.0
    assert sample(metrics, "agent_trigger_hook_events_total", outcome="ignored") == 1.0


def _fallback_call(profile: str, turn: str) -> bytes:
    """A call of 10k fresh tokens and 300 out, on the fallback every source declares."""
    return model_call(
        1,
        profile=profile,
        turn=turn,
        content=ANSWER,
        input_tokens=10000,
        cache_read_tokens=0,
        model="gpt-6-luna",
        provider="openai-api",
    )


async def test_what_a_turn_cost_is_counted_by_person_and_source(
    receiver: Receiver, rows: Rows
) -> None:
    client, metrics = receiver

    await _deliver(
        client,
        _fallback_call("lou", CHAT_TURN),
        turn_ended(profile="lou", model="gpt-6-luna"),
        _fallback_call("kim", "7b3e"),
        turn_ended(profile="kim", turn="7b3e", model="gpt-6-luna"),
    )

    def cost(enduser_id: str, source: str) -> float:
        return sample(
            metrics,
            "agent_trigger_cost_usd_total",
            use_case="messenger",
            source=source,
            step="fallback",
            enduser_id=enduser_id,
        )

    # Each at the price of their own source's fallback.
    assert cost("lou", "free-account") == pytest.approx(0.0023)
    assert cost("kim", "owner-subscription") == pytest.approx(0.00115)


async def test_a_persons_turn_is_a_trace_that_names_them_and_where_they_wrote(
    receiver: Receiver, rows: Rows, spans: InMemorySpanExporter, respx_mock: respx.MockRouter
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    client, _ = receiver

    await _deliver(
        client,
        model_call(1, profile="lou", session_id="web_7d1e4a", platform="api_server", content="Ja."),
        turn_ended(profile="lou", session_id="web_7d1e4a", platform="api_server"),
        model_call(1, session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK, content="Ok."),
        turn_ended(session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK),
    )

    chat, cron = sorted(
        (span for span in spans.get_finished_spans() if span.parent is None),
        key=lambda span: str(span.name),
    )
    assert chat.name == "invoke_agent messenger"
    assert chat.attributes is not None
    assert chat.attributes["enduser.id"] == "lou"
    assert chat.attributes["lares.surface"] == "web"
    # The machines' run names nobody.
    assert cron.name == "invoke_agent propose-faults"
    assert cron.attributes is not None
    assert "enduser.id" not in cron.attributes
    assert "lares.surface" not in cron.attributes


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """The service's log lines, with what the service's own handler adds."""
    caplog.set_level(logging.INFO, logger="lares_agent_trigger")
    caplog.handler.addFilter(TraceIds())
    return caplog


async def test_a_persons_log_lines_name_them(
    receiver: Receiver, rows: Rows, logs: pytest.LogCaptureFixture
) -> None:
    client, _ = receiver

    await _deliver(client, model_call(1, profile="kim", content=ANSWER), turn_ended(profile="kim"))

    (row,) = rows()
    (line,) = [record for record in logs.records if f"run {row['id']}" in record.getMessage()]
    assert getattr(line, "enduser.id", None) == "kim"
    # Outside the turn a line names nobody.
    logging.getLogger("lares_agent_trigger.test").info("between turns")
    assert not hasattr(logs.records[-1], "enduser.id")


async def test_the_warning_that_a_turn_has_no_cost_names_its_person(
    receiver: Receiver, rows: Rows, logs: pytest.LogCaptureFixture
) -> None:
    """A profile no person runs on has no source to price by."""
    client, _ = receiver

    await _deliver(client, model_call(1, profile="rex", content=ANSWER), turn_ended(profile="rex"))

    assert len(rows()) == 1
    (line,) = [record for record in logs.records if "has no cost" in record.getMessage()]
    assert getattr(line, "enduser.id", None) == "rex"


async def test_a_persons_turn_that_never_ended_is_a_trace_that_names_them(
    receiver: Receiver, rows: Rows, spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = receiver
    await _deliver(
        client,
        model_call(1, profile="lou", session_id="web_7d1e4a", platform="api_server", turn="0rphan"),
    )

    # Every tally is past its hour from here on; the next call drops it.
    monkeypatch.setattr(turn_runs, "_TALLY_LIFETIME_SECONDS", -1.0)
    await _deliver(client, model_call(1, profile="kim", content=ANSWER))

    assert rows() == []
    (root,) = [span for span in spans.get_finished_spans() if span.parent is None]
    assert root.name == "invoke_agent messenger"
    assert root.attributes is not None
    assert root.attributes["error.type"] == "turn_without_end"
    assert root.attributes["enduser.id"] == "lou"
    assert root.attributes["lares.surface"] == "web"
