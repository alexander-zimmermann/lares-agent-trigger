"""The HTTP receiver of the harness's outbound hook: verify, parse, hand on, answer.

The trigger's own API (`api.py`) shares the app and the port.

The answer is what the harness acts on. It sends a delivery at most twice,
once more only after a connection error or a 5xx, so every answer here is
chosen for that: 2xx for a delivery that is done with — a call or a tool
counted, a row written, either one already held, a turn that is not ours; 4xx
for one that can never succeed — a bad signature, a body that is none of the
hooks; 503 for one that might on the second try, because the ledger or the
Jobs API did not answer.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Literal

import httpx
import psycopg
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .api import TriggerApi
from .hermes import HermesError
from .hooks import ToolCall, parse_hook, verify_signature
from .metrics import Metrics
from .skill_corrections import SkillCorrections
from .turn_runs import Outcome, TurnRuns

logger = logging.getLogger(__name__)

HOOK_PATH = "/hooks/hermes"
_SIGNATURE_HEADER = "X-Hermes-Signature-256"

# What became of a delivery: the hook path's own outcome, or why it never got there.
HookOutcome = Outcome | Literal["refused", "invalid", "error"]


def create_app(
    turns: TurnRuns,
    secret: str,
    api: TriggerApi,
    metrics: Metrics,
    corrections: SkillCorrections,
) -> Starlette:
    """The ASGI app: the route the harness's hook posts to, and the trigger's own API.

    A skill write the read-only mount refused goes to the corrections as well,
    which take it up once the hook is answered.
    """

    async def hermes_hook(request: Request) -> JSONResponse:
        body = await request.body()
        if not verify_signature(secret, body, request.headers.get(_SIGNATURE_HEADER)):
            logger.warning("refused a hook delivery with a bad or missing signature")
            return _answer(metrics, "refused", 401)
        try:
            hook = parse_hook(body)
        except ValueError as exc:
            logger.warning("refused a hook delivery that is none of the hooks: %s", exc)
            return _answer(metrics, "invalid", 400)
        try:
            outcome = await turns.handle(hook)
        except HermesError, httpx.HTTPError, psycopg.Error, OSError:
            logger.exception("could not handle a hook of turn %s", hook.turn_id)
            return _answer(metrics, "error", 503)
        if isinstance(hook, ToolCall) and hook.skill_write is not None:
            corrections.start(hook.skill_write)
        return _answer(metrics, outcome, 200)

    return Starlette(routes=[Route(HOOK_PATH, hermes_hook, methods=["POST"]), *api.routes()])


def _answer(metrics: Metrics, outcome: HookOutcome, status_code: int) -> JSONResponse:
    metrics.hook_events.labels(outcome=outcome).inc()
    return JSONResponse({"outcome": outcome}, status_code=status_code)


class ReceiverServer(uvicorn.Server):
    """Uvicorn inside the service's own loop, leaving SIGTERM to the service.

    `serve()` would otherwise swap in its own signal handlers and stop the
    receiver ahead of the consumer; here both stop when the service says so.
    """

    def __init__(self, app: Starlette, port: int) -> None:
        super().__init__(
            uvicorn.Config(
                app, host="0.0.0.0", port=port, lifespan="off", access_log=False, log_config=None
            )
        )

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield
