"""The trigger's own API end to end: a keyed request in, a run, a job or a note out.

The bridge forwards its `start_run` tool here with the shared key; the
request goes in over HTTP to the app in process, the rows land in the real
ledger, and the harness is the fake of `fakes.py`.
"""

from __future__ import annotations

import asyncio
import email
import json
import re
from collections.abc import Callable
from email.policy import default as default_policy
from typing import Any

import httpx
import pytest
import respx

from lares_agent_trigger.memory import LIMIT_BYTES
from lares_agent_trigger.metrics import Metrics

from .conftest import API_KEY, Relay, Service
from .fakes import (
    COMPLETED,
    EXECUTION_ID,
    EXPLANATION,
    FakeJobs,
    fake_alertmanager,
    fake_hermes,
    model_call,
    sample,
    sign,
    turn_ended,
)

Rows = Callable[[], list[dict[str, Any]]]
Api = tuple[httpx.AsyncClient, Metrics]
Execute = Callable[..., None]
Memory = Callable[[], dict[str, str]]

RUNS = "/api/runs"
MEMORY = "/api/memory"
KEY = {"Authorization": f"Bearer {API_KEY}"}

# The fakes register routes a refused request never reaches.
pytestmark = pytest.mark.respx(assert_all_called=False)


async def _settled(rows: Rows) -> dict[str, Any]:
    """The one row, once the run started in the background has closed it."""
    for _ in range(500):
        (row,) = rows()
        if row["status"] not in ("queued", "running"):
            return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"the run never closed its row: {rows()}")


def _api_requests(metrics: Metrics, route: str, code: int) -> float:
    return sample(metrics, "agent_trigger_api_requests_total", route=route, code=str(code))


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": f"Bearer {'b' * 32}"}, {"Authorization": f"Basic {API_KEY}"}],
    ids=["without", "wrong", "not-bearer"],
)
@pytest.mark.parametrize("path", [RUNS, MEMORY])
async def test_a_request_without_the_key_is_refused(
    api: Api, rows: Rows, memory: Memory, headers: dict[str, str], path: str
) -> None:
    client, metrics = api

    response = await client.post(
        path, json={"use_case": "explain-episode", "subject": "15510"}, headers=headers
    )

    assert response.status_code == 401
    assert rows() == []
    assert memory() == {}
    assert _api_requests(metrics, path.removeprefix("/api/"), 401) == 1.0


