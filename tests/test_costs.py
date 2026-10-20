"""Every model call priced by the declared source step that served it, on every path.

The declaration in `conftest.PERSONS`: the default profile runs on the owner's
subscription, which costs nothing per call, and falls back to a capped API
project at 0.10 USD per million fresh input tokens, 0.01 per million cache
reads and 0.50 per million output tokens.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from lares_agent_trigger.consumer import EpisodeConsumer
from lares_agent_trigger.metrics import Metrics

from .fakes import (
    ANSWER,
    COMPLETED,
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

pytestmark = pytest.mark.respx(assert_all_called=False)

FALLBACK = {"provider": "openai-api", "model": "gpt-6-luna"}
# What `_call` costs on the fallback: 10k fresh input, 30k cache reads and 500
# out are 0.001 + 0.0003 + 0.00025 USD. At the fresh rate the cache reads
# alone would cost 0.003.
ONE_CALL = Decimal("0.00155")


def _call(number: int, *, fallback: bool = False, **fields: Any) -> bytes:
    """One call reading mostly from the cache, on the subscription or on its fallback."""
    if fallback:
        fields = {**FALLBACK, **fields}
    return model_call(
        number, input_tokens=10000, cache_read_tokens=30000, output_tokens=500, **fields
    )


async def _deliver(client: httpx.AsyncClient, *bodies: bytes) -> None:
    for body in bodies:
        response = await client.post(HOOK, content=body, headers=sign(body))
        assert response.status_code == 200, response.text


def _by_step(metrics: Metrics, name: str, use_case: str, step: str) -> float:
    return sample(metrics, name, use_case=use_case, source="owner-subscription", step=step)


def _answers(metrics: Metrics, use_case: str, step: str) -> float:
    return _by_step(metrics, "agent_trigger_answers_total", use_case, step)


def _spent(metrics: Metrics, use_case: str, step: str) -> float:
    return _by_step(metrics, "agent_trigger_cost_usd_total", use_case, step)


def _unpriced(metrics: Metrics, use_case: str, provider: str, model: str, reason: str) -> float:
    return sample(
        metrics,
        "agent_trigger_unpriced_calls_total",
        use_case=use_case,
        provider=provider,
        model=model,
        reason=reason,
    )


async def test_a_turn_on_the_subscription_costs_nothing(receiver: Receiver, rows: Rows) -> None:
    client, metrics = receiver

    await _deliver(
        client,
        _call(1, tool_calls=1),
        _call(2, content=ANSWER),
        turn_ended(),
    )

    (row,) = rows()
    assert row["model_source"] == "openai-codex"
    assert row["cost"] == Decimal("0")
    assert _answers(metrics, "messenger", "primary") == 1.0
    assert _spent(metrics, "messenger", "primary") == 0.0


async def test_a_turn_on_the_fallback_is_priced_with_cache_reads_apart(
    receiver: Receiver, rows: Rows
) -> None:
    client, metrics = receiver

    await _deliver(
        client,
        _call(1, tool_calls=1, fallback=True),
        _call(2, content=ANSWER, fallback=True),
        turn_ended(),
    )

    (row,) = rows()
    assert row["model_source"] == "openai-api"
    assert row["cost"] == 2 * ONE_CALL
    assert _answers(metrics, "messenger", "fallback") == 1.0
    assert _answers(metrics, "messenger", "primary") == 0.0
    assert _spent(metrics, "messenger", "fallback") == pytest.approx(float(2 * ONE_CALL))


async def test_a_turn_that_fell_back_midway_pays_for_its_fallback_calls_only(
    receiver: Receiver, rows: Rows
) -> None:
    client, metrics = receiver

    await _deliver(
        client,
        _call(1, tool_calls=1),
        _call(2, content=ANSWER, fallback=True),
        turn_ended(),
    )

    (row,) = rows()
    assert row["cost"] == ONE_CALL
    # The answer is the last call's, and the fallback gave it.
    assert _answers(metrics, "messenger", "fallback") == 1.0
    assert _spent(metrics, "messenger", "primary") == 0.0
    assert _spent(metrics, "messenger", "fallback") == pytest.approx(float(ONE_CALL))


async def test_a_cron_run_is_priced_the_same_way(
    receiver: Receiver, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    client, metrics = receiver

    await _deliver(
        client,
        _call(
            1,
            session_id=CRON_SESSION,
            platform="cron",
            task_id=CRON_TASK,
            content="Drei Vorschläge.",
            fallback=True,
        ),
        turn_ended(session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK),
    )

    (row,) = rows()
    assert row["use_case"] == "propose-faults"
    assert row["cost"] == ONE_CALL
    assert _answers(metrics, "propose-faults", "fallback") == 1.0


async def test_an_event_run_is_priced_from_its_calls_not_the_session_estimate(
    consumer: Consumer,
    receiver: Receiver,
    publish: Publish,
    rows: Rows,
    respx_mock: respx.MockRouter,
) -> None:
    """The session record of `run_1` still reports 0.021 USD; nothing reads it."""
    on_fallback = {**COMPLETED, "runtime": {**FALLBACK, "route_source": "fallback"}}
    fake_hermes(respx_mock, states=[on_fallback])
    episode_consumer, metrics = consumer
    client, _ = receiver
    api = {"session_id": "sess_1", "platform": "api_server", "task_id": "0f3c9d2e-run"}

    await _deliver(
        client,
        _call(1, tool_calls=4, **api, fallback=True),
        _call(2, content=EXPLANATION, **api, fallback=True),
        turn_ended(session_id="sess_1", platform="api_server", task_id="0f3c9d2e-run"),
    )
    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["use_case"] == "explain-episode"
    assert row["model_source"] == "openai-api"
    assert row["cost"] == 2 * ONE_CALL
    assert _answers(metrics, "explain-episode", "fallback") == 1.0
    assert _spent(metrics, "explain-episode", "fallback") == pytest.approx(float(2 * ONE_CALL))


async def test_an_event_run_whose_calls_never_came_has_no_cost(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    fake_hermes(respx_mock)
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed"
    assert row["cost"] is None
    # It answered, so it called the model: its cost is unknown, and that is counted.
    assert _unpriced(metrics, "explain-episode", "openai-codex", "gpt-6-sol", "no_calls") == 1.0
    assert _answers(metrics, "explain-episode", "primary") == 1.0


async def test_a_turn_whose_calls_a_restart_lost_is_counted_without_a_cost(
    receiver: Receiver, rows: Rows
) -> None:
    client, metrics = receiver

    await _deliver(client, turn_ended())

    (row,) = rows()
    assert row["cost"] is None
    assert _unpriced(metrics, "messenger", "", "gpt-6-sol", "no_calls") == 1.0


async def test_a_retried_attempt_still_counts_what_it_spent(
    consumer: Consumer,
    receiver: Receiver,
    publish: Publish,
    rows: Rows,
    respx_mock: respx.MockRouter,
) -> None:
    """The row keeps the last attempt; the first one's calls on the fallback were paid for."""
    rate_limited = {
        "status": "failed",
        "error": "Error code: 429 - rate limit exceeded, retry later",
        "session_id": "sess_1",
    }
    fake_hermes(respx_mock, runs=[[rate_limited], [COMPLETED]])
    episode_consumer, metrics = consumer
    client, _ = receiver
    api = {"session_id": "sess_1", "platform": "api_server", "task_id": "0f3c9d2e-run"}

    await _deliver(
        client,
        _call(1, tool_calls=2, **api, fallback=True),
        turn_ended(session_id="sess_1", platform="api_server", task_id="0f3c9d2e-run"),
    )
    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed"
    assert row["attempt"] == 2
    assert _spent(metrics, "explain-episode", "fallback") == pytest.approx(float(ONE_CALL))
    assert _answers(metrics, "explain-episode", "primary") == 1.0


