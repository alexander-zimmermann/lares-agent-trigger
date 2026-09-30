"""Chat and cron runs end to end: signed hook deliveries in, one ledger row per turn out."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx

from lares_agent_trigger import turn_runs
from lares_agent_trigger.config import Settings
from lares_agent_trigger.consumer import EpisodeConsumer
from lares_agent_trigger.metrics import Metrics

from .conftest import HERMES_URL
from .fakes import (
    ANSWER,
    CHAT_SESSION,
    CHAT_TURN,
    COMPLETED,
    CRON_SESSION,
    CRON_TASK,
    EXECUTION_ID,
    EXPLANATION,
    JOB_ID,
    SESSION,
    fake_hermes,
    fake_job,
    model_call,
    sample,
    sign,
    start_response,
    turn_ended,
)

Rows = Callable[[], list[dict[str, Any]]]
Receiver = tuple[httpx.AsyncClient, Metrics]
Consumer = tuple[EpisodeConsumer, Metrics]
Publish = Callable[..., Awaitable[None]]

HOOK = "/hooks/hermes"

# A cron test that never reaches its Jobs API route is not a failure here.
pytestmark = pytest.mark.respx(assert_all_called=False)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """Wait long enough for a hook delivery that is still on its way."""
    return settings.model_copy(update={"trace_wait_seconds": 1.0})


async def _deliver(client: httpx.AsyncClient, *bodies: bytes) -> list[int]:
    """Post each body as the gateway does: one after the other, signed."""
    return [
        (await client.post(HOOK, content=body, headers=sign(body))).status_code for body in bodies
    ]


def _cron_call(number: int, **fields: Any) -> bytes:
    return model_call(number, session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK, **fields)


def _cron_end() -> bytes:
    return turn_ended(session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK)


# A run the trigger started through the Runs API: its session is the one the
# fake harness reports for `run_1`.
def _api_call(number: int, **fields: Any) -> bytes:
    return model_call(
        number, session_id="sess_1", platform="api_server", task_id="0f3c9d2e-run", **fields
    )


def _api_end() -> bytes:
    return turn_ended(session_id="sess_1", platform="api_server", task_id="0f3c9d2e-run")


def _hook_events(metrics: Metrics, outcome: str) -> float:
    return sample(metrics, "agent_trigger_hook_events_total", outcome=outcome)


async def test_a_discord_turn_becomes_a_messenger_row(receiver: Receiver, rows: Rows) -> None:
    client, metrics = receiver

    # The model asks for one tool, then answers.
    statuses = await _deliver(
        client,
        model_call(1, tool_calls=1, started_at=1790756100.0, seconds=3.0),
        model_call(2, content=ANSWER, input_tokens=46000, started_at=1790756104.5, seconds=6.5),
        turn_ended(),
    )

    assert statuses == [200, 200, 200]
    (row,) = rows()
    assert row["use_case"] == "messenger"
    assert row["trigger"] == "message"
    assert row["subject_kind"] == "chat"
    # Session, then the turn: the part before the colon finds every turn of
    # one conversation, the whole key finds this one.
    assert row["subject_key"] == f"{CHAT_SESSION}:{CHAT_TURN}"
    assert row["session_id"] == CHAT_SESSION
    assert row["harness_run_id"] == f"{CHAT_SESSION}:{CHAT_SESSION}:{CHAT_TURN}"
    assert row["status"] == "completed"
    assert row["attempt"] == 1
    assert row["language"] == "de"
    assert row["text"] == ANSWER
    assert row["tldr"] == "Der Gefrierschrank läuft seit 17.8. auf Stufe 3."
    # Who served the turn, from the calls themselves.
    assert row["model"] == "gpt-6-sol"
    assert row["model_source"] == "openai-codex"
    # What the model read, cache included, and what it wrote, over both calls.
    assert row["tokens_in"] == (40000 + 2000) + (46000 + 2000)
    assert row["tokens_out"] == 300 + 300
    # Which tools the turn asked for, call by call, never what they returned.
    assert row["tool_trace"] == {
        "tool_count": 1,
        "calls": [
            {
                "call": 1,
                "tokens_in": 42000,
                "tokens_out": 300,
                "seconds": 3.0,
                "tools": [{"name": "list_episodes", "arguments": '{"state":"open","days":7}'}],
            },
            {"call": 2, "tokens_in": 48000, "tokens_out": 300, "seconds": 6.5, "tools": []},
        ],
    }
    # From the first call's start to the last call's end.
    assert row["duration"].total_seconds() == pytest.approx(11.0)
    # A flat subscription bills nothing per call; nothing is guessed in its place.
    assert row["cost"] is None
    assert row["finished_at"] is not None

    assert _hook_events(metrics, "counted") == 2.0
    assert _hook_events(metrics, "recorded") == 1.0
    assert (
        sample(
            metrics, "agent_trigger_recorded_runs_total", use_case="messenger", status="completed"
        )
        == 1.0
    )


async def test_each_turn_of_a_conversation_carries_only_its_own_calls(
    receiver: Receiver, rows: Rows
) -> None:
    """A conversation keeps its session across turns; a row is one turn, never the session."""
    client, _ = receiver

    await _deliver(
        client,
        model_call(1, content=ANSWER),
        turn_ended(),
        model_call(1, turn="77aa01bc", content="Die Tür ist zu.", input_tokens=90000),
        turn_ended(turn="77aa01bc"),
    )

    earlier, later = rows()
    assert earlier["tokens_in"] == 42000
    assert later["subject_key"] == f"{CHAT_SESSION}:77aa01bc"
    assert later["text"] == "Die Tür ist zu."
    assert later["tokens_in"] == 92000
    assert later["tool_trace"]["tool_count"] == 0
    assert [call["call"] for call in later["tool_trace"]["calls"]] == [1]


async def test_a_fallback_shows_as_the_model_that_answered(receiver: Receiver, rows: Rows) -> None:
    client, _ = receiver

    await _deliver(
        client,
        model_call(1, tool_calls=1),
        model_call(2, content=ANSWER, model="grok-4.3", provider="xai"),
        turn_ended(),
    )

    (row,) = rows()
    assert row["model"] == "grok-4.3"
    assert row["model_source"] == "xai"


async def test_a_call_without_usage_leaves_the_turns_tokens_unknown(
    receiver: Receiver, rows: Rows
) -> None:
    client, _ = receiver

    await _deliver(
        client,
        model_call(1, tool_calls=1),
        model_call(2, content=ANSWER, usage=False),
        turn_ended(),
    )

    (row,) = rows()
    assert row["tokens_in"] is None
    assert row["tokens_out"] is None
    assert row["text"] == ANSWER


async def test_a_turn_that_failed_is_a_failed_row_with_its_reason(
    receiver: Receiver, rows: Rows
) -> None:
    client, metrics = receiver

    statuses = await _deliver(
        client,
        model_call(1, tool_calls=1),
        turn_ended(completed=False, failed=True, exit_reason="error(model_not_found)"),
    )

    assert statuses == [200, 200]
    (row,) = rows()
    assert row["status"] == "failed"
    assert row["error"] == "error(model_not_found)"
    assert row["text"] is None
    assert row["tldr"] is None
    # What it spent before it failed still counts.
    assert row["tokens_in"] == 42000
    assert (
        sample(metrics, "agent_trigger_recorded_runs_total", use_case="messenger", status="failed")
        == 1.0
    )


async def test_the_same_deliveries_twice_yield_one_row(receiver: Receiver, rows: Rows) -> None:
    client, metrics = receiver
    call = model_call(1, content=ANSWER)
    end = turn_ended()

    # A replay is answered like a success: the gateway must not send it a third time.
    statuses = await _deliver(client, call, call, end, end)

    assert statuses == [200, 200, 200, 200]
    (row,) = rows()
    assert row["tokens_in"] == 42000
    assert _hook_events(metrics, "counted") == 1.0
    assert _hook_events(metrics, "recorded") == 1.0
    assert _hook_events(metrics, "duplicate") == 2.0


async def test_a_turn_whose_calls_went_unheard_keeps_its_row_without_them(
    receiver: Receiver, rows: Rows
) -> None:
    """A restart between a turn's calls and its end loses the tally, never the row."""
    client, _ = receiver

    await _deliver(client, turn_ended())

    (row,) = rows()
    assert row["status"] == "completed"
    assert row["model"] == "gpt-6-sol"
    assert row["model_source"] is None
    assert row["text"] is None
    assert row["tokens_in"] is None
    assert row["tool_trace"] is None
    assert row["duration"] is None


