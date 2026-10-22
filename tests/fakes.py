"""The fakes at the trigger's outer edge: the harness, Alertmanager and Discord, over respx.

Each fake keeps the rule of the real service it stands in for, so a test can
never pass against a fake that is laxer than the live side.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import respx
from prometheus_client import CollectorRegistry

from lares_agent_trigger.metrics import Metrics

from .conftest import ALERTMANAGER_URL, DISCORD_CHANNEL, DISCORD_TOKEN, HERMES_URL, HOOK_SECRET

# An answer in the shape the lares-explain skill asks for, as a live run wrote
# one: the cause, a blank line, the proof lines, the open point.
EXPLANATION = (
    "Die Waschmaschine hängt seit 14:20 im Spülgang.\n"
    "\n"
    "-# Subject: appliance_runtime auf 2/1/197, seit 25.09. 14:20, Stufe 2\n"
    "-# Channel: 49 mA, 3 min alt, Waschmaschine, Hauswirtschaftsraum\n"
    "-# History: seit 14:20 durchgehend 45 bis 52 mA, davor 0 mA\n"
    "-# Surroundings: niemand zu Hause seit 13:50, Präsenz aus\n"
    "\n"
    "Offen: Ob die Tür verriegelt ist, meldet kein Kanal."
)

DISCORD_MESSAGES = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL}/messages"

# A run record as the live harness returned one. `run_id`, never `id`; the
# top-level `model` is the harness's own name and `runtime` holds the model
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
# harness wraps it.
SESSION: dict[str, Any] = {
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


def start_response(request: httpx.Request, run_id: str = "run_1") -> httpx.Response:
    """The harness's own rule on `input`, so a fake can never be laxer than it is.

    `api_server_runs.py` reads a string as the user message and takes `content`
    off the last entry of a list; anything else leaves the message empty and is
    refused.
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
    return httpx.Response(200, json={"object": "hermes.run", "run_id": run_id, "status": "queued"})


def fake_hermes(
    respx_mock: respx.MockRouter,
    *,
    states: list[dict[str, Any]] | None = None,
    runs: list[list[dict[str, Any]]] | None = None,
) -> Any:
    """A harness that accepts runs and reports each one's states in turn.

    `states` is the one run of the common case; `runs` gives every start its
    own run, `run_1`, `run_2`, … in the order the starts arrive — a new
    idempotency key is a new run, as it is on the harness.
    """
    per_run = runs or [states or [COMPLETED]]
    numbers = iter(range(1, len(per_run) + 1))

    def start(request: httpx.Request) -> httpx.Response:
        return start_response(request, run_id=f"run_{next(numbers)}")

    started = respx_mock.post(f"{HERMES_URL}/v1/runs").mock(side_effect=start)
    for number, run_states in enumerate(per_run, start=1):
        respx_mock.get(f"{HERMES_URL}/v1/runs/run_{number}").mock(
            side_effect=[
                httpx.Response(200, json={**state, "run_id": f"run_{number}"})
                for state in run_states
            ]
        )
    respx_mock.get(f"{HERMES_URL}/api/sessions/sess_1").mock(
        return_value=httpx.Response(200, json=SESSION)
    )
    return started


def _alertmanager_response(request: httpx.Request) -> httpx.Response:
    """Alertmanager's rule on `POST /api/v2/alerts`: a list of alerts, each with labels.

    It answers 400 to anything else, and 200 with an empty body to a valid one.
    """
    body = json.loads(request.content)
    valid = (
        isinstance(body, list)
        and bool(body)
        and all(isinstance(alert, dict) and alert.get("labels") for alert in body)
    )
    if not valid:
        return httpx.Response(400, json={"code": 400, "message": "invalid alerts"})
    return httpx.Response(200)


def fake_alertmanager(respx_mock: respx.MockRouter, *, status: int | None = None) -> Any:
    """Alertmanager's alerts API; `status` forces an answer, for an Alertmanager that is down."""
    route = respx_mock.post(f"{ALERTMANAGER_URL}/api/v2/alerts")
    if status is not None:
        return route.mock(return_value=httpx.Response(status))
    return route.mock(side_effect=_alertmanager_response)


