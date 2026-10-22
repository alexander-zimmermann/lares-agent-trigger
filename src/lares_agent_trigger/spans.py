"""A turn's trace, built after the fact from the times the harness's hooks reported.

The harness emits no spans of its own for a turn, so the trigger writes them
once the turn is over: one root `invoke_agent <use case>`, and under it one
`chat <model>` per model call and one `execute_tool <tool>` per tool call, at
the times they started and ended. Names follow OpenTelemetry's GenAI
conventions. A span carries names and numbers only: never a prompt, an answer
or what a tool was asked or returned, and `error.type` is one of a few classes,
never a reason in the harness's words. An attempt at a call that failed is a
`chat <model>` span too, with the harness's class of the failure.

The root is started before the row is written, because the row names the
trace; it is ended once the row's id is known. A root that is never ended is
never exported — what becomes of a delivery whose row was already there.

A log line written inside a span carries its `trace_id` and `span_id`, so
Grafana can go from a trace to its lines and back, and a line of a person's
turn names them as `enduser.id`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind, StatusCode

if TYPE_CHECKING:
    # The hooks reach the ledger, whose row write is a span: no import at run time.
    from .hooks import FailedCall, ModelCall, ToolCall
    from .ledger import Surface

TRACER = trace.get_tracer("lares_agent_trigger")

# A turn whose end said it did not complete.
TURN_FAILED = "turn_failed"
# A turn whose end never came, given up with its tally.
TURN_WITHOUT_END = "turn_without_end"

# The person the current turn answered, for its log lines.
_ENDUSER: ContextVar[str | None] = ContextVar("enduser_id", default=None)


def failed(span: Span, error_type: str) -> None:
    """Mark the span as failed, with the class of what went wrong."""
    span.set_status(StatusCode.ERROR)
    span.set_attribute("error.type", error_type)


class TurnTrace:
    """The root of one turn's trace, from its first call until its end is known."""

    def __init__(
        self,
        agent: str | None,
        *,
        conversation_id: str | None,
        started_at: float,
        context: Context | None = None,
        enduser_id: str | None = None,
        surface: Surface | None = None,
    ) -> None:
        """Start the root; `context` None makes it a child of the current span.

        `agent` is the use case the turn ran; None for one nobody can tell.
        `enduser_id` and `surface` name the person a chat turn answered and
        where they wrote; a machine's turn has neither.
        """
        attributes = {"gen_ai.operation.name": "invoke_agent"}
        if agent is not None:
            attributes["gen_ai.agent.name"] = agent
        if conversation_id is not None:
            attributes["gen_ai.conversation.id"] = conversation_id
        if enduser_id is not None:
            attributes["enduser.id"] = enduser_id
        if surface is not None:
            attributes["lares.surface"] = surface
        self._enduser_id = enduser_id
        self._root: Span = TRACER.start_span(
            f"invoke_agent {agent}" if agent is not None else "invoke_agent",
            context=context,
            kind=SpanKind.INTERNAL,
            start_time=_ns(started_at),
            attributes=attributes,
        )

    @property
    def trace_id(self) -> str | None:
        """The trace's id as the row holds it; None when it was not sampled."""
        return _sampled_id(self._root)

    @contextmanager
    def current(self) -> Iterator[None]:
        """Make the root the current span, so a log line carries the turn's trace and person."""
        with trace.use_span(self._root, end_on_exit=False):
            token = _ENDUSER.set(self._enduser_id)
            try:
                yield
            finally:
                _ENDUSER.reset(token)

    def finish(
        self,
        *,
        run_id: int | None,
        ended_at: float,
        calls: Sequence[ModelCall],
        tools: Sequence[ToolCall],
        failures: Sequence[FailedCall] = (),
        error_type: str | None = None,
    ) -> None:
        """Name the row, add a span per call, failure and tool, and end the root at the turn's end.

        `run_id` is None for a turn that has no row.
        """
        if run_id is not None:
            self._root.set_attribute("lares.run.id", run_id)
        if error_type is not None:
            failed(self._root, error_type)
        parent = trace.set_span_in_context(self._root)
        for call in calls:
            _chat_span(call, parent)
        for failure in failures:
            _failed_chat_span(failure, parent)
        for tool in tools:
            _tool_span(tool, parent)
        self._root.end(end_time=_ns(ended_at))


class TraceIds(logging.Filter):
    """Adds the current span's `trace_id` and `span_id` to a record, when it is sampled.

    Inside a person's turn it adds `enduser.id` as well, sampled or not.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        context = trace.get_current_span().get_span_context()
        if context.is_valid and context.trace_flags.sampled:
            record.trace_id = f"{context.trace_id:032x}"
            record.span_id = f"{context.span_id:016x}"
        enduser_id = _ENDUSER.get()
        if enduser_id is not None:
            # OpenTelemetry's name, as the bridge's lines carry it.
            record.__dict__["enduser.id"] = enduser_id
        return True


def log_trace_ids() -> None:
    """Have every handler of the root logger add the ids; called once logging is set up."""
    for handler in logging.getLogger().handlers:
        handler.addFilter(TraceIds())


def current_trace_id() -> str | None:
    """The id of the trace the current span belongs to, as a row holds it, if it is sampled."""
    return _sampled_id(trace.get_current_span())


def _sampled_id(span: Span) -> str | None:
    context = span.get_span_context()
    if not context.is_valid or not context.trace_flags.sampled:
        return None
    return f"{context.trace_id:032x}"


def _start_chat(
    model_source: str | None,
    model: str | None,
    started_at: float,
    parent: Context,
    usage: dict[str, int] | None = None,
) -> Span:
    attributes: dict[str, str | int] = {"gen_ai.operation.name": "chat"}
    if model_source is not None:
        attributes["gen_ai.provider.name"] = model_source
    if model is not None:
        attributes["gen_ai.request.model"] = model
    return TRACER.start_span(
        f"chat {model}" if model is not None else "chat",
        context=parent,
        kind=SpanKind.CLIENT,
        start_time=_ns(started_at),
        attributes={**attributes, **(usage or {})},
    )


def _chat_span(call: ModelCall, parent: Context) -> None:
    usage: dict[str, int] = {}
    if call.tokens_in is not None:
        usage["gen_ai.usage.input_tokens"] = call.tokens_in
    if call.tokens_out is not None:
        usage["gen_ai.usage.output_tokens"] = call.tokens_out
    span = _start_chat(call.model_source, call.model, call.started_at, parent, usage)
    span.end(end_time=_ns(call.ended_at))


def _failed_chat_span(failure: FailedCall, parent: Context) -> None:
    span = _start_chat(failure.model_source, failure.model, failure.started_at, parent)
    failed(span, failure.reason)
    span.end(end_time=_ns(failure.ended_at))


def _tool_span(tool: ToolCall, parent: Context) -> None:
    attributes = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool.name}
    if tool.call_id is not None:
        attributes["gen_ai.tool.call.id"] = tool.call_id
    span = TRACER.start_span(
        f"execute_tool {tool.name}",
        context=parent,
        kind=SpanKind.INTERNAL,
        start_time=_ns(tool.started_at),
        attributes=attributes,
    )
    if tool.error_type is not None:
        failed(span, tool.error_type)
    span.end(end_time=_ns(tool.ended_at))


def _ns(seconds: float) -> int:
    """Unix seconds as the nanoseconds a span's clock counts in."""
    return round(seconds * 1e9)