async def test_a_bad_signature_is_refused_and_counted(receiver: Receiver, rows: Rows) -> None:
    client, metrics = receiver

    body = turn_ended()
    forged = await client.post(HOOK, content=body, headers=sign(body, secret="x" * 32))
    unsigned = await client.post(HOOK, content=body, headers={"Content-Type": "application/json"})
    tampered = await client.post(
        HOOK, content=body.replace(b"discord", b"cron"), headers=sign(body)
    )

    assert forged.status_code == 401
    assert unsigned.status_code == 401
    assert tampered.status_code == 401
    assert rows() == []
    assert _hook_events(metrics, "refused") == 3.0


async def test_a_body_that_is_neither_hook_is_refused(receiver: Receiver, rows: Rows) -> None:
    client, metrics = receiver

    # 4xx is final for the gateway: a body it cannot fix is never sent twice.
    statuses = await _deliver(
        client,
        turn_ended(event="post_tool_call"),
        b"not json",
        turn_ended().replace(b'"turn_id"', b'"turn"'),
        turn_ended().replace(b'"platform"', b'"surface"'),
        model_call(1).replace(b'"api_call_count"', b'"calls"'),
        model_call(1, tool_calls=1).replace(b'"name": "list_episodes"', b'"tool": "x"'),
    )

    assert statuses == [400, 400, 400, 400, 400, 400]
    assert rows() == []
    assert _hook_events(metrics, "invalid") == 6.0


