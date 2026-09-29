"""Entry point: load the declaration, open the ledger, bind the consumer, serve health."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
import time

from nats_bridge_core import configure as configure_logging
from nats_bridge_core import serve as serve_metrics
from nats_bridge_core import tracing, watchdog_ok

from .config import Settings
from .consumer import EpisodeConsumer
from .event_runs import EventRuns
from .hermes import HermesClient
from .ledger import Ledger
from .metrics import Metrics
from .use_cases import load_use_cases

logger = logging.getLogger(__name__)


async def _amain() -> int:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level, settings.log_format)
    tracing.configure(settings, service_name="lares-agent-trigger")
    logger.info("lares-agent-trigger starting")

    # A file that does not validate stops the pod here, with the reason in the
    # log: starting runs from a half-read catalogue is worse than not starting.
    try:
        use_cases = load_use_cases(settings.use_cases_file)
    except ValueError as exc:
        logger.error("refusing to start: %s", exc)
        return 1
    for use_case in use_cases.values():
        logger.info(
            "config: use case %s (%s)",
            use_case.name,
            "enabled" if use_case.is_enabled else f"dormant — {use_case.dormant}",
        )

    metrics = Metrics()
    ledger = Ledger(settings)
    hermes = HermesClient(settings)
    consumer = EpisodeConsumer(settings, EventRuns(use_cases, ledger, hermes, metrics), metrics)

    async def is_healthy() -> bool:
        # The harness is deliberately not part of health: a gateway outage is
        # a failed run and its own alert, never a restart loop here.
        if not consumer.is_connected or not await ledger.is_reachable():
            return False
        return watchdog_ok(time.monotonic())

    http_server = await serve_metrics(metrics.registry, settings.metrics_port, is_healthy)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        await ledger.open()
        await consumer.connect()
        logger.info("trigger is up")
        while not stop.is_set():
            await consumer.run_once()
    except Exception:
        logger.exception("fatal error in trigger startup/run")
        return 1
    finally:
        logger.info("shutting down")
        await consumer.close()
        await hermes.aclose()
        await ledger.close()
        http_server.close()
        with contextlib.suppress(Exception):
            await http_server.wait_closed()
        tracing.shutdown()
    return 0


def run() -> None:
    """Console entry point."""
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    run()
