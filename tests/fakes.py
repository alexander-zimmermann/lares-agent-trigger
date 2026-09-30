"""The fakes at the trigger's outer edge: the harness, Alertmanager and Discord, over respx.

Each fake keeps the rule of the real service it stands in for, so a test can
never pass against a fake that is laxer than the live side.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

import httpx
import respx
from prometheus_client import CollectorRegistry

from lares_agent_trigger.metrics import Metrics

from .conftest import ALERTMANAGER_URL, DISCORD_CHANNEL, DISCORD_TOKEN, HERMES_URL

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
