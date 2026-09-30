"""AgentRunFailed, posted straight to Alertmanager's alerts API.

No PrometheusRule stands behind this alert: a failed run is an event, not a
state a scrape could see, so the trigger raises it itself. It carries no
`endsAt`, which lets Alertmanager resolve it after its resolve timeout — the
failure happened once and there is nothing to keep firing.
"""

from __future__ import annotations

import logging

import httpx

from .config import Settings
from .metrics import Metrics

logger = logging.getLogger(__name__)

# Alertmanager keeps annotations whole; the cut keeps a stack trace from
# turning the notification into a wall of text. The row holds the full error.
_DESCRIPTION_LIMIT = 1024


class Alertmanager:
    """One HTTP client against Alertmanager's v2 API."""

    def __init__(
        self, settings: Settings, metrics: Metrics, client: httpx.AsyncClient | None = None
    ) -> None:
        self._metrics = metrics
        self._client = client or httpx.AsyncClient(
            base_url=settings.alertmanager_url.rstrip("/"),
            timeout=settings.alertmanager_request_timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def run_failed(self, *, use_case: str, summary: str, error: str) -> None:
        """Post AgentRunFailed; an Alertmanager that does not take it is logged, never raised.

        The row is already closed when this is called, and the message behind
        it must still be acknowledged — a lost alert costs a log line and a
        counter, not a redelivery loop.
        """
        alert = {
            "labels": {
                "alertname": "AgentRunFailed",
                "use_case": use_case,
                "severity": "warning",
            },
            "annotations": {
                "summary": summary,
                "description": error[:_DESCRIPTION_LIMIT],
            },
        }
        try:
            response = await self._client.post("/api/v2/alerts", json=[alert])
            response.raise_for_status()
        except httpx.HTTPError:
            logger.exception("could not post AgentRunFailed for %s: %s", use_case, summary)
            self._metrics.alerts.labels(outcome="failed").inc()
            return
        self._metrics.alerts.labels(outcome="sent").inc()