async def test_a_run_of_the_api_server_is_left_to_the_event_path(
    receiver: Receiver, rows: Rows
) -> None:
    """The trigger started it and already holds its row; a second one would double
    every run. Its calls are kept for that row instead."""
    client, metrics = receiver

    statuses = await _deliver(client, _api_call(1, content=ANSWER), _api_end())

    assert statuses == [200, 200]
    assert rows() == []
    assert _hook_events(metrics, "counted") == 1.0
    assert _hook_events(metrics, "traced") == 1.0


async def test_a_cron_run_of_a_managed_job_is_a_row_of_its_use_case(
    receiver: Receiver, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    job = fake_job(respx_mock, name="lares:propose-faults")
    client, _ = receiver

    statuses = await _deliver(
        client,
        _cron_call(1, tool_calls=3),
        _cron_call(2, content="Drei Vorschläge."),
        _cron_end(),
    )

    assert statuses == [200, 200, 200]
    assert job.call_count == 1
    (row,) = rows()
    assert row["use_case"] == "propose-faults"
    assert row["trigger"] == "schedule"
    assert row["subject_kind"] == "none"
    # The job, then the execution: the part before the colon finds every run of one job.
    assert row["subject_key"] == f"{JOB_ID}:{EXECUTION_ID}"
    assert row["session_id"] == CRON_SESSION
    assert row["language"] == "en"
    assert row["text"] == "Drei Vorschläge."
    assert row["tool_trace"]["tool_count"] == 3


@pytest.mark.parametrize(
    "name",
    [
        "Tägliche Erinnerung",  # made in the dashboard, not by the trigger
        "lares:explain-episode",  # declared, but not a schedule use case
        "lares:summarise-week",  # a schedule use case, but dormant
        "lares:retired-use-case",  # the prefix, without a declaration
        None,  # deleted before the delivery arrived
    ],
)
async def test_a_cron_run_the_file_does_not_declare_is_ignored(
    receiver: Receiver, rows: Rows, respx_mock: respx.MockRouter, name: str | None
) -> None:
    fake_job(respx_mock, name=name)
    client, metrics = receiver

    statuses = await _deliver(client, _cron_call(1), _cron_end())

    assert statuses == [200, 200]
    assert rows() == []
    assert _hook_events(metrics, "ignored") == 1.0


async def test_a_job_the_harness_cannot_name_is_answered_for_a_retry(
    receiver: Receiver, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    """Without the job's name the run has no use case; 5xx is the answer the gateway retries."""
    respx_mock.get(f"{HERMES_URL}/api/jobs/{JOB_ID}").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"job": {"id": JOB_ID, "name": "lares:propose-faults"}}),
        ]
    )
    client, metrics = receiver
    end = _cron_end()

    first = await _deliver(client, _cron_call(1, content="Drei Vorschläge."), end)
    assert first == [200, 503]
    assert rows() == []
    assert _hook_events(metrics, "error") == 1.0

    # The gateway's one retry lands, with everything the calls reported.
    assert await _deliver(client, end) == [200]
    (row,) = rows()
    assert row["use_case"] == "propose-faults"
    assert row["tokens_in"] == 42000
    assert row["text"] == "Drei Vorschläge."


