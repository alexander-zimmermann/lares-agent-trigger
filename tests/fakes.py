"""The fakes at the trigger's outer edge: the harness, Alertmanager and Discord, over respx.

Each fake keeps the rule of the real service it stands in for, so a test can
never pass against a fake that is laxer than the live side.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import re
from collections.abc import Callable
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
    """The gateway's own rule on `input`, so a fake can never be laxer than it is.

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
    idempotency key is a new run, as it is on the gateway.
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


# A Discord turn as the gateway names it: the session is the conversation, the
# task id of a gateway turn is its session id, and the turn id appends a random
# tail — `agent/turn_context.py`.
CHAT_SESSION = "20260930_101500_a1b2c3"
CHAT_TURN = "4f9c2e1a"

ANSWER = (
    "Der Gefrierschrank läuft seit 17.8. auf Stufe 3.\n"
    "Leistung 92 W im Mittel über 24 h, sonst 61 W."
)

# A managed cron job: the gateway mints the 12-hex id itself, so the use case
# rides on the name. A cron run's task id is `cron:<job id>:<execution id>`
# and its session `cron_<job id>_<time>` — `cron/scheduler.py`.
JOB_ID = "a1b2c3d4e5f6"
EXECUTION_ID = "9e8d7c6b5a49"
CRON_SESSION = f"cron_{JOB_ID}_20260927_030000"
CRON_TASK = f"cron:{JOB_ID}:{EXECUTION_ID}"


def _hook_body(event: str, session_id: str, extra: dict[str, Any]) -> bytes:
    """A delivery as `agent/outbound_webhooks.py` renders it.

    `session_id` is promoted to the top level and every other keyword of the
    hook call lands under `extra` (`_payload_fields` in `agent/shell_hooks.py`),
    serialised with `default=str`.
    """
    payload = {
        "hook_event_name": event,
        "profile": "default",
        "tool_name": None,
        "tool_input": None,
        "session_id": session_id,
        "cwd": "/opt/data",
        "extra": extra,
        "delivery_id": "0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e",
        "timestamp": "2026-09-30T10:15:42.123456Z",
    }
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def model_call(
    number: int,
    *,
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
    # The gateway's `ToolCall` dataclass through `asdict`: flat, `function` is
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
    )


def turn_ended(
    *,
    session_id: str = CHAT_SESSION,
    platform: str = "discord",
    task_id: str | None = None,
    turn: str = CHAT_TURN,
    completed: bool = True,
    failed: bool = False,
    exit_reason: str = "text_response(stop)",
    model: str = "gpt-6-sol",
    event: str = "on_session_end",
) -> bytes:
    """One `on_session_end` delivery — `agent/turn_finalizer.py`, once per turn."""
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
    """The Jobs API for one job; `name=None` is a job the gateway no longer has."""
    route = respx_mock.get(f"{HERMES_URL}/api/jobs/{job_id}")
    if name is None:
        return route.mock(return_value=httpx.Response(404, json={"error": "Job not found"}))
    return route.mock(return_value=httpx.Response(200, json={"job": {"id": job_id, "name": name}}))


class FakeJobs:
    """The gateway's Jobs API over a job list it keeps, with the gateway's own rules.

    `gateway/platforms/api_server.py` and `cron/jobs.py`: the gateway mints a
    12-hex id and ignores one sent along; a create takes name, schedule,
    prompt, skills and deliver and nothing else; a PATCH takes only its
    whitelist and refuses a body without one of them; a name over 200
    characters, a prompt over 5000 or a schedule that is no cron expression
    is a 400; a run resumes a paused job; a list hides paused jobs unless
    `include_disabled` is asked for; an id that is not 12 hex is a 400, a
    missing job a 404. Every create, update, delete and run is kept in
    `changes` as (method, job id), so a test sees what was touched. The
    gateway's scan of a prompt for injections is not kept: the prompts here
    are the generator's own.
    """

    _CREATE_FIELDS = ("name", "schedule", "prompt", "skills", "deliver")
    # `_UPDATE_ALLOWED_FIELDS` of the gateway.
    _UPDATE_FIELDS = frozenset(
        {"name", "schedule", "prompt", "deliver", "skills", "skill", "repeat", "enabled"}
    )

    def __init__(self, respx_mock: respx.MockRouter) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.changes: list[tuple[str, str]] = []
        # How many lists answer 503 before the gateway is up.
        self.unavailable = 0
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
        """A job already in the harness; its id, as the gateway minted it."""
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

    @staticmethod
    def _invalid(fields: dict[str, Any]) -> httpx.Response | None:
        """The gateway's 400 for a name, prompt or schedule it does not take."""
        if len(fields.get("name") or "") > 200:
            return httpx.Response(400, json={"error": "Name must be ≤ 200 characters"})
        if len(fields.get("prompt") or "") > 5000:
            return httpx.Response(400, json={"error": "Prompt must be ≤ 5000 characters"})
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

    def _run(self, _request: httpx.Request, job_id: str) -> httpx.Response:
        if refused := self._found(job_id):
            return refused
        job = self.jobs[job_id]
        job.update({"enabled": True, "state": "scheduled", "manual_run_at": "2026-10-02T14:20:05"})
        self.changes.append(("RUN", job_id))
        return httpx.Response(200, json={"job": job})


# The house wiki as the wiki_page delivery reaches it.
WIKI_URL = "http://wiki-js.test"
WIKI_TOKEN = "eyJhbGciOiJSUzI1NiJ9.write.token"


def wiki_listed(*pages: dict[str, Any]) -> httpx.Response:
    """`pages.list` as Wiki.js answers it."""
    return httpx.Response(200, json={"data": {"pages": {"list": list(pages)}}})


def wiki_answer(
    listed: httpx.Response, action: str, path: str, page_id: int, *, refused: str | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    """Wiki.js answering the lookup with `listed` and the write as Wiki.js 2.5 does.

    A write answers with the raw page row `createPage`/`updatePage` return: it
    has `localeCode`, never `locale`, so selecting a field the row lacks fails
    the answer after the write went through.
    """
    row = {"id": page_id, "path": path, "title": "Wartungsplan", "localeCode": "de"}

    def answer(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if "list(" in query:
            return listed
        if refused is not None:
            status = {"succeeded": False, "slug": "PageUpdateForbidden", "message": refused}
            result = {"responseResult": status, "page": None}
            return httpx.Response(200, json={"data": {"pages": {action: result}}})
        selection = re.search(r"page \{([^}]*)\}", query)
        assert selection is not None
        fields = selection.group(1).split()
        status = {"succeeded": True, "slug": "ok", "message": "ok"}
        missing = [field for field in fields if field not in row]
        if missing:
            errors = [
                {"message": f"Cannot return null for non-nullable field Page.{field}."}
                for field in missing
            ]
            result = {"responseResult": status, "page": None}
            return httpx.Response(200, json={"errors": errors, "data": {"pages": {action: result}}})
        page = {field: row[field] for field in fields}
        result = {"responseResult": status, "page": page}
        return httpx.Response(200, json={"data": {"pages": {action: result}}})

    return answer
