"""The event path end to end: an episode event in, a closed ledger row out."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx
from prometheus_client import CollectorRegistry

from lares_agent_trigger.consumer import EpisodeConsumer
from lares_agent_trigger.metrics import Metrics

from .conftest import HERMES_URL

Publish = Callable[..., Awaitable[None]]
Rows = Callable[[], list[dict[str, Any]]]
Consumer = tuple[EpisodeConsumer, Metrics]

# Several tests register a poll route the run never reaches (a filtered event,
# a refused start); an unused route is not a failure here.
pytestmark = pytest.mark.respx(assert_all_called=False)

EXPLANATION = (
    "Die Waschmaschine hängt seit 14:20 im Spülgang.\n"
    "Leistung 49 mA über 45 Minuten, Schwelle 30 mA."
)

# A run record as the live gateway returned one. `run_id`, never `id`; the
# top-level `model` is the gateway's own name and `runtime` holds the model
# that actually served the run; `created_at`/`updated_at` are unix seconds.
COMPLETED = {
    "object": "hermes.run",
    "run_id": "run_1",
    "status": "completed",
    "completed": True,
    "session_id": "sess_1",
    "model": "hermes-agent",
    "runtime": {"model": "gpt-6-sol", "provider": "openai-codex", "route_source": "global"},
    "output": EXPLANATION,
    "created_at": 1790714600.671442,
    "updated_at": 1790714611.808311,
    "usage": {
        "input_tokens": 4200,
        "output_tokens": 310,
        "total_tokens": 4510,
        "cache_read_tokens": 4864,
        "cache_write_tokens": 0,
    },
}

# The session record, where cost and the tool count live — wrapped, as the
# gateway wraps it.
SESSION = {
    "object": "hermes.session",
    "session": {
        "id": "sess_1",
        "model": "gpt-6-sol",
        "message_count": 4,
        "tool_call_count": 7,
        "input_tokens": 4200,
        "output_tokens": 310,
        "reasoning_tokens": 641,
        "estimated_cost_usd": 0.0247,
        "actual_cost_usd": 0.021,
        "api_call_count": 2,
    },
}


def _start_response(request: httpx.Request) -> httpx.Response:
    """The gateway's own rule on `input`, so a fake can never be laxer than it is.

    `api_server_runs.py` reads a string as the user message and takes `content`
    off the last entry of a list; anything else leaves the message empty and is
    refused. Sending the pointer as a JSON object passed every test here and
    failed on the first live run, which is why the rule lives in the fake now.
    """
    raw = json.loads(request.content)["input"]
    if isinstance(raw, str):
        message = raw
    elif isinstance(raw, list):
        message = raw[-1].get("content", "")
    else:
        message = ""
    if not message:
        return httpx.Response(400, json={"error": {"message": "No user message found in input"}})
    return httpx.Response(200, json={"object": "hermes.run", "run_id": "run_1", "status": "queued"})


def _fake_hermes(
    respx_mock: respx.MockRouter, *, states: list[dict[str, Any]] | None = None
) -> Any:
    """A harness that accepts a run and reports the given states in turn."""
    started = respx_mock.post(f"{HERMES_URL}/v1/runs").mock(side_effect=_start_response)
    respx_mock.get(f"{HERMES_URL}/v1/runs/run_1").mock(
        side_effect=[httpx.Response(200, json=state) for state in (states or [COMPLETED])]
    )
    respx_mock.get(f"{HERMES_URL}/api/sessions/sess_1").mock(
        return_value=httpx.Response(200, json=SESSION)
    )
    return started


def _value(metrics: Metrics, name: str, **labels: str) -> float:
    registry: CollectorRegistry = metrics.registry
    return registry.get_sample_value(name, labels) or 0.0


async def test_an_episode_appearing_at_severity_two_is_explained(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    started = _fake_hermes(respx_mock)
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
        _value(metrics, "agent_trigger_runs_total", use_case="explain-episode", status="completed")
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
    started = _fake_hermes(respx_mock)
    episode_consumer, metrics = consumer

    await publish("appeared", 1)
    assert await episode_consumer.run_once() == 1

    assert rows() == []
    assert not started.called
    assert (
        _value(metrics, "agent_trigger_events_total", kind="appeared", outcome="unmatched") == 1.0
    )


async def test_an_escalation_to_severity_three_is_explained(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    _fake_hermes(respx_mock)
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
    _fake_hermes(respx_mock)
    episode_consumer, _ = consumer

    await publish("escalated", 2)
    assert await episode_consumer.run_once() == 1

    (row,) = rows()
    assert row["subject_key"] == "15510:escalated"
    assert row["status"] == "completed"


async def test_an_episode_that_ended_never_runs(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    started = _fake_hermes(respx_mock)
    episode_consumer, _ = consumer

    await publish("ended", 3)
    assert await episode_consumer.run_once() == 1

    assert rows() == []
    assert not started.called


async def test_the_same_event_twice_yields_one_row(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    started = _fake_hermes(respx_mock)
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    await publish("appeared", 2)
    assert await episode_consumer.run_once() == 1
    assert await episode_consumer.run_once() == 1

    assert len(rows()) == 1
    assert started.call_count == 1
    assert (
        _value(metrics, "agent_trigger_duplicate_events_total", use_case="explain-episode") == 1.0
    )


async def test_the_eleventh_run_of_a_day_is_a_capped_row(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(f"{HERMES_URL}/v1/runs").mock(side_effect=_start_response)
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
    assert _value(metrics, "agent_trigger_capped_total", use_case="explain-episode") == 1.0


async def test_a_run_is_polled_until_it_is_terminal(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    _fake_hermes(respx_mock, states=[{"status": "running"}, {"status": "running"}, COMPLETED])
    episode_consumer, _ = consumer

    await publish("appeared", 3)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed"
    assert row["text"] == EXPLANATION


async def test_a_failed_run_closes_its_row_with_the_error(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    _fake_hermes(
        respx_mock,
        states=[{"status": "failed", "error": "model source refused the request"}],
    )
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    assert row["error"] == "model source refused the request"
    assert row["text"] is None
    assert (
        _value(metrics, "agent_trigger_runs_total", use_case="explain-episode", status="failed")
        == 1.0
    )


async def test_a_harness_that_refuses_the_start_closes_the_row(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(f"{HERMES_URL}/v1/runs").mock(return_value=httpx.Response(503, text="down"))
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    assert "503" in row["error"]
    assert row["harness_run_id"] is None


async def test_a_message_that_is_not_an_episode_event_is_dropped(
    consumer: Consumer, publish_raw: Callable[[bytes], Awaitable[None]], rows: Rows
) -> None:
    episode_consumer, metrics = consumer

    await publish_raw(b"{}")

    assert await episode_consumer.run_once() == 1
    assert rows() == []
    assert _value(metrics, "agent_trigger_events_total", kind="unknown", outcome="invalid") == 1.0
