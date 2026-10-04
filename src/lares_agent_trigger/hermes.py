"""The harness's API server: the Runs API for event runs, the Jobs API for cron jobs.

The Runs API takes no toolset list, so what an API run may see is decided in
the harness configuration (`platform_toolsets.api_server`, the read server
only) and not here. It takes no skill field either, which is why the use
case's skill is named in the instructions.

`input` is the user message of the run, so it is a string or a list of
messages — the gateway reads a string as the message and takes `content` off
the last entry of a list, and anything else is a 400. The pointer therefore
travels as compact JSON inside that string.

What a run reports about itself is split across two endpoints, and the split
is the gateway's, not ours. The run record carries `run_id`, `status`,
`session_id`, `output`, `error`, a `usage` block of token counters, and
`runtime` — which is where the model that actually served the run and its
provider are, the top-level `model` being the gateway's own name. Its
`created_at` and `updated_at` are unix seconds, and their difference is the
run's real duration. Cost and the tool count live on the session record
(`/api/sessions/{id}`), whose payload sits under a `session` key.

A cron job's id is minted by the gateway, so what the job runs is read off its
name through the Jobs API (`/api/jobs/{id}`, wrapped under `job`). The same API
creates, updates, deletes and runs the managed jobs: a create takes the name,
the schedule, the prompt, the skills and the delivery and ignores everything
else, an update takes those through a whitelist, and a list leaves paused jobs
out unless asked for them. A job's `schedule` comes back parsed, the cron
expression under `expr`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

import httpx

from .config import Settings
from .cron_jobs import CronJob
from .ledger import ClosedStatus, Usage

# What the harness reports while a run is still going; anything else is final.
_PENDING = ("queued", "running", "in_progress")

# ISO 639-1 to the word an instruction line uses. A code without an entry is
# passed through, so a new language is a file change, not a code change.
_LANGUAGE_NAMES = {"de": "German", "en": "English"}


class HermesError(RuntimeError):
    """The harness refused a request or answered with something that is not a run.

    `status_code` is the HTTP status of a refusal, None when the answer came
    back 2xx but unusable.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class RunOutcome:
    """A finished run: what it produced, or why it did not.

    `status` is already the ledger's own word, so the caller writes it through
    rather than mapping the harness's vocabulary a second time.
    """

    harness_run_id: str
    status: ClosedStatus
    output: str | None
    error: str | None
    usage: Usage
    # The session the run was given; its hook deliveries carry the same id.
    session_id: str | None = None


@dataclass(frozen=True)
class HarnessJob:
    """A cron job as the Jobs API reports it, in the fields the reconcile manages."""

    id: str
    name: str
    # The cron expression; None for a job on another kind of schedule.
    schedule: str | None
    prompt: str
    skills: tuple[str, ...]
    deliver: str | None
    # False for a paused job.
    enabled: bool


def language_name(language: str) -> str:
    """The word an instruction line uses for an ISO 639-1 code."""
    return _LANGUAGE_NAMES.get(language, language)


