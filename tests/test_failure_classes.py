"""Which failure is which: the gateway's error texts against the classes that decide a retry."""

from __future__ import annotations

import httpx
import pytest

from lares_agent_trigger.failures import classify_error, classify_exception
from lares_agent_trigger.hermes import HermesError


@pytest.mark.parametrize(
    ("error", "failure_class"),
    [
        ("Error code: 429 - rate limit exceeded", "rate_limited"),
        ("You exceeded your current quota", "rate_limited"),
        ("Request timed out.", "timeout"),
        ("Connection error.", "network_error"),
        ("The gateway restarted before this run settled.", "interrupted"),
        ("⚠️ Provider authentication failed: token expired", "auth_failed"),
        ("Error code: 401 - Unauthorized", "auth_failed"),
        # Credit exhaustion outranks both: xAI sends it as a 403, OpenAI as a quota.
        ("Error code: 403 - Your team has no usable credits", "credits_exhausted"),
        ("insufficient_quota: You exceeded your current quota", "credits_exhausted"),
        ("Error code: 402 - Payment Required", "credits_exhausted"),
        ("Iteration budget exhausted (40/40)", "budget_exhausted"),
        ("run did not finish within 10 minutes (last status: running)", "budget_exhausted"),
        ("invalid model configuration", "invalid_config"),
        # A status code counts as one only where it stands alone.
        ("upstream request req_84029 was dropped", "unknown"),
        ("upstream request req_14290 was dropped", "unknown"),
        ("harness ended the run as cancelled", "unknown"),
        ("", "unknown"),
    ],
)
def test_a_run_error_is_classified(error: str, failure_class: str) -> None:
    assert classify_error(error) == failure_class


@pytest.mark.parametrize(
    ("exc", "failure_class"),
    [
        (httpx.ConnectError("[Errno 111] Connection refused"), "hermes_unreachable"),
        (httpx.ReadTimeout("timed out"), "timeout"),
        (HermesError("POST /v1/runs returned 503: down", status_code=503), "hermes_unreachable"),
        (HermesError("POST /v1/runs returned 429: slow down", status_code=429), "rate_limited"),
        (HermesError("POST /v1/runs returned 401: no", status_code=401), "auth_failed"),
        (HermesError("POST /v1/runs returned 400: bad", status_code=400), "invalid_config"),
        (HermesError("POST /v1/runs returned no run_id"), "unknown"),
    ],
)
def test_a_harness_call_that_raised_is_classified(exc: Exception, failure_class: str) -> None:
    assert classify_exception(exc) == failure_class
