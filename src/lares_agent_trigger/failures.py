"""What went wrong with a run, and whether trying once more could help.

The harness reports a failed run as free text, so the class is read off the
text. The rules follow the gateway's own classifier for cron runs
(`agent/monitoring/cron_health.py`, first match wins), with two classes it has
no use for: credit exhaustion, which has to outrank auth and rate limits
because xAI sends it as a 403 and OpenAI as a quota, and a spent budget.

A failure that fixes itself gets one retry; one that does not — a wrong key,
an empty account, a broken configuration, a run that used up its budget, or
something nobody recognises — is reported at once.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

import httpx

from .hermes import HermesError

FailureClass = Literal[
    "rate_limited",
    "timeout",
    "network_error",
    "hermes_unreachable",
    "interrupted",
    "credits_exhausted",
    "auth_failed",
    "budget_exhausted",
    "invalid_config",
    "unknown",
    # Not read off any text: the event path finds a row a dead pod left open.
    "trigger_restarted",
    # Not read off any text: a declared target refused the finished output.
    "delivery_failed",
]

TRANSIENT: frozenset[FailureClass] = frozenset(
    {"rate_limited", "timeout", "network_error", "hermes_unreachable", "interrupted"}
)

# Word boundaries, so "oauth", "tokenizer" or "HTTP 4015" do not read as auth.
_AUTH_RE = re.compile(
    r"\b(?:authentication|authenticated|authenticate|authorization|authorized|authorize"
    r"|unauthorized|forbidden|bearer|401|403)\b"
    r"|\b(?:access|api|refresh) token\b"
)


def _contains_any(*needles: str) -> Callable[[str], bool]:
    return lambda text: any(needle in text for needle in needles)


def _status(code: int) -> Callable[[str], bool]:
    """An HTTP status as a word of its own, not a digit run inside a request id."""
    pattern = re.compile(rf"\b{code}\b")
    return lambda text: pattern.search(text) is not None


def _either(*rules: Callable[[str], bool]) -> Callable[[str], bool]:
    return lambda text: any(rule(text) for rule in rules)


_RULES: tuple[tuple[Callable[[str], bool], FailureClass], ...] = (
    (
        _either(
            _contains_any(
                "insufficient credits",
                "insufficient_quota",
                "insufficient balance",
                "credit balance",
                "credits exhausted",
                "no usable credits",
                "payment required",
            ),
            _status(402),
        ),
        "credits_exhausted",
    ),
    (lambda text: _AUTH_RE.search(text) is not None, "auth_failed"),
    # The gateway's iteration cap, and our own deadline in `HermesClient.await_run`.
    (
        _contains_any("iteration budget exhausted", "maximum iterations", "did not finish within"),
        "budget_exhausted",
    ),
    (
        _either(
            _contains_any("rate limit", "rate_limit", "quota", "too many requests"), _status(429)
        ),
        "rate_limited",
    ),
    (_contains_any("timeout", "timed out"), "timeout"),
    (
        _contains_any("network", "connection", "dns", "socket", "unreachable", "name resolution"),
        "network_error",
    ),
    (_contains_any("interrupt", "restarted"), "interrupted"),
    (_contains_any("config", "missing", "invalid"), "invalid_config"),
)


def classify_error(error: str) -> FailureClass:
    """The class of a run the harness closed as failed, from its error text."""
    text = error.lower()
    return next((failure for matches, failure in _RULES if matches(text)), "unknown")


def classify_exception(exc: Exception) -> FailureClass:
    """The class of a call to the harness that raised instead of answering."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.TransportError | OSError):
        return "hermes_unreachable"
    if isinstance(exc, HermesError) and exc.status_code is not None:
        if exc.status_code >= 500:
            return "hermes_unreachable"
        if exc.status_code == 429:
            return "rate_limited"
        if exc.status_code in (401, 403):
            return "auth_failed"
        return "invalid_config"
    return "unknown"


def describe(exc: Exception) -> str:
    """The exception's own text; some transport errors carry none."""
    return str(exc) or type(exc).__name__
