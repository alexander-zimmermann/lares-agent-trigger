"""One trace per turn and per event run, as Tempo would receive it and the row names it."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from lares_agent_trigger import turn_runs
from lares_agent_trigger.consumer import EpisodeConsumer
from lares_agent_trigger.metrics import Metrics
from lares_agent_trigger.spans import TraceIds

from .conftest import API_KEY
from .fakes import (
    ANSWER,
    CHAT_SESSION,
    CRON_SESSION,
    CRON_TASK,
    EXPLANATION,
    fake_alertmanager,
    fake_hermes,
    fake_job,
    model_call,
    sample,
    sign,
    tool_call,
    turn_ended,
)

Rows = Callable[[], list[dict[str, Any]]]
Receiver = tuple[httpx.AsyncClient, Metrics]
Consumer = tuple[EpisodeConsumer, Metrics]
Api = tuple[httpx.AsyncClient, Metrics]

HOOK = "/hooks/hermes"
# The engine's span that published the episode event, as its NATS headers carry it.
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
ENGINE_TRACE_ID = 0x0AF7651916CD43DD8448EB211C80319C
ENGINE_SPAN_ID = 0xB7AD6B7169203331
# The bridge's span that forwarded a person's start_run.
BRIDGE_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
BRIDGE_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
BRIDGE_SPAN_ID = 0x00F067AA0BA902B7
EPISODE_EVENT = {
    "episode_id": 15510,
    "fault": "appliance_runtime",
    "subject": "2/1/197",
    "severity": 2,
    "kind": "appeared",
    "time": "2026-09-25T14:20:00+00:00",
}
# The default arguments the fake model call asks its tool with.
ARGUMENTS = '{"state":"open","days":7}'


async def _deliver(client: httpx.AsyncClient, *bodies: bytes) -> None:
    for body in bodies:
        response = await client.post(HOOK, content=body, headers=sign(body))
        assert response.status_code == 200, response.text


def _named(spans: InMemorySpanExporter, name: str) -> ReadableSpan:
    (span,) = [span for span in spans.get_finished_spans() if span.name == name]
    return span


def _children(spans: InMemorySpanExporter, parent: ReadableSpan) -> list[ReadableSpan]:
    """The spans directly under `parent`, in the order they started."""
    assert parent.context is not None
    return sorted(
        (
            span
            for span in spans.get_finished_spans()
            if span.parent is not None and span.parent.span_id == parent.context.span_id
        ),
        key=lambda span: span.start_time or 0,
    )


async def _finished(spans: InMemorySpanExporter, name: str) -> ReadableSpan:
    """The span of that name, once the run going in the background has ended it."""
    for _ in range(500):
        found = [span for span in spans.get_finished_spans() if span.name == name]
        if found:
            (span,) = found
            return span
        await asyncio.sleep(0.01)
    raise AssertionError(f"no span {name} ended")


def _ns(seconds: float) -> int:
    return round(seconds * 1e9)


def _values(span: ReadableSpan) -> list[str]:
    return [str(value) for value in (span.attributes or {}).values()]


async def test_a_chat_turn_is_one_trace_its_row_names(
    receiver: Receiver, rows: Rows, spans: InMemorySpanExporter
) -> None:
    client, _ = receiver

    # The model asks for one tool, then answers; the turn ends a second later.
    await _deliver(
        client,
        model_call(1, tool_calls=1, started_at=1790756100.0, seconds=3.0),
        model_call(2, content=ANSWER, input_tokens=46000, started_at=1790756104.5, seconds=6.5),
        turn_ended(at=1790756112.0),
    )

    (row,) = rows()
    root = _named(spans, "invoke_agent messenger")
    assert root.parent is None
    assert root.context is not None
    assert row["trace_id"] == f"{root.context.trace_id:032x}"
    # From the first call's start to the turn's end, on the harness's own clock.
    assert root.start_time == _ns(1790756100.0)
    assert root.end_time == _ns(1790756112.0)
    assert root.attributes == {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": "messenger",
        "gen_ai.conversation.id": CHAT_SESSION,
        "lares.run.id": row["id"],
    }

    first, second = _children(spans, root)
    assert first.name == second.name == "chat gpt-6-sol"
    assert (first.start_time, first.end_time) == (_ns(1790756100.0), _ns(1790756103.0))
    assert (second.start_time, second.end_time) == (_ns(1790756104.5), _ns(1790756111.0))
    assert second.attributes == {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai-codex",
        "gen_ai.request.model": "gpt-6-sol",
        "gen_ai.usage.input_tokens": 48000,
        "gen_ai.usage.output_tokens": 300,
    }
    # Names and numbers only: neither the answer nor what a tool was asked.
    for span in (root, first, second):
        assert not any(
            ANSWER in value or ARGUMENTS in value or "list_episodes" in value
            for value in _values(span)
        )


async def test_each_tool_call_is_a_span_between_the_model_calls(
    receiver: Receiver, rows: Rows, spans: InMemorySpanExporter
) -> None:
    client, metrics = receiver
    result = '{"episodes": [{"id": 15510, "fault": "appliance_runtime"}]}'

    await _deliver(
        client,
        model_call(1, tool_calls=2, started_at=1790756100.0, seconds=3.0),
        tool_call("call_1_0", result=result, ended_at=1790756103.5, milliseconds=420),
        tool_call(
            "call_1_1",
            name="get_episode",
            arguments={"episode_id": 15510},
            result='{"error": "episode 15510 not found"}',
            ended_at=1790756104.2,
            milliseconds=900,
            status="error",
            error_type="tool_error",
            error_message="episode 15510 not found",
        ),
        model_call(2, content=ANSWER, started_at=1790756104.5, seconds=6.5),
        turn_ended(at=1790756112.0),
    )

    root = _named(spans, "invoke_agent messenger")
    children = _children(spans, root)
    assert [span.name for span in children] == [
        "chat gpt-6-sol",
        "execute_tool list_episodes",
        "execute_tool get_episode",
        "chat gpt-6-sol",
    ]
    listed, failed = children[1], children[2]
    # The hook fires as the tool returns; it started its duration before.
    assert (listed.start_time, listed.end_time) == (_ns(1790756103.08), _ns(1790756103.5))
    assert listed.attributes == {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "list_episodes",
        "gen_ai.tool.call.id": "call_1_0",
    }
    assert listed.status.status_code is StatusCode.UNSET
    assert (failed.start_time, failed.end_time) == (_ns(1790756103.3), _ns(1790756104.2))
    assert failed.status.status_code is StatusCode.ERROR
    assert failed.attributes is not None
    assert failed.attributes["error.type"] == "tool_error"
    # What the tool was asked, what it returned and how it said it failed stay out.
    for span in children:
        assert not any(
            fragment in value
            for value in _values(span)
            for fragment in ("15510", "not found", ARGUMENTS)
        )
    # The row's tool trace is the model's, as before; the hook's results never reach it.
    (row,) = rows()
    assert [tool["name"] for tool in row["tool_trace"]["calls"][0]["tools"]] == [
        "list_episodes",
        "list_episodes",
    ]
    assert result not in str(row)
    assert sample(metrics, "agent_trigger_hook_events_total", outcome="counted") == 4.0


async def test_a_tool_call_of_no_tallied_turn_is_left_alone(
    receiver: Receiver, rows: Rows, spans: InMemorySpanExporter
) -> None:
    """A tool whose turn no model call opened: another surface's, or one a restart lost."""
    client, metrics = receiver

    await _deliver(client, tool_call("call_9_0", turn="5e1d0a77"))

    assert rows() == []
    assert spans.get_finished_spans() == ()
    assert sample(metrics, "agent_trigger_hook_events_total", outcome="ignored") == 1.0