async def test_a_call_no_declared_step_covers_leaves_the_cost_unknown_and_is_counted(
    receiver: Receiver, rows: Rows
) -> None:
    client, metrics = receiver

    await _deliver(
        client,
        _call(1, tool_calls=1, fallback=True),
        model_call(2, content=ANSWER, provider="openai-api", model="gpt-6-sol"),
        turn_ended(),
    )

    (row,) = rows()
    assert row["cost"] is None
    assert _unpriced(metrics, "messenger", "openai-api", "gpt-6-sol", "undeclared") == 1.0
    # What could be priced is still spent.
    assert _spent(metrics, "messenger", "fallback") == pytest.approx(float(ONE_CALL))
    assert _answers(metrics, "messenger", "undeclared") == 1.0


async def test_a_metered_call_without_usage_is_unpriced(receiver: Receiver, rows: Rows) -> None:
    client, metrics = receiver

    await _deliver(
        client,
        _call(1, content=ANSWER, usage=False, fallback=True),
        turn_ended(),
    )

    (row,) = rows()
    assert row["cost"] is None
    assert _unpriced(metrics, "messenger", "openai-api", "gpt-6-luna", "no_usage") == 1.0


async def test_a_subscription_call_without_usage_still_costs_nothing(
    receiver: Receiver, rows: Rows
) -> None:
    client, _ = receiver

    await _deliver(client, model_call(1, content=ANSWER, usage=False), turn_ended())

    (row,) = rows()
    assert row["cost"] == Decimal("0")


async def test_a_turn_from_a_profile_without_a_source_is_unpriced(
    receiver: Receiver, rows: Rows
) -> None:
    """Only the default profile is declared; another one's calls have no source to price by."""
    client, metrics = receiver
    call = _call(1, content=ANSWER).replace(b'"profile": "default"', b'"profile": "kim"')
    end = turn_ended().replace(b'"profile": "default"', b'"profile": "kim"')

    await _deliver(client, call, end)

    (row,) = rows()
    assert row["cost"] is None
    assert _unpriced(metrics, "messenger", "openai-codex", "gpt-6-sol", "undeclared") == 1.0