async def test_long_arguments_are_cut_in_the_trace(receiver: Receiver, rows: Rows) -> None:
    client, _ = receiver
    names = ",".join(f'"Raum-{i}"' for i in range(200))

    await _deliver(
        client,
        model_call(1, tool_calls=1, arguments=f'{{"rooms":[{names}]}}'),
        model_call(2, content=ANSWER),
        turn_ended(),
    )

    (row,) = rows()
    (tool,) = row["tool_trace"]["calls"][0]["tools"]
    assert len(tool["arguments"]) == 300
    assert tool["arguments"].startswith('{"rooms":["Raum-0"')


async def test_an_event_run_carries_the_calls_its_api_turn_reported(
    consumer: Consumer,
    receiver: Receiver,
    publish: Publish,
    rows: Rows,
    respx_mock: respx.MockRouter,
) -> None:
    """The trigger starts the run through the Runs API; the same turn reports its
    calls through the hook, under the session the run was given."""
    fake_hermes(respx_mock)
    episode_consumer, _ = consumer
    client, metrics = receiver
    # Seven tools over three calls, as the session record of `run_1` counts them.
    statuses = await _deliver(
        client,
        _api_call(1, tool_calls=4),
        _api_call(2, tool_calls=3),
        _api_call(3, content=EXPLANATION),
        _api_end(),
    )
    await publish("appeared", 2)
    await episode_consumer.run_once()

    assert statuses == [200, 200, 200, 200]
    (row,) = rows()
    assert row["use_case"] == "explain-episode"
    assert row["trigger"] == "event"
    # The count stays the session record's; the calls come from the hook.
    assert row["tool_trace"]["tool_count"] == 7
    assert [call["call"] for call in row["tool_trace"]["calls"]] == [1, 2, 3]
    assert [len(call["tools"]) for call in row["tool_trace"]["calls"]] == [4, 3, 0]
    assert _hook_events(metrics, "traced") == 1.0


async def test_an_event_run_whose_calls_never_came_is_recorded_without_them(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    fake_hermes(respx_mock)
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed"
    assert row["tool_trace"] == {"tool_count": 7}


async def test_calls_that_come_in_while_the_run_waits_are_kept(
    consumer: Consumer,
    receiver: Receiver,
    publish: Publish,
    rows: Rows,
    respx_mock: respx.MockRouter,
) -> None:
    """The Runs API can call the run over before the hook has sent its turn's end:
    the event path waits for it, and the calls that come in meanwhile count."""
    episode_consumer, _ = consumer
    client, _ = receiver
    late: list[asyncio.Task[list[int]]] = []

    async def deliver_later() -> list[int]:
        await asyncio.sleep(0.05)
        return await _deliver(client, _api_call(1, tool_calls=7), _api_call(2), _api_end())

    def completed(request: httpx.Request) -> httpx.Response:
        late.append(asyncio.create_task(deliver_later()))
        return httpx.Response(200, json=COMPLETED)

    respx_mock.post(f"{HERMES_URL}/v1/runs").mock(side_effect=start_response)
    respx_mock.get(f"{HERMES_URL}/v1/runs/run_1").mock(side_effect=completed)
    respx_mock.get(f"{HERMES_URL}/api/sessions/sess_1").mock(
        return_value=httpx.Response(200, json=SESSION)
    )

    await publish("appeared", 2)
    await episode_consumer.run_once()

    assert await late[0] == [200, 200, 200]
    (row,) = rows()
    assert [call["call"] for call in row["tool_trace"]["calls"]] == [1, 2]


async def test_calls_of_a_turn_that_never_ended_are_counted_when_dropped(
    receiver: Receiver, rows: Rows, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness's own work after an answer reports calls but no turn end; they
    are spent and in no row, and the counter says so once the tally is given up."""
    client, metrics = receiver
    await _deliver(client, model_call(1, turn="0rphan01"), model_call(2, turn="0rphan01"))

    # Every tally is past its hour from here on.
    monkeypatch.setattr(turn_runs, "_TALLY_LIFETIME_SECONDS", -1.0)
    await _deliver(client, model_call(1, content=ANSWER), turn_ended())

    assert sample(metrics, "agent_trigger_orphaned_calls_total") == 2.0
    (row,) = rows()
    assert row["subject_key"] == f"{CHAT_SESSION}:{CHAT_TURN}"