async def test_a_redelivered_tool_call_is_one_span(
    receiver: Receiver, spans: InMemorySpanExporter
) -> None:
    client, metrics = receiver
    call = tool_call("call_1_0")

    await _deliver(
        client,
        model_call(1, tool_calls=1, started_at=1790756100.0, seconds=3.0),
        call,
        call,
        model_call(2, content=ANSWER, started_at=1790756104.5, seconds=6.5),
        turn_ended(),
    )

    root = _named(spans, "invoke_agent messenger")
    assert [span.name for span in _children(spans, root)].count("execute_tool list_episodes") == 1
    assert sample(metrics, "agent_trigger_hook_events_total", outcome="duplicate") == 1.0


async def test_a_turn_not_sampled_leaves_its_row_without_a_trace(
    receiver: Receiver,
    rows: Rows,
    spans: InMemorySpanExporter,
    sampling_ratio: Callable[[float], None],
) -> None:
    sampling_ratio(0.0)
    client, _ = receiver

    await _deliver(client, model_call(1, content=ANSWER), turn_ended())

    (row,) = rows()
    assert row["status"] == "completed"
    assert row["trace_id"] is None
    assert spans.get_finished_spans() == ()


async def test_a_turn_that_never_ended_becomes_a_trace_when_it_is_dropped(
    receiver: Receiver, rows: Rows, spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness's own work after an answer reports calls but no end: no row, but a trace."""
    client, _ = receiver
    await _deliver(
        client,
        model_call(1, turn="0rphan01", tool_calls=1, started_at=1790756100.0, seconds=3.0),
        tool_call("call_1_0", turn="0rphan01", ended_at=1790756103.5, milliseconds=420),
        model_call(2, turn="0rphan01", started_at=1790756104.5, seconds=2.0),
    )

    # Every tally is past its hour from here on; the next call drops it.
    monkeypatch.setattr(turn_runs, "_TALLY_LIFETIME_SECONDS", -1.0)
    await _deliver(client, model_call(1, content=ANSWER))

    assert rows() == []
    root = _named(spans, "invoke_agent messenger")
    assert root.parent is None
    assert root.status.status_code is StatusCode.ERROR
    assert root.attributes == {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": "messenger",
        "gen_ai.conversation.id": CHAT_SESSION,
        "error.type": "turn_without_end",
    }
    # From its first call to the last thing it reported.
    assert (root.start_time, root.end_time) == (_ns(1790756100.0), _ns(1790756106.5))
    assert [span.name for span in _children(spans, root)] == [
        "chat gpt-6-sol",
        "execute_tool list_episodes",
        "chat gpt-6-sol",
    ]


async def test_a_failed_turn_is_a_failed_trace(
    receiver: Receiver, rows: Rows, spans: InMemorySpanExporter
) -> None:
    client, _ = receiver

    await _deliver(
        client,
        model_call(1, tool_calls=1),
        turn_ended(completed=False, failed=True, exit_reason="error(model_not_found)"),
    )

    (row,) = rows()
    root = _named(spans, "invoke_agent messenger")
    assert root.status.status_code is StatusCode.ERROR
    assert root.attributes is not None
    # A class, not the harness's reason: that stays on the row.
    assert root.attributes["error.type"] == "turn_failed"
    assert row["error"] == "error(model_not_found)"
    assert root.attributes["lares.run.id"] == row["id"]


# The turn of `run_1`, as the hook reports it under the session the fake harness gave the run.
def _api_call(number: int, **fields: Any) -> bytes:
    return model_call(
        number, session_id="sess_1", platform="api_server", task_id="0f3c9d2e-run", **fields
    )


async def test_an_event_run_continues_the_trace_of_its_episode_event(
    consumer: Consumer,
    receiver: Receiver,
    stream: Any,
    rows: Rows,
    spans: InMemorySpanExporter,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(respx_mock)
    episode_consumer, _ = consumer
    client, _ = receiver
    await _deliver(
        client,
        _api_call(1, tool_calls=1, started_at=1790714601.0, seconds=3.0),
        tool_call(
            "call_1_0",
            session_id="sess_1",
            task_id="0f3c9d2e-run",
            ended_at=1790714604.5,
            milliseconds=420,
        ),
        _api_call(2, content=EXPLANATION, started_at=1790714605.0, seconds=6.0),
        turn_ended(
            session_id="sess_1", platform="api_server", task_id="0f3c9d2e-run", at=1790714611.5
        ),
    )

    await stream.publish(
        "episode.appeared",
        json.dumps(EPISODE_EVENT).encode(),
        headers={"traceparent": TRACEPARENT},
    )
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed"
    assert row["trace_id"] == f"{ENGINE_TRACE_ID:032x}"
    # The engine's trace, joined where the trigger took the event off the stream.
    process = _named(spans, "process episode.appeared")
    assert process.parent is not None
    assert process.parent.span_id == ENGINE_SPAN_ID
    assert {span.context.trace_id for span in spans.get_finished_spans() if span.context} == {
        ENGINE_TRACE_ID
    }

    attempt = _named(spans, "run explain-episode")
    assert [span.name for span in _children(spans, process)] == ["run explain-episode"]
    assert attempt.attributes == {
        "gen_ai.agent.name": "explain-episode",
        "lares.run.id": row["id"],
        "lares.run.attempt": 1,
    }
    # The turn on the harness's clock, the trigger's own steps on its clock.
    turn = _named(spans, "invoke_agent explain-episode")
    steps = [span for span in _children(spans, attempt) if span is not turn]
    assert turn.parent is not None and attempt.context is not None
    assert turn.parent.span_id == attempt.context.span_id
    assert [span.name for span in steps] == [
        "POST",
        "await_run",
        "UPDATE agent_runs",
        "deliver explain-episode",
    ]
    # The polling is one span, the requests to the gateway under it.
    assert {span.name for span in _children(spans, steps[1])} == {"GET"}
    assert [span.name for span in _children(spans, turn)] == [
        "chat gpt-6-sol",
        "execute_tool list_episodes",
        "chat gpt-6-sol",
    ]
    assert (turn.start_time, turn.end_time) == (_ns(1790714601.0), _ns(1790714611.5))
    for span in spans.get_finished_spans():
        assert not any("Waschmaschine" in value for value in _values(span))


async def test_a_run_asked_for_through_the_api_continues_the_callers_trace(
    api: Api,
    rows: Rows,
    episode: int,
    relay: Any,
    discord: Any,
    spans: InMemorySpanExporter,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(respx_mock)
    client, _ = api

    response = await client.post(
        "/api/runs",
        json={"use_case": "explain-episode", "subject": str(episode)},
        headers={"Authorization": f"Bearer {API_KEY}", "traceparent": BRIDGE_TRACEPARENT},
    )

    assert response.status_code == 202
    attempt = await _finished(spans, "run explain-episode")
    (row,) = rows()
    assert row["trace_id"] == BRIDGE_TRACE_ID
    request = _named(spans, "POST /api/runs")
    assert request.kind is SpanKind.SERVER
    assert request.parent is not None
    assert request.parent.span_id == BRIDGE_SPAN_ID
    assert request.attributes == {
        "http.request.method": "POST",
        "http.route": "/api/runs",
        "http.response.status_code": 202,
    }
    # The run goes on after the answer, in the same trace.
    assert attempt.parent is not None and request.context is not None
    assert attempt.parent.span_id == request.context.span_id


async def test_a_hand_fed_run_is_in_the_trace_of_its_request(
    api: Api, rows: Rows, discord: Any, spans: InMemorySpanExporter
) -> None:
    client, _ = api

    response = await client.post(
        "/api/runs",
        json={"use_case": "summarise-week", "output": "Die Woche war ruhig."},
        headers={"Authorization": f"Bearer {API_KEY}", "traceparent": BRIDGE_TRACEPARENT},
    )

    assert response.status_code == 200
    (row,) = rows()
    assert row["trace_id"] == BRIDGE_TRACE_ID


async def test_a_request_without_a_traceparent_starts_a_trace_of_its_own(
    api: Api, rows: Rows, spans: InMemorySpanExporter
) -> None:
    client, _ = api

    response = await client.post(
        "/api/runs",
        json={"use_case": "explain-episode", "subject": "99"},
        headers={"Authorization": f"Bearer {API_KEY}"},
    )

    assert response.status_code == 404
    request = _named(spans, "POST /api/runs")
    assert request.parent is None
    assert request.attributes is not None
    assert request.attributes["http.response.status_code"] == 404


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """The service's log lines, with the ids the service's own handler adds."""
    caplog.set_level(logging.INFO, logger="lares_agent_trigger")
    caplog.handler.addFilter(TraceIds())
    return caplog


def _ids(record: logging.LogRecord) -> tuple[str | None, str | None]:
    return getattr(record, "trace_id", None), getattr(record, "span_id", None)


async def test_a_turns_log_line_carries_its_trace(
    receiver: Receiver,
    rows: Rows,
    spans: InMemorySpanExporter,
    logs: pytest.LogCaptureFixture,
) -> None:
    client, _ = receiver

    await _deliver(client, model_call(1, content=ANSWER), turn_ended())

    (row,) = rows()
    root = _named(spans, "invoke_agent messenger")
    assert root.context is not None
    (line,) = [record for record in logs.records if f"run {row['id']}" in record.getMessage()]
    assert _ids(line) == (row["trace_id"], f"{root.context.span_id:016x}")


async def test_an_event_runs_log_lines_carry_its_trace(
    consumer: Consumer,
    stream: Any,
    rows: Rows,
    spans: InMemorySpanExporter,
    logs: pytest.LogCaptureFixture,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(respx_mock)
    episode_consumer, _ = consumer

    await stream.publish(
        "episode.appeared",
        json.dumps(EPISODE_EVENT).encode(),
        headers={"traceparent": TRACEPARENT},
    )
    await episode_consumer.run_once()

    (row,) = rows()
    attempt = _named(spans, "run explain-episode")
    assert attempt.context is not None
    (line,) = [record for record in logs.records if f"run {row['id']}" in record.getMessage()]
    assert _ids(line) == (f"{ENGINE_TRACE_ID:032x}", f"{attempt.context.span_id:016x}")
    # Outside every span a line carries neither.
    logging.getLogger("lares_agent_trigger.test").info("between runs")
    assert _ids(logs.records[-1]) == (None, None)


async def test_a_cron_turn_is_one_trace(
    receiver: Receiver, rows: Rows, spans: InMemorySpanExporter, respx_mock: respx.MockRouter
) -> None:
    """Asking the Jobs API whose job it was happens before the trace and is in none."""
    fake_job(respx_mock, name="lares:propose-faults")
    client, _ = receiver

    await _deliver(
        client,
        model_call(1, session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK, content="Drei."),
        turn_ended(session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK),
    )

    (row,) = rows()
    root = _named(spans, "invoke_agent propose-faults")
    assert root.context is not None
    assert row["trace_id"] == f"{root.context.trace_id:032x}"
    assert {span.context.trace_id for span in spans.get_finished_spans() if span.context} == {
        root.context.trace_id
    }


async def test_a_failed_event_run_is_closed_inside_its_attempt(
    consumer: Consumer,
    stream: Any,
    rows: Rows,
    spans: InMemorySpanExporter,
    logs: pytest.LogCaptureFixture,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(
        respx_mock,
        runs=[[{"status": "failed", "error": "Provider authentication failed: 401 Unauthorized"}]],
    )
    fake_alertmanager(respx_mock)
    episode_consumer, _ = consumer

    await stream.publish(
        "episode.appeared",
        json.dumps(EPISODE_EVENT).encode(),
        headers={"traceparent": TRACEPARENT},
    )
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    attempt = _named(spans, "run explain-episode")
    assert attempt.context is not None
    assert attempt.status.status_code is StatusCode.ERROR
    assert attempt.attributes is not None
    assert attempt.attributes["error.type"] == "auth_failed"
    # What the failed run spent is written under the attempt, and so is why it failed.
    assert "UPDATE agent_runs" in [span.name for span in _children(spans, attempt)]
    (line,) = [record for record in logs.records if record.levelno == logging.ERROR]
    assert _ids(line) == (f"{ENGINE_TRACE_ID:032x}", f"{attempt.context.span_id:016x}")