async def test_starting_an_explanation_runs_it_on_that_episode(
    api: Api,
    rows: Rows,
    episode: int,
    relay: Relay,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    started = fake_hermes(respx_mock)
    client, metrics = api

    response = await client.post(
        RUNS, json={"use_case": "explain-episode", "subject": str(episode)}, headers=KEY
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    # Where the answer will arrive, so the chat can say so.
    assert body["output"] == ["stored", "discord", "mail"]
    row = await _settled(rows)
    assert row["id"] == body["run_id"]
    assert row["trigger"] == "message"
    assert row["subject_kind"] == "episode"
    # The episode, then when it was asked for: every request is a run of its own.
    assert re.fullmatch(r"15510:message:\d{8}T\d{6}Z", row["subject_key"])
    assert body["subject_key"] == row["subject_key"]
    assert row["status"] == "completed"
    assert row["text"] == EXPLANATION

    request = started.calls.last.request
    assert request.headers["Idempotency-Key"] == f"explain-episode/episode/{row['subject_key']}"
    pointer = json.loads(json.loads(request.content)["input"])
    assert pointer["episode_id"] == 15510
    assert pointer["fault"] == "appliance_runtime"
    assert pointer["subject"] == "2/1/197"
    # Delivered like any explanation: the message and the mail.
    assert discord.call_count == 1
    (mail,) = relay.messages
    assert isinstance(mail.content, bytes)
    subject = email.message_from_bytes(mail.content, policy=default_policy)["Subject"]
    assert subject.endswith("· 2/1/197")
    assert _api_requests(metrics, "runs", 202) == 1.0


async def test_asking_again_later_starts_another_run(
    api: Api, rows: Rows, episode: int, relay: Relay, respx_mock: respx.MockRouter
) -> None:
    """A second request is not a redelivery: the skill says what got worse since."""
    fake_hermes(respx_mock, runs=[[COMPLETED], [COMPLETED]])
    client, _ = api
    request = {"use_case": "explain-episode", "subject": str(episode)}

    first = await client.post(RUNS, json=request, headers=KEY)
    await _settled(rows)
    await asyncio.sleep(1.0)
    second = await client.post(RUNS, json=request, headers=KEY)

    assert first.status_code == second.status_code == 202
    assert first.json()["run_id"] != second.json()["run_id"]


async def test_a_request_while_its_run_is_open_is_that_run(
    api: Api, rows: Rows, episode: int, relay: Relay, respx_mock: respx.MockRouter
) -> None:
    """A tool call the model sent twice at once is one request, and says which run it is."""
    fake_hermes(respx_mock)
    client, _ = api
    request = {"use_case": "explain-episode", "subject": episode}

    answers = await asyncio.gather(
        *[client.post(RUNS, json=request, headers=KEY) for _ in range(2)]
    )

    started, refused = sorted(answers, key=lambda answer: answer.status_code)
    assert (started.status_code, refused.status_code) == (202, 409)
    assert refused.json()["run_id"] == started.json()["run_id"]
    assert "already running" in refused.json()["error"]
    await _settled(rows)


async def test_a_request_past_the_days_budget_is_a_capped_row(
    api: Api, rows: Rows, episode: int, execute: Execute, respx_mock: respx.MockRouter
) -> None:
    started = fake_hermes(respx_mock)
    for number in range(10):
        execute(
            "INSERT INTO agent_runs (use_case, trigger, subject_kind, subject_key, status)"
            " VALUES ('explain-episode', 'event', 'episode', %s, 'completed')",
            f"{14000 + number}:appeared",
        )
    client, _ = api

    response = await client.post(
        RUNS, json={"use_case": "explain-episode", "subject": "15510"}, headers=KEY
    )

    assert response.status_code == 429
    assert "10 runs" in response.json()["error"]
    capped = [row for row in rows() if row["trigger"] == "message"]
    assert [row["status"] for row in capped] == ["capped"]
    assert capped[0]["id"] == response.json()["run_id"]
    assert started.call_count == 0


@pytest.mark.parametrize(
    ("request_body", "code", "error"),
    [
        ({"use_case": "retired", "subject": "15510"}, 404, "no use case named retired"),
        ({"use_case": "summarise-week"}, 409, "summarise-week is dormant: Waits for its skill."),
        ({"use_case": "messenger"}, 400, "messenger is the chat itself"),
        ({"use_case": "explain-episode"}, 400, "name the episode id as the subject"),
        ({"use_case": "explain-episode", "subject": "Waschmaschine"}, 400, "no episode id"),
        ({"use_case": "explain-episode", "subject": "99999"}, 404, "no episode 99999"),
        ({"use_case": "propose-faults", "subject": "15510"}, 400, "takes no subject"),
        ({"subject": "15510"}, 400, "use_case"),
        (["explain-episode"], 400, "a JSON object"),
    ],
)
async def test_what_cannot_be_started_is_refused_with_the_reason(
    api: Api,
    rows: Rows,
    episode: int,
    respx_mock: respx.MockRouter,
    request_body: Any,
    code: int,
    error: str,
) -> None:
    started = fake_hermes(respx_mock)
    jobs = FakeJobs(respx_mock)
    client, _ = api

    response = await client.post(RUNS, json=request_body, headers=KEY)

    assert response.status_code == code
    assert error in response.json()["error"]
    assert rows() == []
    assert started.call_count == 0
    assert jobs.changes == []


async def test_a_schedule_use_case_runs_its_job_now_and_its_row_says_so(
    api: Api, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    jobs = FakeJobs(respx_mock)
    job_id = jobs.add("lares:propose-faults")
    client, _ = api

    response = await client.post(RUNS, json={"use_case": "propose-faults"}, headers=KEY)

    assert response.status_code == 202
    assert response.json() == {
        "use_case": "propose-faults",
        "job_id": job_id,
        "status": "requested",
        "output": ["stored"],
    }
    assert jobs.changes == [("RUN", job_id)]

    # The harness runs it on its next tick and reports the turn like any cron run.
    async def cron_turn(execution: str) -> None:
        task = f"cron:{job_id}:{execution}"
        session = f"cron_{job_id}_20261002_142100"
        for body in (
            model_call(1, session_id=session, platform="cron", task_id=task, content="Nichts."),
            turn_ended(session_id=session, platform="cron", task_id=task),
        ):
            assert (await client.post("/hooks/hermes", content=body, headers=sign(body))).is_success

    await cron_turn(EXECUTION_ID)
    # Its next run is its schedule's again.
    await cron_turn("1f2e3d4c5b6a")

    requested, scheduled = rows()
    assert (requested["use_case"], requested["trigger"]) == ("propose-faults", "message")
    assert requested["subject_key"] == f"{job_id}:{EXECUTION_ID}"
    assert scheduled["trigger"] == "schedule"


async def test_a_paused_job_is_not_resumed_by_running_it(
    api: Api, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    """Running a job now resumes it for good in the gateway; a person paused it on purpose."""
    jobs = FakeJobs(respx_mock)
    job_id = jobs.add("lares:propose-faults", enabled=False)
    client, _ = api

    response = await client.post(RUNS, json={"use_case": "propose-faults"}, headers=KEY)

    assert response.status_code == 409
    assert "paused" in response.json()["error"]
    assert jobs.changes == []
    assert jobs.jobs[job_id]["enabled"] is False


async def test_a_schedule_past_the_days_budget_is_not_run(
    api: Api, rows: Rows, execute: Execute, respx_mock: respx.MockRouter
) -> None:
    jobs = FakeJobs(respx_mock)
    jobs.add("lares:propose-faults")
    execute(
        "INSERT INTO agent_runs (use_case, trigger, subject_kind, subject_key, status)"
        " VALUES ('propose-faults', 'schedule', 'none', 'b0b000000001:9e8d7c6b5a49', 'completed')"
    )
    client, _ = api

    response = await client.post(RUNS, json={"use_case": "propose-faults"}, headers=KEY)

    assert response.status_code == 429
    assert "1 runs today" in response.json()["error"]
    assert jobs.changes == []


async def test_a_job_the_harness_does_not_hold_yet_is_unavailable(
    api: Api, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    jobs = FakeJobs(respx_mock)
    client, _ = api

    response = await client.post(RUNS, json={"use_case": "propose-faults"}, headers=KEY)

    assert response.status_code == 503
    assert "holds no job lares:propose-faults" in response.json()["error"]
    assert jobs.changes == []


async def test_a_harness_that_does_not_answer_is_unavailable(
    api: Api, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    jobs = FakeJobs(respx_mock)
    jobs.unavailable = 1
    client, metrics = api

    response = await client.post(RUNS, json={"use_case": "propose-faults"}, headers=KEY)

    assert response.status_code == 503
    assert _api_requests(metrics, "runs", 503) == 1.0


async def test_a_note_is_appended_to_the_use_cases_memory(api: Api, memory: Memory) -> None:
    client, _ = api

    first = await client.post(
        MEMORY,
        json={
            "use_case": "propose-faults",
            "text": "2026-09-27: channel_silence gap 5 → 6 rejected",
        },
        headers=KEY,
    )
    second = await client.post(
        MEMORY,
        json={"use_case": "propose-faults", "text": "  2026-10-04: nothing new\n"},
        headers=KEY,
    )

    assert first.status_code == second.status_code == 200
    text = memory()["propose-faults"]
    assert text == "2026-09-27: channel_silence gap 5 → 6 rejected\n2026-10-04: nothing new"
    assert second.json() == {"use_case": "propose-faults", "bytes": len(text.encode())}


async def test_memory_keeps_the_newest_lines_within_its_bound(
    api: Api, memory: Memory, execute: Execute
) -> None:
    """About 8 KB, oldest lines first, never half a line: the dashboard shows whole notes."""
    lines = [f"{number:03d} {'ü' * 60}" for number in range(100)]
    execute(
        "INSERT INTO agent_memory (use_case, text) VALUES ('propose-faults', %s)", "\n".join(lines)
    )
    client, _ = api

    response = await client.post(
        MEMORY, json={"use_case": "propose-faults", "text": "newest"}, headers=KEY
    )

    assert response.status_code == 200
    text = memory()["propose-faults"]
    kept = text.split("\n")
    assert kept[-1] == "newest"
    assert len(text.encode()) <= LIMIT_BYTES
    # Whole lines, cut from the front, and not one more than the bound needs.
    oldest = lines.index(kept[0])
    assert kept[:-1] == lines[oldest:]
    assert len(f"{lines[oldest - 1]}\n{text}".encode()) > LIMIT_BYTES


@pytest.mark.parametrize(
    ("request_body", "code", "error"),
    [
        ({"use_case": "explain-episode", "text": "x"}, 409, "explain-episode keeps no memory"),
        ({"use_case": "retired", "text": "x"}, 404, "no use case named retired"),
        ({"use_case": "propose-faults", "text": "  "}, 400, "text"),
        ({"use_case": "propose-faults"}, 400, "text"),
        ({"use_case": "propose-faults", "text": "x" * (LIMIT_BYTES + 1)}, 400, "more than"),
    ],
)
async def test_a_note_that_cannot_be_kept_is_refused(
    api: Api, memory: Memory, request_body: dict[str, Any], code: int, error: str
) -> None:
    client, _ = api

    response = await client.post(MEMORY, json=request_body, headers=KEY)

    assert response.status_code == code
    assert error in response.json()["error"]
    assert memory() == {}


async def test_a_request_a_stopped_pod_left_open_is_closed_and_reported(
    service: Service, rows: Rows, execute: Execute, respx_mock: respx.MockRouter
) -> None:
    """Nothing redelivers a request, so the next pod closes what the last one left running."""
    alerts = fake_alertmanager(respx_mock)
    execute(
        "INSERT INTO agent_runs (use_case, trigger, subject_kind, subject_key, status, language)"
        " VALUES ('explain-episode', 'message', 'episode', '15510:message:20261002T142005Z',"
        " 'running', 'de'), ('explain-episode', 'event', 'episode', '15510:appeared',"
        " 'running', 'de')"
    )

    await service.runs.close_abandoned()

    requested, event = rows()
    assert requested["status"] == "failed"
    assert "restarted" in requested["error"]
    assert requested["finished_at"] is not None
    # An event's row waits for the event's redelivery, which closes it the same way.
    assert event["status"] == "running"
    (alert,) = json.loads(alerts.calls.last.request.content)
    assert alert["labels"]["use_case"] == "explain-episode"
    assert "trigger_restarted" in alert["annotations"]["summary"]
    assert (
        sample(
            service.metrics,
            "agent_trigger_failures_total",
            use_case="explain-episode",
            **{"class": "trigger_restarted"},
        )
        == 1.0
    )