def _discord_response(request: httpx.Request, ids: Any) -> httpx.Response:
    """Discord's rules on creating a message: a bot token, and 1 to 2000 characters of content."""
    if request.headers.get("Authorization") != f"Bot {DISCORD_TOKEN}":
        return httpx.Response(401, json={"message": "401: Unauthorized", "code": 0})
    content = json.loads(request.content).get("content") or ""
    if not content:
        return httpx.Response(400, json={"message": "Cannot send an empty message", "code": 50006})
    if len(content) > 2000:
        return httpx.Response(
            400,
            json={
                "message": "Invalid Form Body",
                "code": 50035,
                "errors": {
                    "content": {
                        "_errors": [
                            {
                                "code": "BASE_TYPE_MAX_LENGTH",
                                "message": "Must be 2000 or fewer in length.",
                            }
                        ]
                    }
                },
            },
        )
    return httpx.Response(
        200,
        json={"id": str(next(ids)), "channel_id": DISCORD_CHANNEL, "content": content, "type": 0},
    )


def fake_discord(respx_mock: respx.MockRouter) -> Any:
    """The home channel's create-message endpoint, answering with snowflake ids in order."""
    ids = itertools.count(1548300000000000001)
    return respx_mock.post(DISCORD_MESSAGES).mock(
        side_effect=lambda request: _discord_response(request, ids)
    )


def sample(metrics: Metrics, name: str, **labels: str) -> float:
    registry: CollectorRegistry = metrics.registry
    return registry.get_sample_value(name, labels) or 0.0


# A Discord turn as the harness names it: the session is the conversation, the
# task id of such a turn is its session id, and the turn id appends a random
# tail — `agent/turn_context.py`.
CHAT_SESSION = "20260930_101500_a1b2c3"
CHAT_TURN = "4f9c2e1a"

ANSWER = (
    "Der Gefrierschrank läuft seit 17.8. auf Stufe 3.\n"
    "Leistung 92 W im Mittel über 24 h, sonst 61 W."
)

# A background review after a chat answer: the harness forks it into the
# answer's session without a task id, so the fork's task is a fresh UUID and
# its turn id `<session>:<uuid>:<random>` — `agent/background_review.py`,
# `agent/turn_facade.py`. It never ends: the fork reports no `on_session_end`.
REVIEW_TASK = "7c1e9a52-3f0d-4b8e-9a61-2d5c8e0f4b13"
REVIEW_TURN = "d3b07384"
# What a review that found nothing worth keeping says last.
REVIEWED = "Nothing to save."


def review_call(number: int, **fields: Any) -> bytes:
    """One `post_api_request` of a background review in the chat session."""
    return model_call(number, task_id=REVIEW_TASK, turn=REVIEW_TURN, **fields)


def review_tool(call: str, **fields: Any) -> bytes:
    """One `post_tool_call` of a background review in the chat session."""
    return tool_call(call, task_id=REVIEW_TASK, turn=REVIEW_TURN, **fields)


# A managed cron job: the harness mints the 12-hex id itself, so the use case
# rides on the name. A cron run's task id is `cron:<job id>:<execution id>`
# and its session `cron_<job id>_<time>` — `cron/scheduler.py`.
JOB_ID = "a1b2c3d4e5f6"
EXECUTION_ID = "9e8d7c6b5a49"
CRON_SESSION = f"cron_{JOB_ID}_20260927_030000"
CRON_TASK = f"cron:{JOB_ID}:{EXECUTION_ID}"


