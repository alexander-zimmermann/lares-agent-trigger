"""The durable pull consumer on the EPISODE stream.

The consumer itself is a CRD in lares, not created here: the service binds to
`agent-trigger` and fails loudly when it is absent, so a missing right or a
misspelt name is a startup error rather than a silently empty queue.

One message at a time, acknowledged after the row is closed. A run may take
its whole budget, so the consumer's ackWait must be longer than the longest
budget declared; the CRD in lares carries that number.
"""

from __future__ import annotations

import logging

import nats
from nats.aio.client import Client as NatsClient
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js import JetStreamContext

from .config import Settings
from .event_runs import EventRuns
from .events import parse_episode_event
from .metrics import Metrics

logger = logging.getLogger(__name__)


class EpisodeConsumer:
    """Binds the durable consumer and turns each message into a handled event."""

    def __init__(self, settings: Settings, runs: EventRuns, metrics: Metrics) -> None:
        self._settings = settings
        self._runs = runs
        self._metrics = metrics
        self._nc: NatsClient | None = None
        self._subscription: JetStreamContext.PullSubscription | None = None

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected

    async def connect(self) -> None:
        self._nc = await nats.connect(
            servers=self._settings.nats_servers_list,
            name="lares-agent-trigger",
            **self._settings.nats_auth_kwargs(),
        )
        self._subscription = await self._nc.jetstream().pull_subscribe_bind(
            consumer=self._settings.consumer_name,
            stream=self._settings.nats_stream_name,
        )
        logger.info(
            "bound consumer %s on stream %s",
            self._settings.consumer_name,
            self._settings.nats_stream_name,
        )

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.close()
            self._nc = None
            self._subscription = None

    async def run_once(self) -> int:
        """Fetch at most one message, handle it, acknowledge it. Returns what it handled.

        A fetch that times out is the normal quiet case and returns 0. A message
        whose payload is not the engine's contract is acknowledged and counted:
        redelivering it forever would only block the ones behind it.
        """
        if self._subscription is None:
            raise RuntimeError("consumer not bound — call connect() first")
        try:
            messages = await self._subscription.fetch(
                batch=1, timeout=self._settings.fetch_timeout_seconds
            )
        except NatsTimeoutError:
            return 0

        for message in messages:
            try:
                event = parse_episode_event(message.data)
            except ValueError:
                logger.exception("dropping a message that is not an episode event")
                self._metrics.events.labels(kind="unknown", outcome="invalid").inc()
                await message.ack()
                continue
            await self._runs.handle(event)
            await message.ack()
        return len(messages)
