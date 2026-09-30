"""The event path end to end: an episode event in, a closed ledger row out."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx

from lares_agent_trigger.consumer import EpisodeConsumer
from lares_agent_trigger.metrics import Metrics

from .conftest import HERMES_URL
from .fakes import COMPLETED, EXPLANATION, SESSION, fake_hermes, sample, start_response

Publish = Callable[..., Awaitable[None]]
Rows = Callable[[], list[dict[str, Any]]]
Consumer = tuple[EpisodeConsumer, Metrics]

# Several tests register a poll route the run never reaches (a filtered event,
# a refused start); an unused route is not a failure here.
pytestmark = pytest.mark.respx(assert_all_called=False)


async def test_an_episode_appearing_at_severity_two_is_explained(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    started = fake_hermes(respx_mock)
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    assert await episode_consumer.run_once() == 1

    (row,) = rows()
    assert row["use_case"] == "explain-episode"
    assert row["trigger"] == "event"
    assert row["subject_kind"] == "episode"
    assert row["subject_key"] == "15510:appeared"
    assert row["status"] == "completed"
    assert row["harness_run_id"] == "run_1"
    assert row["attempt"] == 1
    assert row["language"] == "de"
    assert row["tldr"] == "Die Waschmaschine hängt seit 14:20 im Spülgang."
    assert row["text"] == EXPLANATION
    # Who actually served the run — never the gateway's own name `hermes-agent`.
    # The values come from the response, never from this package: no model or
    # provider name appears anywhere in `src/`.
    assert row["model_source"] == "openai-codex"
    assert row["model"] == "gpt-6-sol"
    assert row["tokens_in"] == 4200
    assert row["tokens_out"] == 310
    # Cost and the tool count come from the session record the run names.
    assert float(row["cost"]) == pytest.approx(0.021)
    assert row["tool_trace"] == {"tool_count": 7}
    # The record's own span, not the time we happened to spend polling: a run
    # that finishes inside the POST would leave that at nearly zero.
    assert row["duration"].total_seconds() == pytest.approx(11.14, abs=0.01)
    assert row["finished_at"] is not None

    assert (
        sample(metrics, "agent_trigger_runs_total", use_case="explain-episode", status="completed")
        == 1.0
    )

    # What the harness was actually asked: the pointer as input, the skill named
    # in the instructions, and the ledger key as the idempotency key.
    request = started.calls.last.request
    assert request.headers["Idempotency-Key"] == "explain-episode/episode/15510:appeared"
    body = json.loads(request.content)
    # The pointer travels as the run's user message, not as a JSON object.
    assert isinstance(body["input"], str)
    pointer = json.loads(body["input"])
    assert pointer["episode_id"] == 15510
    assert pointer["severity"] == 2
    assert "lares-explain" in body["instructions"]
    assert "German" in body["instructions"]


async def test_an_episode_appearing_at_severity_one_is_left_alone(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    started = fake_hermes(respx_mock)
    episode_consumer, metrics = consumer

    await publish("appeared", 1)
    assert await episode_consumer.run_once() == 1

    assert rows() == []
    assert not started.called
    assert (
        sample(metrics, "agent_trigger_events_total", kind="appeared", outcome="unmatched") == 1.0
    )


async def test_an_escalation_to_severity_three_is_explained(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    fake_hermes(respx_mock)
    episode_consumer, _ = consumer

    await publish("escalated", 3)
    assert await episode_consumer.run_once() == 1

    (row,) = rows()
    assert row["subject_key"] == "15510:escalated"
    assert row["status"] == "completed"


async def test_an_escalation_to_severity_two_is_explained(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    """The episode `escalated: 2` exists for: it opened at 1 and only now rises.

    Its `appeared` was filtered out, and the engine spends the escalation
    budget on this one rise — at a threshold of 3 it would never be explained.
    """
    fake_hermes(respx_mock)
    episode_consumer, _ = consumer

    await publish("escalated", 2)
    assert await episode_consumer.run_once() == 1

    (row,) = rows()
    assert row["subject_key"] == "15510:escalated"
    assert row["status"] == "completed"


async def test_an_episode_that_ended_never_runs(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    started = fake_hermes(respx_mock)
    episode_consumer, _ = consumer

    await publish("ended", 3)
    assert await episode_consumer.run_once() == 1

    assert rows() == []
    assert not started.called


async def test_the_same_event_twice_yields_one_row(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    started = fake_hermes(respx_mock)
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    await publish("appeared", 2)
    assert await episode_consumer.run_once() == 1
    assert await episode_consumer.run_once() == 1

    assert len(rows()) == 1
    assert started.call_count == 1
    assert (
        sample(metrics, "agent_trigger_duplicate_events_total", use_case="explain-episode") == 1.0
    )


async def test_the_eleventh_run_of_a_day_is_a_capped_row(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(f"{HERMES_URL}/v1/runs").mock(side_effect=start_response)
    respx_mock.get(f"{HERMES_URL}/v1/runs/run_1").mock(
        return_value=httpx.Response(200, json=COMPLETED)
    )
    respx_mock.get(f"{HERMES_URL}/api/sessions/sess_1").mock(
        return_value=httpx.Response(200, json=SESSION)
    )
    episode_consumer, metrics = consumer

    for episode_id in range(1, 12):  # the budget is ten runs a day
        await publish("appeared", 2, episode_id=episode_id)
        assert await episode_consumer.run_once() == 1

    by_status = [row["status"] for row in rows()]
    assert by_status == ["completed"] * 10 + ["capped"]
    capped = rows()[-1]
    assert capped["harness_run_id"] is None
    assert capped["finished_at"] is not None
    assert sample(metrics, "agent_trigger_capped_total", use_case="explain-episode") == 1.0


async def test_a_run_is_polled_until_it_is_terminal(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    fake_hermes(respx_mock, states=[{"status": "running"}, {"status": "running"}, COMPLETED])
    episode_consumer, _ = consumer

    await publish("appeared", 3)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed"
    assert row["text"] == EXPLANATION


async def test_a_message_that_is_not_an_episode_event_is_dropped(
    consumer: Consumer, publish_raw: Callable[[bytes], Awaitable[None]], rows: Rows
) -> None:
    episode_consumer, metrics = consumer

    await publish_raw(b"{}")

    assert await episode_consumer.run_once() == 1
    assert rows() == []
    assert sample(metrics, "agent_trigger_events_total", kind="unknown", outcome="invalid") == 1.0
