"""Failed turns: a turn whose last call failed for good is a failed row, never silence."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from lares_agent_trigger.config import Settings
from lares_agent_trigger.metrics import Metrics

from .fakes import (
    ANSWER,
    CHAT_SESSION,
    CHAT_TURN,
    CRON_SESSION,
    CRON_TASK,
    FAILED_PROMPT,
    PERSONS,
    failed_call,
    fake_job,
    model_call,
    review_call,
    sample,
    sign,
    turn_ended,
    until,
)

Rows = Callable[[], list[dict[str, Any]]]
Receiver = tuple[httpx.AsyncClient, Metrics]

HOOK = "/hooks/hermes"
OTHER_SESSION = "20260930_102000_d4e5f6"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The declaration with persons in it."""
    settings.persons_file.write_text(PERSONS, encoding="utf-8")
    return settings


async def _deliver(client: httpx.AsyncClient, *bodies: bytes) -> None:
    for body in bodies:
        response = await client.post(HOOK, content=body, headers=sign(body))
        assert response.status_code == 200, response.text


def _failed_turns(metrics: Metrics, enduser_id: str, error_type: str) -> float:
    return sample(
        metrics,
        "agent_trigger_failed_chat_turns_total",
        enduser_id=enduser_id,
        error_type=error_type,
    )


async def _closed(metrics: Metrics, *, use_case: str = "messenger", enduser_id: str = "") -> None:
    """Wait for the sweep to close the turn: counting its failed run is the last thing it does."""
    await until(
        lambda: (
            sample(
                metrics,
                "agent_trigger_recorded_runs_total",
                use_case=use_case,
                status="failed",
                enduser_id=enduser_id,
            )
            > 0
        )
    )


def _root(spans: InMemorySpanExporter) -> ReadableSpan:
    (root,) = [span for span in spans.get_finished_spans() if span.parent is None]
    return root


def _children(spans: InMemorySpanExporter, root: ReadableSpan) -> list[ReadableSpan]:
    """The spans under the root in time order; each attempt at a call starts with the first."""
    assert root.context is not None
    return sorted(
        (
            span
            for span in spans.get_finished_spans()
            if span.parent is not None and span.parent.span_id == root.context.span_id
        ),
        key=lambda span: (span.start_time or 0, span.end_time or 0),
    )


def _ns(seconds: float) -> int:
    return round(seconds * 1e9)


async def test_a_turn_whose_only_call_failed_and_that_never_ends_is_a_failed_row(
    receiver: Receiver, rows: Rows
) -> None:
    client, metrics = receiver

    await _deliver(client, failed_call(1, profile="kim"))

    await _closed(metrics, enduser_id="kim")
    (row,) = rows()
    assert row["use_case"] == "messenger"
    assert row["subject_key"] == f"{CHAT_SESSION}:{CHAT_TURN}"
    assert (row["status"], row["enduser_id"], row["surface"]) == ("failed", "kim", "discord")
    assert (row["model_source"], row["model"]) == ("openai-api", "gpt-6-luna")
    assert row["error"] == "the last model call to openai-api failed with 401 (auth)"
    assert _failed_turns(metrics, "kim", "auth") == 1.0


async def test_a_failed_turn_is_a_failed_trace_its_row_names(
    receiver: Receiver, rows: Rows, spans: InMemorySpanExporter
) -> None:
    client, metrics = receiver

    await _deliver(client, failed_call(1, profile="kim", started_at=1790756100.0, seconds=0.7))

    await _closed(metrics, enduser_id="kim")
    (row,) = rows()
    root = _root(spans)
    assert root.context is not None
    assert row["trace_id"] == f"{root.context.trace_id:032x}"
    assert root.status.status_code is StatusCode.ERROR
    assert root.attributes is not None
    assert root.attributes["error.type"] == "turn_failed"
    assert root.attributes["enduser.id"] == "kim"
    # The failed attempt, on the harness's clock, with the harness's class of it.
    (attempt,) = _children(spans, root)
    assert (attempt.name, attempt.start_time, attempt.end_time) == (
        "chat gpt-6-luna",
        _ns(1790756100.0),
        _ns(1790756100.7),
    )
    assert attempt.status.status_code is StatusCode.ERROR
    assert attempt.attributes is not None
    assert attempt.attributes["error.type"] == "auth"
    assert attempt.attributes["gen_ai.provider.name"] == "openai-api"
    assert (root.start_time, root.end_time) == (_ns(1790756100.0), _ns(1790756100.7))


async def test_what_the_failed_request_carried_reaches_neither_row_nor_trace(
    receiver: Receiver, rows: Rows, spans: InMemorySpanExporter
) -> None:
    """kim keeps answers; the request with the person's message is still never read."""
    client, metrics = receiver

    await _deliver(client, failed_call(1, profile="kim"))

    await _closed(metrics, enduser_id="kim")
    (row,) = rows()
    assert FAILED_PROMPT not in str(row)
    # Nor the source's own words for the failure.
    assert "Incorrect API key" not in str(row)
    assert not any(
        FAILED_PROMPT in str(value) or "Incorrect API key" in str(value)
        for span in spans.get_finished_spans()
        for value in (span.attributes or {}).values()
    )


