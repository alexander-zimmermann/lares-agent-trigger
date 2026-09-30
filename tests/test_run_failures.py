"""The failure path: one retry for what fixes itself, then AgentRunFailed.

A transient failure — a rate limit, a timeout, a harness that is not there —
is started once more after the retry delay, under a new idempotency key. A
permanent one — auth, credits, config, a spent budget — is not. Whatever is
still failed after that is closed on its row and posted to Alertmanager.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx

from lares_agent_trigger.consumer import EpisodeConsumer
from lares_agent_trigger.metrics import Metrics

from .conftest import HERMES_URL, RETRY_DELAY_SECONDS
from .fakes import COMPLETED, fake_alertmanager, fake_hermes, sample

Publish = Callable[..., Awaitable[None]]
Rows = Callable[[], list[dict[str, Any]]]
Execute = Callable[..., None]
Consumer = tuple[EpisodeConsumer, Metrics]

pytestmark = pytest.mark.respx(assert_all_called=False)

KEY = "explain-episode/episode/15510:appeared"

# Error texts as the gateway writes them into a failed run record.
RATE_LIMITED = {"status": "failed", "error": "Error code: 429 - rate limit exceeded, retry later"}
AUTH_FAILED = {"status": "failed", "error": "⚠️ Provider authentication failed: 401 Unauthorized"}
NO_CREDITS = {
    "status": "failed",
    "error": "Error code: 403 - Your team has no usable credits. Top up your credits.",
}


def _stamped(route: Any) -> list[float]:
    """When each call reached the route, on the event loop's clock."""
    stamps: list[float] = []
    side_effect = route.side_effect

    def stamp(request: httpx.Request) -> Any:
        stamps.append(asyncio.get_running_loop().time())
        return side_effect(request)

    route.side_effect = stamp
    return stamps


