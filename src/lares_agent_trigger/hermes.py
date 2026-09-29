"""The harness's Runs API: start a run, poll it to its terminal state.

The Runs API takes no toolset list, so what an API run may see is decided in
the harness configuration (`platform_toolsets.api_server`, the read server
only) and not here. It takes no skill field either, which is why the use
case's skill is named in the instructions.

`input` is the user message of the run, so it is a string or a list of
messages — the gateway reads a string as the message and takes `content` off
the last entry of a list, and anything else is a 400. The pointer therefore
travels as compact JSON inside that string.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import httpx

from .config import Settings
from .ledger import Usage

# What the harness reports while a run is still going; anything else is final.
_PENDING = ("queued", "running", "in_progress")

# ISO 639-1 to the word an instruction line uses. A code without an entry is
# passed through, so a new language is a file change, not a code change.
_LANGUAGE_NAMES = {"de": "German", "en": "English"}


class HermesError(RuntimeError):
    """The harness refused a request or never reached a terminal state."""


@dataclass(frozen=True)
class RunOutcome:
    """A finished run: what it produced, or why it did not.

    `status` is already the ledger's own word, so the caller writes it through
    rather than mapping the harness's vocabulary a second time.
    """

    harness_run_id: str
    status: Literal["completed", "failed"]
    output: str | None
    error: str | None
    usage: Usage


def instructions_for(skill: str, language: str, tool_calls: int, minutes: int) -> str:
    """The instruction block an API run gets in place of the fields it has no room for."""
    spoken = _LANGUAGE_NAMES.get(language, language)
    return (
        f"Run the skill `{skill}`.\n"
        f"The subject is the episode named in the input; explain that one and nothing else.\n"
        f"Answer in {spoken}. Open with the cause in one sentence.\n"
        f"Stay within {tool_calls} tool calls and {minutes} minutes."
    )


class HermesClient:
    """One HTTP client against the gateway's API server."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=settings.hermes_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.hermes_api_key}"},
            timeout=settings.hermes_request_timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def start_run(
        self,
        *,
        idempotency_key: str,
        run_input: dict[str, Any],
        instructions: str,
        model: str | None = None,
    ) -> str:
        """Create a run and return the harness's id for it.

        The idempotency key is the ledger key, so a retried POST can never
        produce a second run behind one ledger row. A use case that pins a
        model routes this one request to it; without a pin the gateway's own
        default and its fallback chain decide.
        """
        body: dict[str, Any] = {
            "input": json.dumps(run_input, ensure_ascii=False, separators=(",", ":")),
            "instructions": instructions,
        }
        if model is not None:
            body["model"] = model
        response = await self._client.post(
            "/v1/runs",
            json=body,
            headers={"Idempotency-Key": idempotency_key},
        )
        if response.status_code >= 400:
            raise HermesError(f"POST /v1/runs returned {response.status_code}: {response.text}")
        run_id = response.json().get("id")
        if not run_id:
            raise HermesError("POST /v1/runs returned no run id")
        return str(run_id)

    async def await_run(self, harness_run_id: str, *, deadline_seconds: float) -> RunOutcome:
        """Poll the run until it is terminal, or until its budget of minutes is spent."""
        loop = asyncio.get_running_loop()
        give_up_at = loop.time() + deadline_seconds
        while True:
            body = await self._get_run(harness_run_id)
            status = str(body.get("status", ""))
            if status not in _PENDING:
                return _outcome(harness_run_id, body, status)
            if loop.time() >= give_up_at:
                return RunOutcome(
                    harness_run_id=harness_run_id,
                    status="failed",
                    output=None,
                    error=(
                        f"run did not finish within {deadline_seconds / 60:.0f} minutes"
                        f" (last status: {status or 'unknown'})"
                    ),
                    usage=_usage(body),
                )
            await asyncio.sleep(self._settings.hermes_poll_seconds)

    async def _get_run(self, harness_run_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/v1/runs/{harness_run_id}")
        if response.status_code >= 400:
            raise HermesError(
                f"GET /v1/runs/{harness_run_id} returned {response.status_code}: {response.text}"
            )
        body = response.json()
        if not isinstance(body, dict):
            raise HermesError(f"GET /v1/runs/{harness_run_id} returned {type(body).__name__}")
        return body


def _outcome(harness_run_id: str, body: dict[str, Any], status: str) -> RunOutcome:
    output = body.get("output")
    error = body.get("error")
    if status != "completed" and not error:
        error = f"harness ended the run as {status}"
    return RunOutcome(
        harness_run_id=harness_run_id,
        status="completed" if status == "completed" else "failed",
        output=str(output) if output is not None else None,
        error=str(error) if error is not None else None,
        usage=_usage(body),
    )


def _usage(body: dict[str, Any]) -> Usage:
    """Read the usage block; every field is optional, and absent means unknown.

    Verified on the image: `usage` carries input/output/reasoning tokens,
    api_calls and cost_usd, beside `duration_seconds`, `tool_count` and
    `model` on the run itself. `provider` is taken when the harness sends it —
    without it the ledger's `model_source` stays NULL rather than guessing.
    """
    usage = body.get("usage") or {}
    cost = usage.get("cost_usd")
    return Usage(
        model_source=_str_or_none(body.get("provider")),
        model=_str_or_none(body.get("model")),
        tokens_in=_int_or_none(usage.get("input_tokens")),
        tokens_out=_int_or_none(usage.get("output_tokens")),
        cost=Decimal(str(cost)) if cost is not None else None,
        duration_seconds=_float_or_none(body.get("duration_seconds")),
        tool_count=_int_or_none(body.get("tool_count")),
    )


def _str_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def _int_or_none(value: Any) -> int | None:
    return int(value) if value is not None else None


def _float_or_none(value: Any) -> float | None:
    return float(value) if value is not None else None