async def test_a_turn_answered_after_a_failed_attempt_is_not_failed(
    receiver: Receiver, rows: Rows, spans: InMemorySpanExporter
) -> None:
    """The subscription refuses, the fallback answers: the attempt shows in the trace only."""
    client, metrics = receiver

    await _deliver(
        client,
        failed_call(
            1,
            profile="kim",
            model="gpt-6-sol",
            provider="openai-codex",
            status_code=429,
            reason="rate_limit",
            started_at=1790756100.0,
            seconds=0.5,
        ),
        model_call(
            1,
            profile="kim",
            content=ANSWER,
            model="gpt-6-luna",
            provider="openai-api",
            started_at=1790756100.0,
            seconds=4.0,
        ),
        turn_ended(profile="kim", model="gpt-6-luna", at=1790756104.5),
    )

    (row,) = rows()
    assert (row["status"], row["error"], row["text"]) == ("completed", None, ANSWER)
    assert (row["model_source"], row["model"]) == ("openai-api", "gpt-6-luna")
    assert _failed_turns(metrics, "kim", "rate_limit") == 0.0
    root = _root(spans)
    assert root.status.status_code is not StatusCode.ERROR
    assert [
        (span.name, span.status.status_code is StatusCode.ERROR) for span in _children(spans, root)
    ] == [("chat gpt-6-sol", True), ("chat gpt-6-luna", False)]


async def test_a_turn_whose_end_says_it_failed_after_its_last_call_failed_names_the_failure(
    receiver: Receiver, rows: Rows
) -> None:
    """The harness gave up after its retries and still finished the turn."""
    client, metrics = receiver

    await _deliver(
        client,
        failed_call(1, profile="lou", status_code=503, reason="overloaded", seconds=0.5),
        failed_call(1, profile="lou", status_code=503, reason="overloaded", seconds=9.5),
        turn_ended(profile="lou", completed=False, failed=True, exit_reason="error(overloaded)"),
    )

    (row,) = rows()
    assert (row["status"], row["enduser_id"]) == ("failed", "lou")
    assert row["error"] == "the last model call to openai-api failed with 503 (overloaded)"
    assert _failed_turns(metrics, "lou", "overloaded") == 1.0


async def test_a_failed_attempt_delivered_twice_is_one_attempt(
    receiver: Receiver, rows: Rows, spans: InMemorySpanExporter
) -> None:
    client, metrics = receiver
    attempt = failed_call(1, profile="kim")

    await _deliver(client, attempt, attempt)

    await _closed(metrics, enduser_id="kim")
    assert len(rows()) == 1
    assert len(_children(spans, _root(spans))) == 1
    assert _failed_turns(metrics, "kim", "auth") == 1.0
    assert sample(metrics, "agent_trigger_hook_events_total", outcome="duplicate") == 1.0


async def test_a_source_that_answers_no_status_is_named_without_one(
    receiver: Receiver, rows: Rows
) -> None:
    """Codex fails a quota inside a 200 answer and names its code as a word."""
    client, metrics = receiver

    await _deliver(
        client,
        failed_call(
            1,
            profile="kim",
            model="gpt-6-sol",
            provider="openai-codex",
            status_code="usage_limit_reached",
            reason="invalid_response",
        ),
    )

    await _closed(metrics, enduser_id="kim")
    (row,) = rows()
    assert row["error"] == "the last model call to openai-codex failed (invalid_response)"


async def test_a_background_review_that_fails_leaves_the_answer_alone(
    receiver: Receiver, rows: Rows
) -> None:
    """A review answers nobody: its failure is no failed turn."""
    client, metrics = receiver

    await _deliver(
        client,
        model_call(1, profile="kim", content=ANSWER),
        turn_ended(profile="kim"),
        review_call(1, profile="kim", tool_calls=1),
        failed_call(
            2, profile="kim", task_id="7c1e9a52-3f0d-4b8e-9a61-2d5c8e0f4b13", turn="d3b07384"
        ),
        # A turn of another session failing after it: once the sweep closed this
        # one, it would have closed a failed review as well.
        failed_call(1, profile="kim", session_id=OTHER_SESSION, reason="rate_limit"),
    )

    await _closed(metrics, enduser_id="kim")
    answer, other = rows()
    assert (answer["status"], answer["error"]) == ("completed", None)
    assert (other["session_id"], other["status"]) == (OTHER_SESSION, "failed")
    assert _failed_turns(metrics, "kim", "auth") == 0.0
    assert _failed_turns(metrics, "kim", "rate_limit") == 1.0


@pytest.mark.respx(assert_all_called=False)
async def test_a_cron_turn_whose_last_call_failed_is_its_use_cases_failed_row(
    receiver: Receiver, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    """A schedule run fails like a chat turn, and no person got an error."""
    fake_job(respx_mock, name="lares:propose-faults")
    client, metrics = receiver

    await _deliver(
        client,
        failed_call(1, session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK),
    )

    await _closed(metrics, use_case="propose-faults")
    (row,) = rows()
    assert (row["use_case"], row["status"], row["enduser_id"]) == (
        "propose-faults",
        "failed",
        None,
    )
    assert row["error"] == "the last model call to openai-api failed with 401 (auth)"
    assert _failed_turns(metrics, "", "auth") == 0.0