def _hook_body(
    event: str,
    session_id: str,
    extra: dict[str, Any],
    *,
    fired_at: float,
    profile: str,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> bytes:
    """A delivery as `agent/outbound_webhooks.py` renders it.

    `session_id`, the tool's name and its arguments are promoted to the top
    level and every other keyword of the hook call lands under `extra`
    (`_payload_fields` in `agent/shell_hooks.py`), serialised with
    `default=str`. `timestamp` is when the hook fired, in UTC.
    """
    payload = {
        "hook_event_name": event,
        "profile": profile,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": session_id,
        "cwd": "/opt/data",
        "extra": extra,
        "delivery_id": "0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e",
        "timestamp": datetime.fromtimestamp(fired_at, UTC).isoformat().replace("+00:00", "Z"),
    }
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def model_call(
    number: int,
    *,
    profile: str = "default",
    session_id: str = CHAT_SESSION,
    platform: str = "discord",
    task_id: str | None = None,
    turn: str = CHAT_TURN,
    input_tokens: int = 40000,
    cache_read_tokens: int = 2000,
    output_tokens: int = 300,
    usage: bool = True,
    tool_calls: int = 0,
    arguments: str = '{"state":"open","days":7}',
    content: str | None = None,
    started_at: float = 1790756100.0,
    seconds: float = 4.0,
    model: str = "gpt-6-sol",
    provider: str = "openai-codex",
) -> bytes:
    """One `post_api_request` delivery — `agent/turn_response_intake.py`.

    `usage` is `normalize_usage` as a dict plus `prompt_tokens` (input with the
    cache) and `total_tokens` (`agent/api_request_hooks.py`); a response with
    no usage sends null. The reply comes twice: sanitised under `response`, and
    as the raw object, which `default=str` turns into its repr.
    """
    task = task_id if task_id is not None else session_id
    summary = (
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": 0,
            "reasoning_tokens": 120,
            "request_count": 1,
            "prompt_tokens": input_tokens + cache_read_tokens,
            "total_tokens": input_tokens + cache_read_tokens + output_tokens,
        }
        if usage
        else None
    )
    # The harness's `ToolCall` dataclass through `asdict`: flat, `function` is
    # only a property on it — `agent/transports/types.py`.
    calls = [
        {
            "id": f"call_{number}_{i}",
            "name": "list_episodes",
            "arguments": arguments,
            "provider_data": {"call_id": f"call_{number}_{i}", "response_item_id": f"fc_{i}"},
        }
        for i in range(tool_calls)
    ]
    finish_reason = "tool_calls" if tool_calls else "stop"
    return _hook_body(
        "post_api_request",
        session_id,
        {
            "task_id": task,
            "turn_id": f"{session_id}:{task}:{turn}",
            "api_request_id": f"req_{turn}_{number}",
            "platform": platform,
            "model": model,
            "provider": provider,
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_mode": "codex_responses",
            "api_call_count": number,
            "api_duration": seconds,
            "started_at": started_at,
            "ended_at": started_at + seconds,
            "first_chunk_at": started_at + 0.8,
            "finish_reason": finish_reason,
            "message_count": 1 + 2 * number,
            "response_model": model,
            "response": {
                "model": model,
                "finish_reason": finish_reason,
                "assistant_message": {"role": "assistant", "content": content, "tool_calls": calls},
                "usage": summary,
            },
            "usage": summary,
            "assistant_message": f"SimpleNamespace(role='assistant', content={content!r})",
            "assistant_content_chars": len(content or ""),
            "assistant_tool_call_count": tool_calls,
            "moa_references": None,
        },
        fired_at=started_at + seconds,
        profile=profile,
    )


def turn_ended(
    *,
    profile: str = "default",
    session_id: str = CHAT_SESSION,
    platform: str = "discord",
    task_id: str | None = None,
    turn: str = CHAT_TURN,
    completed: bool = True,
    failed: bool = False,
    exit_reason: str = "text_response(stop)",
    model: str = "gpt-6-sol",
    event: str = "on_session_end",
    at: float = 1790756112.0,
) -> bytes:
    """One `on_session_end` delivery — `agent/turn_finalizer.py`, once per turn, fired `at`."""
    task = task_id if task_id is not None else session_id
    return _hook_body(
        event,
        session_id,
        {
            "task_id": task,
            "turn_id": f"{session_id}:{task}:{turn}",
            "completed": completed,
            "failed": failed,
            "interrupted": False,
            "turn_exit_reason": exit_reason,
            "model": model,
            "platform": platform,
        },
        fired_at=at,
        profile=profile,
    )


