"""Background reviews: their calls booked on the row of the answer that set them off."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lares_agent_trigger.config import Settings
from lares_agent_trigger.metrics import Metrics

from .fakes import (
    ANSWER,
    CHAT_SESSION,
    PERSONS,
    REVIEWED,
    model_call,
    review_call,
    review_tool,
    sample,
    sign,
    turn_ended,
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


def _review(row: dict[str, Any]) -> tuple[object, ...]:
    return (
        row["review_tokens_in"],
        row["review_tokens_out"],
        row["review_cost"],
        row["review_text"],
    )


async def test_a_reviews_calls_land_priced_on_the_newest_answer_of_its_session(
    receiver: Receiver, rows: Rows
) -> None:
    client, _ = receiver

    await _deliver(
        client,
        model_call(1, profile="kim", content=ANSWER, turn="1a2b3c4d"),
        turn_ended(profile="kim", turn="1a2b3c4d"),
        model_call(1, profile="kim", content=ANSWER),
        turn_ended(profile="kim"),
        model_call(1, profile="kim", content=ANSWER, session_id=OTHER_SESSION),
        turn_ended(profile="kim", session_id=OTHER_SESSION),
        # The review reads on the subscription, then answers on the fallback.
        review_call(1, profile="kim", tool_calls=1),
        review_call(
            2,
            profile="kim",
            content=REVIEWED,
            model="gpt-6-luna",
            provider="openai-api",
            input_tokens=10000,
            cache_read_tokens=4000,
            output_tokens=500,
        ),
    )

    earlier, newest, other = rows()
    assert newest["session_id"] == CHAT_SESSION
    # The first call costs nothing; the second 10k fresh at 0.10, 4k cached at 0.01,
    # 500 out at 0.50.
    assert _review(newest) == (42000 + 14000, 300 + 500, Decimal("0.001290"), REVIEWED)
    # The answer's own numbers are the answer's.
    assert (newest["tokens_in"], newest["tokens_out"], newest["cost"]) == (42000, 300, Decimal(0))
    assert _review(earlier) == _review(other) == (None, None, None, None)


async def test_a_person_who_keeps_no_answers_gets_the_reviews_numbers_only(
    receiver: Receiver, rows: Rows
) -> None:
    client, metrics = receiver

    await _deliver(
        client,
        model_call(1, profile="lou", content=ANSWER, model="gpt-6-luna"),
        turn_ended(profile="lou", model="gpt-6-luna"),
        review_call(
            1,
            profile="lou",
            content=REVIEWED,
            model="gpt-6-luna",
            provider="openai-api",
            input_tokens=10000,
            cache_read_tokens=4000,
            output_tokens=500,
        ),
    )

    (row,) = rows()
    # lou's fallback at its own price: 10k fresh at 0.20, 4k cached at 0.02, 500 out at 1.00.
    assert _review(row) == (14000, 500, Decimal("0.002580"), None)
    # What the review cost is lou's spend, beside the answer's.
    assert sample(
        metrics,
        "agent_trigger_cost_usd_total",
        use_case="messenger",
        source="free-account",
        step="fallback",
        enduser_id="lou",
    ) == pytest.approx(0.00258)


async def test_a_review_call_delivered_twice_is_booked_once(receiver: Receiver, rows: Rows) -> None:
    client, _ = receiver

    await _deliver(
        client,
        model_call(1, profile="kim", content=ANSWER),
        turn_ended(profile="kim"),
        review_call(1, profile="kim", tool_calls=1),
        review_call(1, profile="kim", tool_calls=1),
        review_call(2, profile="kim", content=REVIEWED),
    )

    (row,) = rows()
    assert _review(row) == (2 * 42000, 2 * 300, Decimal(0), REVIEWED)


async def test_a_review_whose_first_call_is_unknown_throughout_stays_unknown(
    receiver: Receiver, rows: Rows
) -> None:
    """No usage, no declared price and no reply: the row looks as if it had no review."""
    client, _ = receiver

    await _deliver(
        client,
        model_call(1, profile="kim", content=ANSWER),
        turn_ended(profile="kim"),
        review_call(1, profile="kim", tool_calls=1, usage=False, model="gpt-7-nova"),
        review_call(2, profile="kim", content=REVIEWED),
    )

    (row,) = rows()
    assert _review(row) == (None, None, None, REVIEWED)


async def test_a_review_call_without_usage_leaves_the_reviews_tokens_unknown(
    receiver: Receiver, rows: Rows
) -> None:
    client, _ = receiver

    await _deliver(
        client,
        model_call(1, profile="kim", content=ANSWER),
        turn_ended(profile="kim"),
        review_call(1, profile="kim", tool_calls=1),
        review_call(2, profile="kim", usage=False),
        review_call(3, profile="kim", content=REVIEWED),
    )

    (row,) = rows()
    # The subscription costs nothing, with usage or without.
    assert _review(row) == (None, None, Decimal(0), REVIEWED)


def _ns(seconds: float) -> int:
    return round(seconds * 1e9)


async def test_a_reviews_calls_and_tools_join_the_answers_trace(
    receiver: Receiver, rows: Rows, spans: InMemorySpanExporter
) -> None:
    client, _ = receiver

    await _deliver(
        client,
        model_call(1, profile="kim", content=ANSWER, started_at=1790756100.0, seconds=4.0),
        turn_ended(profile="kim", at=1790756105.0),
        review_call(1, profile="kim", tool_calls=1, started_at=1790756106.0, seconds=3.0),
        review_tool(
            "call_1_0", profile="kim", name="memory", ended_at=1790756109.5, milliseconds=200
        ),
        review_call(2, profile="kim", content=REVIEWED, started_at=1790756110.0, seconds=2.0),
    )

    (row,) = rows()
    (root,) = [span for span in spans.get_finished_spans() if span.parent is None]
    assert root.name == "invoke_agent messenger"
    assert root.context is not None
    assert row["trace_id"] == f"{root.context.trace_id:032x}"
    children = sorted(
        (
            span
            for span in spans.get_finished_spans()
            if span.parent is not None and span.parent.span_id == root.context.span_id
        ),
        key=lambda span: span.start_time or 0,
    )
    # The answer's call, then the review's after the answer's end, on the harness's clock.
    assert [(span.name, span.start_time, span.end_time) for span in children] == [
        ("chat gpt-6-sol", _ns(1790756100.0), _ns(1790756104.0)),
        ("chat gpt-6-sol", _ns(1790756106.0), _ns(1790756109.0)),
        ("execute_tool memory", _ns(1790756109.3), _ns(1790756109.5)),
        ("chat gpt-6-sol", _ns(1790756110.0), _ns(1790756112.0)),
    ]
    # Names and numbers only: never what the review said.
    assert not any(
        REVIEWED in str(value)
        for span in spans.get_finished_spans()
        for value in (span.attributes or {}).values()
    )


async def test_a_review_in_a_session_without_an_answer_row_is_counted_orphaned(
    receiver: Receiver, rows: Rows
) -> None:
    """The answer's end never came through the hook, so no row holds the session."""
    client, metrics = receiver

    await _deliver(
        client,
        review_call(1, profile="kim", tool_calls=1),
        review_call(1, profile="kim", tool_calls=1),
        review_call(2, profile="kim", content=REVIEWED),
    )

    assert rows() == []
    assert sample(metrics, "agent_trigger_orphaned_calls_total", enduser_id="kim") == 2.0
