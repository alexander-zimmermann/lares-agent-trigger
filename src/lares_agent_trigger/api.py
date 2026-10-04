"""The trigger's own API: start a use case now, append to a use case's memory.

The bridge forwards its `start_run` tool here with the key sealed for both
pods as a Bearer token. Notes reach a use case's memory the same way, so the
trigger stays the only writer of `agent_memory`. The API sits on the
receiver's port, beside the hook.

`POST /api/runs` with `use_case` and, for a use case that runs on an episode,
the episode id as `subject`:

- an event use case runs on that episode through the Runs API, exactly as on
  an event, except that its row says `trigger = message` and its subject key
  carries when it was asked for. The answer comes once the row is claimed —
  202 with the run's id, 429 when the day's runs are spent, 409 with the
  run's id while one asked for on that episode is still open — and the output
  goes where the use case delivers;
- a schedule use case has its managed job run now (202 with the job's id), and
  its next cron turn is recorded as started by a person; 429 when the day's
  runs are spent, 409 when a person paused the job;
- the chat, a dormant use case, one that does not exist, or an episode the
  engine never recorded is refused with the reason, so the chat can say it.

With `output` instead, the run is fed by hand: the text is taken as what a
run of that use case wrote, written into a row of its own (`trigger =
manual`, no subject), and delivered to every target the use case declares,
exactly as a run's text would be — a dormant use case included, since no
model is asked. The answer comes once it is delivered: 200 with the run's id,
its status and what it created, `failed` with the reason when a target
refused. The bridge's `start_run` never sends it; it is how a delivery is
tried live before a skill writes for it.

`POST /api/memory` with `use_case` and `text` appends the note to the use
case's memory, if it declares one, cut to its bound (`memory.py`).

Every refusal is `{"error": "<reason>"}` with a 4xx; 503 when the ledger or the
harness did not answer, which the caller may try again.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .closing import Closer
from .deliveries import RunOutput
from .event_runs import EventRuns
from .events import EpisodeRequest
from .hermes import HermesError
from .ledger import Ledger, Usage
from .memory import LIMIT_BYTES
from .metrics import Metrics
from .schedules import MissingJobError, PausedJobError, Schedules
from .use_cases import UseCase

logger = logging.getLogger(__name__)

RUNS_PATH = "/api/runs"
MEMORY_PATH = "/api/memory"

_Answer = tuple[int, dict[str, Any]]


class _RefusedError(Exception):
    """A request answered with a reason instead of what it asked for."""

    def __init__(self, status_code: int, error: str, **extra: Any) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.body = {"error": error, **extra}


class TriggerApi:
    """The two routes, wired to the event path, the schedules and the ledger."""

    def __init__(
        self,
        use_cases: Mapping[str, UseCase],
        runs: EventRuns,
        schedules: Schedules,
        ledger: Ledger,
        closer: Closer,
        key: str,
        metrics: Metrics,
    ) -> None:
        self._use_cases = use_cases
        self._runs = runs
        self._schedules = schedules
        self._ledger = ledger
        self._closer = closer
        self._key = key
        self._metrics = metrics

    def routes(self) -> list[Route]:
        return [
            Route(RUNS_PATH, self._endpoint("runs", self._start_run), methods=["POST"]),
            Route(MEMORY_PATH, self._endpoint("memory", self._append_memory), methods=["POST"]),
        ]

    def _endpoint(
        self, route: str, handle: Callable[[dict[str, Any]], Awaitable[_Answer]]
    ) -> Callable[[Request], Awaitable[JSONResponse]]:
        async def endpoint(request: Request) -> JSONResponse:
            try:
                self._authorise(request)
                status_code, body = await handle(await _object(request))
            except _RefusedError as refused:
                status_code, body = refused.status_code, refused.body
            except (HermesError, httpx.HTTPError, psycopg.Error, OSError) as exc:
                logger.exception("could not answer a request on %s", request.url.path)
                status_code, body = (
                    503,
                    {"error": f"the ledger or the harness did not answer: {exc}"},
                )
            self._metrics.api_requests.labels(route=route, code=str(status_code)).inc()
            return JSONResponse(body, status_code=status_code)

        return endpoint

    def _authorise(self, request: Request) -> None:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme != "Bearer" or not hmac.compare_digest(token.encode(), self._key.encode()):
            logger.warning("refused a request on %s without the key", request.url.path)
            raise _RefusedError(401, "a missing or wrong key")

    def _use_case(self, body: dict[str, Any]) -> UseCase:
        name = body.get("use_case")
        if not isinstance(name, str) or not name:
            raise _RefusedError(400, "use_case: the name of a declared use case")
        use_case = self._use_cases.get(name)
        if use_case is None:
            raise _RefusedError(404, f"no use case named {name}")
        return use_case

    async def _start_run(self, body: dict[str, Any]) -> _Answer:
        use_case = self._use_case(body)
        if "output" in body:
            return await self._hand_fed(use_case, body)
        subject = body.get("subject")
        subject = str(subject).strip() if subject is not None else ""
        if not use_case.is_enabled:
            raise _RefusedError(409, f"{use_case.name} is dormant: {use_case.dormant}")
        if use_case.is_chat:
            raise _RefusedError(400, f"{use_case.name} is the chat itself: write to it instead")
        if use_case.event_trigger is not None:
            return await self._start_on_episode(use_case, subject)
        if subject:
            raise _RefusedError(400, f"{use_case.name} runs on its schedule and takes no subject")
        if await self._ledger.runs_today(use_case.name) >= use_case.budget.runs_per_day:
            raise _RefusedError(429, _spent(use_case))
        try:
            job_id = await self._schedules.run_now(use_case)
        except MissingJobError as exc:
            raise _RefusedError(503, str(exc)) from exc
        except PausedJobError as exc:
            raise _RefusedError(409, str(exc)) from exc
        return 202, {
            "use_case": use_case.name,
            "job_id": job_id,
            "status": "requested",
            "output": list(use_case.output),
        }

    async def _start_on_episode(self, use_case: UseCase, subject: str) -> _Answer:
        if not subject:
            raise _RefusedError(
                400, f"{use_case.name} runs on an episode: name the episode id as the subject"
            )
        try:
            episode_id = int(subject)
        except ValueError:
            raise _RefusedError(
                400, f"{use_case.name} runs on an episode: {subject!r} is no episode id"
            ) from None
        episode = await self._ledger.episode(episode_id)
        if episode is None:
            raise _RefusedError(404, f"no episode {episode_id}")

        request = EpisodeRequest(
            episode_id=episode.episode_id,
            fault=episode.fault,
            subject=episode.subject,
            severity=episode.severity,
            requested_at=datetime.now(UTC),
        )
        requested = await self._runs.request(use_case, request)
        if requested.status == "running":
            raise _RefusedError(
                409,
                f"{use_case.name} is already running on episode {episode_id}"
                f" as run {requested.run_id}",
                run_id=requested.run_id,
            )
        if requested.status == "duplicate":
            raise _RefusedError(
                409, f"{use_case.name} was started on episode {episode_id} a moment ago"
            )
        if requested.status == "capped":
            raise _RefusedError(429, _spent(use_case), run_id=requested.run_id)
        return 202, {
            "use_case": use_case.name,
            "run_id": requested.run_id,
            "subject_key": request.subject_key,
            "status": "queued",
            "output": list(use_case.output),
        }

    async def _hand_fed(self, use_case: UseCase, body: dict[str, Any]) -> _Answer:
        """Deliver a text as if a run of the use case had written it; no model is asked."""
        if use_case.is_chat:
            raise _RefusedError(400, f"{use_case.name} is the chat itself: write to it instead")
        if body.get("subject") is not None:
            raise _RefusedError(400, "a hand-fed run takes no subject")
        output = body["output"]
        if not isinstance(output, str) or not output.strip():
            raise _RefusedError(400, "output: the text a run would have written")
        unserved = [target for target in use_case.output if not self._closer.serves(target)]
        if unserved:
            raise _RefusedError(
                409,
                f"{use_case.name} declares {', '.join(unserved)}, which this trigger is not set up"
                " to deliver",
            )
        run_id = await self._ledger.record_turn(
            use_case=use_case.name,
            trigger="manual",
            subject_kind="none",
            subject_key=f"manual:{datetime.now(UTC):%Y%m%dT%H%M%S.%fZ}",
            session_id=None,
            harness_run_id=None,
            status="running",
            language=use_case.language,
            text=output,
            error=None,
            usage=Usage(),
        )
        if run_id is None:
            raise _RefusedError(409, f"a hand-fed run of {use_case.name} was taken a moment ago")
        status, delivered = await self._closer.deliver(
            use_case,
            RunOutput(
                run_id=run_id, use_case=use_case.name, occasion=None, text=output, usage=Usage()
            ),
            what=f"run {run_id}",
        )
        self._metrics.runs.labels(use_case=use_case.name, status=status).inc()
        answer: dict[str, Any] = {
            "use_case": use_case.name,
            "run_id": run_id,
            "status": status,
            "output_ref": list(delivered.refs),
        }
        if delivered.refusals:
            answer["error"] = delivered.error
        return 200, answer

    async def _append_memory(self, body: dict[str, Any]) -> _Answer:
        use_case = self._use_case(body)
        if not use_case.memory:
            raise _RefusedError(409, f"{use_case.name} keeps no memory")
        text = body.get("text")
        note = text.strip() if isinstance(text, str) else ""
        if not note:
            raise _RefusedError(400, "text: the note to append")
        size = len(note.encode("utf-8"))
        if size > LIMIT_BYTES:
            raise _RefusedError(
                400, f"text: {size} bytes, more than the {LIMIT_BYTES} a memory holds"
            )
        held = await self._ledger.append_memory(use_case.name, note)
        return 200, {"use_case": use_case.name, "bytes": held}


def _spent(use_case: UseCase) -> str:
    return f"{use_case.name} has spent its {use_case.budget.runs_per_day} runs today"


async def _object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise _RefusedError(400, "the body must be a JSON object")
    return body