def instructions_for(skill: str, language: str, tool_calls: int, minutes: int) -> str:
    """The instruction block an API run gets in place of the fields it has no room for."""
    return (
        f"Run the skill `{skill}`.\n"
        f"The subject is the episode named in the input; explain that one and nothing else.\n"
        f"Answer in {language_name(language)}. Open with the cause in one sentence.\n"
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
            raise HermesError(
                f"POST /v1/runs returned {response.status_code}: {response.text}",
                status_code=response.status_code,
            )
        run_id = response.json().get("run_id")
        if not run_id:
            raise HermesError("POST /v1/runs returned no run_id")
        return str(run_id)

    async def await_run(self, harness_run_id: str, *, deadline_seconds: float) -> RunOutcome:
        """Poll the run until it is terminal, or until its budget of minutes is spent."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        give_up_at = started + deadline_seconds
        while True:
            body = await self._get_run(harness_run_id)
            status = str(body.get("status", ""))
            elapsed = loop.time() - started
            if status not in _PENDING:
                usage = await self._usage_with_session(body, elapsed)
                return _outcome(harness_run_id, body, status, usage)
            if loop.time() >= give_up_at:
                return RunOutcome(
                    harness_run_id=harness_run_id,
                    status="failed",
                    output=None,
                    error=(
                        f"run did not finish within {deadline_seconds / 60:.0f} minutes"
                        f" (last status: {status or 'unknown'})"
                    ),
                    usage=_usage(body, elapsed),
                )
            await asyncio.sleep(self._settings.hermes_poll_seconds)

    async def _usage_with_session(self, body: dict[str, Any], elapsed: float) -> Usage:
        """Run usage, plus the cost and tool count only the session record holds.

        A session that cannot be read leaves those two fields unset rather than
        failing the run: the explanation is already written, and a NULL cost is
        honest where a guessed one would not be.
        """
        usage = _usage(body, elapsed)
        session_id = body.get("session_id")
        if not session_id:
            return usage
        try:
            response = await self._client.get(f"/api/sessions/{session_id}")
            payload = response.json() if response.status_code < 400 else {}
        except httpx.HTTPError, ValueError:
            return usage
        # The payload is wrapped: {"object": "hermes.session", "session": {...}}.
        session = payload.get("session") if isinstance(payload, dict) else None
        if not isinstance(session, dict):
            return usage
        # `actual_cost_usd` is what the provider billed; `estimated_cost_usd` is
        # the gateway's own reckoning and stands in until the bill is known.
        cost = session.get("actual_cost_usd") or session.get("estimated_cost_usd")
        return replace(
            usage,
            cost=Decimal(str(cost)) if cost is not None else None,
            tool_count=_int_or_none(session.get("tool_call_count")),
        )

    async def job_name(self, job_id: str) -> str | None:
        """The name of a cron job; None when the gateway holds no such job.

        A gateway that cannot answer raises: without the name a cron run
        belongs to no use case, so its delivery is refused and sent again.
        """
        try:
            payload = await self._send("GET", f"/api/jobs/{job_id}")
        except HermesError as exc:
            if exc.status_code == 404:
                return None
            raise
        job = payload.get("job")
        if not isinstance(job, dict):
            raise HermesError(f"GET /api/jobs/{job_id} returned no job")
        name = job.get("name")
        return str(name) if name else None

    async def list_jobs(self) -> list[HarnessJob]:
        """Every cron job the gateway holds, paused ones included."""
        payload = await self._send("GET", "/api/jobs", params={"include_disabled": "true"})
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise HermesError("GET /api/jobs returned no job list")
        return [_harness_job(job, "GET /api/jobs") for job in jobs]

    async def create_job(self, job: CronJob) -> str:
        """Create a job and return the id the gateway minted for it."""
        payload = await self._send("POST", "/api/jobs", json=job.model_dump(mode="json"))
        return _harness_job(payload.get("job"), "POST /api/jobs").id

    async def update_job(self, job_id: str, fields: dict[str, Any]) -> None:
        """Change these fields of a job and leave the rest of it as it is."""
        await self._send("PATCH", f"/api/jobs/{job_id}", json=fields)

    async def delete_job(self, job_id: str) -> None:
        """Delete a job; one the gateway no longer holds is already where it should be."""
        try:
            await self._send("DELETE", f"/api/jobs/{job_id}")
        except HermesError as exc:
            if exc.status_code != 404:
                raise

    async def run_job(self, job_id: str, prompt: str | None = None) -> None:
        """Have the gateway run a job on its next tick, outside its schedule.

        `prompt` is added to the job's own prompt for that one run; the job keeps its own.
        """
        body = {"prompt": prompt} if prompt is not None else None
        await self._send("POST", f"/api/jobs/{job_id}/run", json=body)

    async def _send(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """One request to the API server; a refusal or a body that is no object raises."""
        response = await self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise HermesError(
                f"{method} {path} returned {response.status_code}: {response.text}",
                status_code=response.status_code,
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise HermesError(f"{method} {path} returned {type(payload).__name__}")
        return payload

    async def _get_run(self, harness_run_id: str) -> dict[str, Any]:
        return await self._send("GET", f"/v1/runs/{harness_run_id}")


def _harness_job(record: Any, request: str) -> HarnessJob:
    if not isinstance(record, dict) or not record.get("id") or not record.get("name"):
        raise HermesError(f"{request} returned a job without an id or a name")
    schedule = record.get("schedule")
    expression = schedule.get("expr") if isinstance(schedule, dict) else None
    skills = record.get("skills")
    deliver = record.get("deliver")
    return HarnessJob(
        id=str(record["id"]),
        name=str(record["name"]),
        schedule=str(expression) if expression is not None else None,
        prompt=str(record.get("prompt") or ""),
        skills=tuple(str(skill) for skill in skills) if isinstance(skills, list) else (),
        deliver=str(deliver) if deliver is not None else None,
        enabled=record.get("enabled") is not False,
    )


def _outcome(harness_run_id: str, body: dict[str, Any], status: str, usage: Usage) -> RunOutcome:
    output = body.get("output")
    error = body.get("error")
    if status != "completed" and not error:
        error = f"harness ended the run as {status}"
    session_id = body.get("session_id")
    return RunOutcome(
        harness_run_id=harness_run_id,
        status="completed" if status == "completed" else "failed",
        output=str(output) if output is not None else None,
        error=str(error) if error is not None else None,
        usage=usage,
        session_id=str(session_id) if session_id else None,
    )


def _usage(body: dict[str, Any], elapsed: float) -> Usage:
    """What the run record reports about itself.

    The model and its provider come from `runtime`: the top-level `model` is
    the gateway's own name (`hermes-agent`) and says nothing about who served
    the run. Duration is the record's own `updated_at - created_at`; the
    elapsed time we measured stands in only when the record carries neither,
    because a run can finish inside the POST and leave our clock at zero.
    """
    usage = body.get("usage") or {}
    runtime = body.get("runtime") or {}
    created_at, updated_at = body.get("created_at"), body.get("updated_at")
    reported = (
        float(updated_at) - float(created_at)
        if isinstance(created_at, int | float) and isinstance(updated_at, int | float)
        else None
    )
    return Usage(
        model_source=_str_or_none(runtime.get("provider")),
        model=_str_or_none(runtime.get("model")),
        tokens_in=_int_or_none(usage.get("input_tokens")),
        tokens_out=_int_or_none(usage.get("output_tokens")),
        duration_seconds=reported if reported is not None else elapsed,
    )


def _str_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def _int_or_none(value: Any) -> int | None:
    return int(value) if value is not None else None