# Captured from Hermes v0.22.4 as OpenAI refused the key; the request's prompt and tools cut.
FAILED_CALL: dict[str, Any] = json.loads(
    (Path(__file__).parent / "hooks" / "api_request_error.json").read_text(encoding="utf-8")
)
# What the person wrote, which the captured request carries.
FAILED_PROMPT = "Sag Hallo."


def failed_call(
    number: int,
    *,
    profile: str = "default",
    session_id: str = CHAT_SESSION,
    platform: str = "discord",
    task_id: str | None = None,
    turn: str = CHAT_TURN,
    model: str = "gpt-6-luna",
    provider: str = "openai-api",
    status_code: int | str | None = 401,
    reason: str = "auth",
    started_at: float = 1790756100.0,
    seconds: float = 0.7,
) -> bytes:
    """One `api_request_error` delivery — `agent/api_request_hooks.py`, once per failed attempt.

    The captured delivery, moved into this turn: everything else, the error in
    the source's words and the request with the person's message, stays as
    Hermes sent it. A retry keeps the call's number and its start.
    """
    task = task_id if task_id is not None else session_id
    turn_id = f"{session_id}:{task}:{turn}"
    payload = json.loads(json.dumps(FAILED_CALL))
    extra = payload["extra"]
    extra.update(
        {
            "task_id": task,
            "turn_id": turn_id,
            "api_request_id": f"{turn_id}:api:{number}",
            "platform": platform,
            "model": model,
            "provider": provider,
            "api_call_count": number,
            "api_duration": seconds,
            "started_at": started_at,
            "ended_at": started_at + seconds,
            "status_code": status_code,
            "reason": reason,
        }
    )
    payload.update(
        {
            "profile": profile,
            "session_id": session_id,
            "timestamp": datetime.fromtimestamp(started_at + seconds, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
        }
    )
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def tool_call(
    call: str,
    *,
    profile: str = "default",
    session_id: str = CHAT_SESSION,
    task_id: str | None = None,
    turn: str = CHAT_TURN,
    name: str = "list_episodes",
    arguments: dict[str, Any] | None = None,
    result: str = '{"episodes": [{"id": 15510, "fault": "appliance_runtime"}]}',
    ended_at: float = 1790756103.5,
    milliseconds: int = 420,
    status: str = "ok",
    error_type: str | None = None,
    error_message: str | None = None,
) -> bytes:
    """One `post_tool_call` delivery — `_emit_post_tool_call_hook` in `model_tools.py`.

    The tool's name and arguments sit at the top level; under `extra` the
    result as the model got it, the call's ids, its duration in whole
    milliseconds and how it ended (`ok`, `error`, `blocked`). No platform and
    no start: the hook fires as the tool returns, `ended_at`.
    """
    task = task_id if task_id is not None else session_id
    return _hook_body(
        "post_tool_call",
        session_id,
        {
            "result": result,
            "task_id": task,
            "tool_call_id": call,
            "turn_id": f"{session_id}:{task}:{turn}",
            "api_request_id": f"req_{turn}_1",
            "duration_ms": milliseconds,
            "status": status,
            "error_type": error_type,
            "error_message": error_message,
            "middleware_trace": [],
        },
        fired_at=ended_at,
        profile=profile,
        tool_name=name,
        tool_input=arguments if arguments is not None else {"state": "open", "days": 7},
    )


def sign(body: bytes, secret: str = HOOK_SECRET) -> dict[str, str]:
    """The headers Hermes sends with a signed delivery."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    try:
        event = str(json.loads(body)["hook_event_name"])
    except ValueError, KeyError, TypeError:
        event = "on_session_end"
    return {
        "Content-Type": "application/json",
        "X-Hermes-Event": event,
        "X-Hermes-Signature-256": f"sha256={digest}",
    }


def fake_job(respx_mock: respx.MockRouter, *, job_id: str = JOB_ID, name: str | None) -> Any:
    """The Jobs API for one job; `name=None` is a job the harness no longer has."""
    route = respx_mock.get(f"{HERMES_URL}/api/jobs/{job_id}")
    if name is None:
        return route.mock(return_value=httpx.Response(404, json={"error": "Job not found"}))
    return route.mock(return_value=httpx.Response(200, json={"job": {"id": job_id, "name": name}}))


class FakeJobs:
    """The harness's Jobs API over a job list it keeps, with the harness's own rules.

    `gateway/platforms/api_server.py` and `cron/jobs.py`: the harness mints a
    12-hex id and ignores one sent along; a create takes name, schedule,
    prompt, skills and deliver and nothing else; a PATCH takes only its
    whitelist and refuses a body without one of them; a name over 200
    characters, a prompt over 5000 or a schedule that is no cron expression
    is a 400; a run resumes a paused job and takes an optional `prompt` for
    that one run, kept as `manual_run_prompt`; a list hides paused jobs unless
    `include_disabled` is asked for; an id that is not 12 hex is a 400, a
    missing job a 404. Every create, update, delete and run is kept in
    `changes` as (method, job id), so a test sees what was touched. The
    harness's scan of a prompt for injections is one phrase here, `blocked`,
    which a prompt holding it is refused for with the harness's wording.
    """

    _CREATE_FIELDS = ("name", "schedule", "prompt", "skills", "deliver")
    # `_UPDATE_ALLOWED_FIELDS` of the harness.
    _UPDATE_FIELDS = frozenset(
        {"name", "schedule", "prompt", "deliver", "skills", "skill", "repeat", "enabled"}
    )

    def __init__(self, respx_mock: respx.MockRouter) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.changes: list[tuple[str, str]] = []
        # How many lists answer 503 before the harness is up.
        self.unavailable = 0
        # A phrase the harness's scan blocks in a prompt.
        self.blocked: str | None = None
        self._ids = (f"{n:012x}" for n in itertools.count(0xB0B000000001))
        base = f"{HERMES_URL}/api/jobs"
        one = rf"^{HERMES_URL}/api/jobs/(?P<job_id>[^/]+)$"
        respx_mock.get(base).mock(side_effect=self._list)
        respx_mock.post(base).mock(side_effect=self._create)
        respx_mock.route(method="GET", url__regex=one).mock(side_effect=self._get)
        respx_mock.route(method="PATCH", url__regex=one).mock(side_effect=self._update)
        respx_mock.route(method="DELETE", url__regex=one).mock(side_effect=self._delete)
        respx_mock.route(method="POST", url__regex=rf"^{base}/(?P<job_id>[^/]+)/run$").mock(
            side_effect=self._run
        )

    def add(
        self,
        name: str,
        *,
        schedule: str = "0 3 * * 0",
        prompt: str = "Answer in English. Stay within 80 tool calls and 20 minutes.",
        skills: tuple[str, ...] = ("lares-propose",),
        deliver: str = "local",
        enabled: bool = True,
    ) -> str:
        """A job already in the harness; its id, as the harness minted it."""
        job_id = next(self._ids)
        self.jobs[job_id] = {
            "id": job_id,
            "name": name,
            "prompt": prompt,
            "skills": list(skills),
            "skill": skills[0] if skills else None,
            "schedule": self._schedule(schedule),
            "schedule_display": schedule,
            "enabled": enabled,
            "state": "scheduled" if enabled else "paused",
            "deliver": deliver,
            "enabled_toolsets": None,
            "model": None,
        }
        return job_id

    def named(self, name: str) -> list[dict[str, Any]]:
        return [job for job in self.jobs.values() if job["name"] == name]

    @staticmethod
    def _schedule(expr: str) -> dict[str, Any]:
        return {"kind": "cron", "expr": expr, "display": expr}

    def _invalid(self, fields: dict[str, Any]) -> httpx.Response | None:
        """The harness's 400 for a name, prompt or schedule it does not take."""
        if len(fields.get("name") or "") > 200:
            return httpx.Response(400, json={"error": "Name must be ≤ 200 characters"})
        if len(fields.get("prompt") or "") > 5000:
            return httpx.Response(400, json={"error": "Prompt must be ≤ 5000 characters"})
        if self.blocked is not None and self.blocked in (fields.get("prompt") or ""):
            return httpx.Response(
                400, json={"error": "Blocked: prompt matches threat pattern 'prompt_injection'."}
            )
        schedule = fields.get("schedule")
        if schedule is not None:
            parts = str(schedule).split()
            if len(parts) < 5 or not all(
                re.fullmatch(r"[A-Za-z\d\*\-,/]+", part) for part in parts[:5]
            ):
                return httpx.Response(400, json={"error": f"Invalid cron expression '{schedule}'"})
        return None

    def _list(self, request: httpx.Request) -> httpx.Response:
        if self.unavailable:
            self.unavailable -= 1
            return httpx.Response(503, json={"error": "Gateway is draining"})
        everything = request.url.params.get("include_disabled", "").lower() in {"true", "1"}
        jobs = [job for job in self.jobs.values() if everything or job["enabled"]]
        return httpx.Response(200, json={"jobs": jobs})

    def _create(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not (body.get("name") or "").strip():
            return httpx.Response(400, json={"error": "Name is required"})
        if not (body.get("schedule") or "").strip():
            return httpx.Response(400, json={"error": "Schedule is required"})
        fields = {key: body[key] for key in self._CREATE_FIELDS if key in body}
        if refused := self._invalid(fields):
            return refused
        job_id = self.add(
            fields["name"],
            schedule=fields["schedule"],
            prompt=fields.get("prompt", ""),
            skills=tuple(fields.get("skills") or ()),
            deliver=fields.get("deliver", "local"),
        )
        self.changes.append(("POST", job_id))
        return httpx.Response(200, json={"job": self.jobs[job_id]})

    def _found(self, job_id: str) -> httpx.Response | None:
        if not re.fullmatch(r"[a-f0-9]{12}", job_id):
            return httpx.Response(400, json={"error": "Invalid job ID format"})
        if job_id not in self.jobs:
            return httpx.Response(404, json={"error": "Job not found"})
        return None

    def _get(self, _request: httpx.Request, job_id: str) -> httpx.Response:
        return self._found(job_id) or httpx.Response(200, json={"job": self.jobs[job_id]})

    def _update(self, request: httpx.Request, job_id: str) -> httpx.Response:
        if refused := self._found(job_id):
            return refused
        body = json.loads(request.content)
        fields = {key: value for key, value in body.items() if key in self._UPDATE_FIELDS}
        if not fields:
            return httpx.Response(400, json={"error": "No valid fields to update"})
        if refused := self._invalid(fields):
            return refused
        job = self.jobs[job_id]
        if "schedule" in fields:
            fields["schedule"] = self._schedule(fields["schedule"])
            job["schedule_display"] = fields["schedule"]["display"]
        job.update(fields)
        self.changes.append(("PATCH", job_id))
        return httpx.Response(200, json={"job": job})

    def _delete(self, _request: httpx.Request, job_id: str) -> httpx.Response:
        if refused := self._found(job_id):
            return refused
        del self.jobs[job_id]
        self.changes.append(("DELETE", job_id))
        return httpx.Response(200, json={"ok": True})

    def _run(self, request: httpx.Request, job_id: str) -> httpx.Response:
        if refused := self._found(job_id):
            return refused
        body = json.loads(request.content) if request.content else None
        prompt = body.get("prompt") if isinstance(body, dict) else None
        if prompt is not None and (refused := self._invalid({"prompt": str(prompt)})):
            return refused
        job = self.jobs[job_id]
        job.update(
            {
                "enabled": True,
                "state": "scheduled",
                "manual_run_at": "2026-10-02T14:20:05",
                "manual_run_prompt": str(prompt) if prompt else None,
            }
        )
        self.changes.append(("RUN", job_id))
        return httpx.Response(200, json={"job": job})


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


async def until(condition: Callable[[], object], *, seconds: float = 2.0) -> None:
    """Wait for what a task in the background does, a moment at a time, within a bound."""
    for _ in range(round(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