async def test_a_transient_failure_is_retried_once_under_a_new_key(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    started = fake_hermes(respx_mock, runs=[[RATE_LIMITED], [COMPLETED]])
    alerted = fake_alertmanager(respx_mock)
    stamps = _stamped(started)
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    assert started.call_count == 2
    first, second = (call.request for call in started.calls)
    assert first.headers["Idempotency-Key"] == KEY
    # The gateway replays a run for a key it has seen, failed or not.
    assert second.headers["Idempotency-Key"] == f"{KEY}/2"
    # The same question both times.
    assert json.loads(first.content) == json.loads(second.content)
    assert stamps[1] - stamps[0] >= RETRY_DELAY_SECONDS

    (row,) = rows()
    assert row["status"] == "completed"
    assert row["attempt"] == 2
    assert row["harness_run_id"] == "run_2"
    assert row["error"] is None
    assert not alerted.called

    assert (
        sample(
            metrics,
            "agent_trigger_failures_total",
            use_case="explain-episode",
            **{"class": "rate_limited"},
        )
        == 1.0
    )
    assert (
        sample(metrics, "agent_trigger_runs_total", use_case="explain-episode", status="completed")
        == 1.0
    )
    assert (
        sample(metrics, "agent_trigger_runs_total", use_case="explain-episode", status="failed")
        == 0.0
    )


async def test_a_transient_failure_twice_posts_agent_run_failed(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    started = fake_hermes(respx_mock, runs=[[RATE_LIMITED], [RATE_LIMITED]])
    alerted = fake_alertmanager(respx_mock)
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    assert started.call_count == 2
    assert alerted.call_count == 1
    (alert,) = json.loads(alerted.calls.last.request.content)
    assert alert["labels"] == {
        "alertname": "AgentRunFailed",
        "use_case": "explain-episode",
        "severity": "warning",
    }
    assert "15510:appeared" in alert["annotations"]["summary"]
    assert alert["annotations"]["description"] == RATE_LIMITED["error"]
    # Without endsAt, Alertmanager resolves it after its resolve timeout.
    assert "endsAt" not in alert

    (row,) = rows()
    assert row["status"] == "failed"
    assert row["attempt"] == 2
    assert row["error"] == RATE_LIMITED["error"]
    assert row["finished_at"] is not None

    assert (
        sample(
            metrics,
            "agent_trigger_failures_total",
            use_case="explain-episode",
            **{"class": "rate_limited"},
        )
        == 2.0
    )
    assert (
        sample(metrics, "agent_trigger_runs_total", use_case="explain-episode", status="failed")
        == 1.0
    )
    assert sample(metrics, "agent_trigger_alerts_total", outcome="sent") == 1.0


@pytest.mark.parametrize(
    ("state", "failure_class"),
    [
        (AUTH_FAILED, "auth_failed"),
        # xAI sends credit exhaustion as a 403; it is still not an auth failure.
        (NO_CREDITS, "credits_exhausted"),
        (
            {"status": "failed", "error": "invalid model configuration: no provider"},
            "invalid_config",
        ),
        ({"status": "failed", "error": "model source refused the request"}, "unknown"),
    ],
)
async def test_a_permanent_failure_is_not_retried(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    respx_mock: respx.MockRouter,
    state: dict[str, Any],
    failure_class: str,
) -> None:
    started = fake_hermes(respx_mock, runs=[[state]])
    alerted = fake_alertmanager(respx_mock)
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    assert started.call_count == 1
    assert alerted.call_count == 1
    (alert,) = json.loads(alerted.calls.last.request.content)
    assert alert["annotations"]["description"] == state["error"]

    (row,) = rows()
    assert row["status"] == "failed"
    assert row["attempt"] == 1
    assert row["error"] == state["error"]
    assert (
        sample(
            metrics,
            "agent_trigger_failures_total",
            use_case="explain-episode",
            **{"class": failure_class},
        )
        == 1.0
    )


async def test_an_unreachable_harness_is_retried_then_reported(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    """The live check of this path: Hermes scaled to zero."""
    started = respx_mock.post(f"{HERMES_URL}/v1/runs").mock(
        side_effect=httpx.ConnectError("[Errno 111] Connection refused")
    )
    alerted = fake_alertmanager(respx_mock)
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    assert await episode_consumer.run_once() == 1

    assert started.call_count == 2
    assert started.calls.last.request.headers["Idempotency-Key"] == f"{KEY}/2"
    assert alerted.call_count == 1

    (row,) = rows()
    assert row["status"] == "failed"
    assert row["attempt"] == 2
    assert row["harness_run_id"] is None
    assert "Connection refused" in row["error"]
    assert (
        sample(
            metrics,
            "agent_trigger_failures_total",
            use_case="explain-episode",
            **{"class": "hermes_unreachable"},
        )
        == 2.0
    )


async def test_a_harness_that_refuses_the_start_is_retried(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    started = respx_mock.post(f"{HERMES_URL}/v1/runs").mock(
        return_value=httpx.Response(503, text="down")
    )
    alerted = fake_alertmanager(respx_mock)
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    assert started.call_count == 2
    assert alerted.call_count == 1
    (row,) = rows()
    assert row["status"] == "failed"
    assert row["attempt"] == 2
    assert "503" in row["error"]


async def test_a_long_error_is_cut_for_the_alert_but_kept_on_the_row(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    error = "invalid config: " + "x" * 3000
    fake_hermes(respx_mock, runs=[[{"status": "failed", "error": error}]])
    alerted = fake_alertmanager(respx_mock)
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (alert,) = json.loads(alerted.calls.last.request.content)
    assert alert["annotations"]["description"] == error[:1024]
    (row,) = rows()
    assert row["error"] == error


async def test_an_alertmanager_that_is_down_leaves_the_row_closed(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    fake_hermes(respx_mock, runs=[[AUTH_FAILED]])
    fake_alertmanager(respx_mock, status=503)
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    assert await episode_consumer.run_once() == 1

    (row,) = rows()
    assert row["status"] == "failed"
    assert sample(metrics, "agent_trigger_alerts_total", outcome="failed") == 1.0


async def test_a_row_a_dead_pod_left_running_is_failed_on_redelivery(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    execute: Execute,
    respx_mock: respx.MockRouter,
) -> None:
    """The pod died mid-run; the redelivered event finds its row still `running`.

    It is not started again — the harness may still be working on the first
    one — but closed and reported, so the interruption does not go unseen.
    """
    started = fake_hermes(respx_mock)
    alerted = fake_alertmanager(respx_mock)
    execute(
        "INSERT INTO agent_runs (use_case, trigger, subject_kind, subject_key, status,"
        " harness_run_id, language) VALUES (%s, 'event', 'episode', %s, 'running', 'run_0', 'de')",
        "explain-episode",
        "15510:appeared",
    )
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    assert not started.called
    assert alerted.call_count == 1
    (row,) = rows()
    assert row["status"] == "failed"
    assert row["attempt"] == 1
    assert "trigger stopped" in row["error"]
    assert row["finished_at"] is not None
    assert (
        sample(metrics, "agent_trigger_duplicate_events_total", use_case="explain-episode") == 0.0
    )


async def test_a_redelivery_of_a_closed_row_stays_a_duplicate(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    execute: Execute,
    respx_mock: respx.MockRouter,
) -> None:
    alerted = fake_alertmanager(respx_mock)
    execute(
        "INSERT INTO agent_runs (use_case, trigger, subject_kind, subject_key, status, language,"
        " finished_at) VALUES (%s, 'event', 'episode', %s, 'failed', 'de', now())",
        "explain-episode",
        "15510:appeared",
    )
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    assert not alerted.called
    assert (
        sample(metrics, "agent_trigger_duplicate_events_total", use_case="explain-episode") == 1.0
    )
