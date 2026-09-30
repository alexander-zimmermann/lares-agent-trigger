"""The harness's lifecycle hooks as they arrive over HTTP: signed reports, never requests.

Hermes mirrors its lifecycle hooks as POSTs (`hooks.outbound` in its
configuration, `agent/outbound_webhooks.py` on the other side of this
contract). The receiver takes two of them:

- `post_api_request`, once per call to the model inside a turn: the tokens,
  the model and its source, when the call started and ended, how many tools
  it asked for, and what it said.
- `on_session_end`, which despite its name fires once per turn — every chat
  message answered, every cron execution finished — with the turn's outcome.

A turn's calls all arrive before its end, one delivery after the other.
`session_id` is promoted to the top level of the body; everything else of the
hook call sits under `extra`.

The signature is HMAC-SHA256 over the raw body with the shared secret, sent
as `X-Hermes-Signature-256: sha256=<hex>`; the body is only parsed once it
has been verified.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

MODEL_CALL = "post_api_request"
TURN_ENDED = "on_session_end"

_SIGNATURE_PREFIX = "sha256="
# A cron run's task id: `cron:<job id>:<execution id>`.
_CRON_TASK_PREFIX = "cron"


@dataclass(frozen=True, slots=True)
class ModelCall:
    """One call to the model inside a turn."""

    turn_id: str
    platform: str
    # The call's number within its turn: what tells a redelivery from the next call.
    number: int
    model: str | None
    model_source: str | None
    # What the model read, cached or not, and what it wrote; None when the
    # provider reported no usage for the call.
    tokens_in: int | None
    tokens_out: int | None
    tool_calls: int
    started_at: float
    ended_at: float
    # The text of the reply; the last call's is the turn's answer.
    content: str | None


@dataclass(frozen=True, slots=True)
class CronRun:
    """The job and the execution a cron turn belongs to."""

    job_id: str
    execution_id: str


@dataclass(frozen=True, slots=True)
class TurnEnded:
    """One finished turn of the harness: a chat message answered or a cron execution run."""

    session_id: str
    # The harness's own name for the turn: `<session>:<task>:<random>`.
    turn_id: str
    # `cron:<job id>:<execution id>` for a cron run, the session id for a chat turn.
    task_id: str
    # The surface that started the turn: `discord`, `cron`, `api_server`, …
    platform: str
    model: str | None
    completed: bool
    # Why the turn stopped (`text_response(stop)`, `error(…)`, …).
    exit_reason: str | None

    @property
    def turn(self) -> str:
        """The random tail of the turn id — what tells two turns of one session apart."""
        return self.turn_id.rpartition(":")[2]

    @property
    def cron_run(self) -> CronRun | None:
        """The job and execution this turn ran as, if it is a cron run."""
        prefix, _, rest = self.task_id.partition(":")
        job_id, _, execution_id = rest.partition(":")
        if prefix != _CRON_TASK_PREFIX or not job_id or not execution_id:
            return None
        return CronRun(job_id, execution_id)


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """True when the header is the HMAC of this body under the shared secret."""
    if not header or not header.startswith(_SIGNATURE_PREFIX):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix(_SIGNATURE_PREFIX))


def parse_hook(body: bytes) -> ModelCall | TurnEnded:
    """Parse one verified body; anything that is neither hook is a ``ValueError``."""
    try:
        raw = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"hook body is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"hook body is not an object: {type(raw).__name__}")
    extra = raw.get("extra")
    if not isinstance(extra, dict):
        raise ValueError("hook body carries no `extra` object")

    event = raw.get("hook_event_name")
    if event == MODEL_CALL:
        return _model_call(extra)
    if event == TURN_ENDED:
        return _turn_ended(raw, extra)
    raise ValueError(f"hook event {event!r} is neither {MODEL_CALL} nor {TURN_ENDED}")


def _model_call(extra: dict[str, Any]) -> ModelCall:
    # The gateway sends `usage: null` for a response that carried none.
    usage = extra.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ValueError("hook body carries an `extra.usage` that is not an object")
    # The sanitised reply, not `assistant_message`, which arrives as the object's repr.
    response = extra.get("response")
    reply = response.get("assistant_message") if isinstance(response, dict) else None
    content = reply.get("content") if isinstance(reply, dict) else None
    return ModelCall(
        turn_id=_text(extra, "turn_id"),
        platform=_text(extra, "platform"),
        number=_count(extra, "api_call_count"),
        model=_optional_text(extra.get("model")),
        model_source=_optional_text(extra.get("provider")),
        tokens_in=_count(usage, "prompt_tokens") if usage is not None else None,
        tokens_out=_count(usage, "output_tokens") if usage is not None else None,
        tool_calls=_count(extra, "assistant_tool_call_count"),
        started_at=_seconds(extra, "started_at"),
        ended_at=_seconds(extra, "ended_at"),
        content=content if isinstance(content, str) and content.strip() else None,
    )


def _turn_ended(raw: dict[str, Any], extra: dict[str, Any]) -> TurnEnded:
    completed = extra.get("completed")
    if not isinstance(completed, bool):
        raise ValueError("hook body misses `extra.completed`")
    return TurnEnded(
        session_id=_text(raw, "session_id"),
        turn_id=_text(extra, "turn_id"),
        task_id=_text(extra, "task_id"),
        platform=_text(extra, "platform"),
        model=_optional_text(extra.get("model")),
        completed=completed,
        exit_reason=_optional_text(extra.get("turn_exit_reason")),
    )


def _text(source: dict[str, Any], field: str) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"hook body misses `{field}`")
    return value


def _count(source: dict[str, Any], field: str) -> int:
    value = source.get(field)
    # A bool is an int to Python and never a count here.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"hook body misses `{field}`")
    return value


def _seconds(source: dict[str, Any], field: str) -> float:
    value = source.get(field)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"hook body misses `{field}`")
    return float(value)


def _optional_text(value: Any) -> str | None:
    return str(value) if value else None
