"""The trigger's one seam: a real NATS stream and a real ledger, a fake harness.

Everything a neighbour would see is real here — the message on JetStream, the
durable consumer, the rows in Postgres — and only the harness is replaced,
because a run is a model call. Skipped automatically where Docker is absent.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import nats
import psycopg
import pytest
import pytest_asyncio
from nats.js.api import AckPolicy, ConsumerConfig
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs
from testcontainers.postgres import PostgresContainer

from lares_agent_trigger.config import Settings
from lares_agent_trigger.consumer import EpisodeConsumer
from lares_agent_trigger.event_runs import EventRuns
from lares_agent_trigger.hermes import HermesClient
from lares_agent_trigger.ledger import Ledger
from lares_agent_trigger.metrics import Metrics
from lares_agent_trigger.use_cases import load_use_cases

# The ledger lives in the TimescaleDB instance; the two tables themselves are
# plain, so the image matters only for fidelity with bootstrap.sql.
TIMESCALEDB_IMAGE = "timescale/timescaledb:latest-pg17"
NATS_IMAGE = "nats:2.10-alpine"

HERMES_URL = "http://hermes.test:8642"

USE_CASES = """
use_cases:
  - name: explain-episode
    sentence: Explains a new or escalated episode on its own event.
    trigger:
      kind: event
      source: episode
      filter:
        appeared: 2
        escalated: 2
    skill: lares-explain
    tools: [lares]
    output: [stored]
    budget:
      tool_calls: 40
      minutes: 10
      runs_per_day: 10
    language: de
    memory: false
    enabled: true
"""

# The ledger as bootstrap.sql creates it; the unique index is the dedupe key.
_LEDGER_DDL = """
CREATE TABLE agent_runs (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    use_case       TEXT           NOT NULL,
    trigger        TEXT           NOT NULL
        CHECK (trigger IN ('event', 'schedule', 'message', 'manual')),
    subject_kind   TEXT           NOT NULL
        CHECK (subject_kind IN ('episode', 'alert_group', 'chat', 'none')),
    subject_key    TEXT,
    session_id     TEXT,
    harness_run_id TEXT,
    status         TEXT           NOT NULL
        CHECK (status IN ('queued', 'running', 'completed', 'failed', 'capped')),
    attempt        SMALLINT       NOT NULL DEFAULT 1,
    error          TEXT,
    tldr           TEXT,
    text           TEXT,
    language       TEXT,
    output_ref     TEXT[]         NOT NULL DEFAULT '{}',
    output_state   TEXT[]         NOT NULL DEFAULT '{}',
    model_source   TEXT,
    model          TEXT,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    cost           NUMERIC(10, 6),
    duration       INTERVAL,
    tool_trace     JSONB,
    verdict        TEXT           CHECK (verdict IN ('helpful', 'useless')),
    verdict_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ    NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    CONSTRAINT agent_runs_output_positions
        CHECK (cardinality(output_ref) = cardinality(output_state)),
    CONSTRAINT agent_runs_output_state_values
        CHECK (array_remove(output_state, NULL) <@ ARRAY['open', 'merged', 'closed'])
);
CREATE UNIQUE INDEX agent_runs_subject_idx
    ON agent_runs (use_case, subject_kind, subject_key);
CREATE TABLE agent_memory (
    use_case   TEXT        PRIMARY KEY,
    text       TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    container = PostgresContainer(
        TIMESCALEDB_IMAGE, username="test", password="test", dbname="homelab"
    )
    container.start()
    try:
        with psycopg.connect(_dsn(container), autocommit=True) as conn:
            conn.execute(_LEDGER_DDL)
        yield container
    finally:
        container.stop()


def _dsn(container: PostgresContainer) -> str:
    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)
    return f"postgresql://test:test@{host}:{port}/homelab"


@pytest.fixture(scope="session")
def nats_url() -> Iterator[str]:
    container = DockerContainer(NATS_IMAGE).with_command("-js").with_exposed_ports(4222)
    container.start()
    try:
        wait_for_logs(container, "Server is ready")
        host = container.get_container_host_ip()
        yield f"nats://{host}:{container.get_exposed_port(4222)}"
    finally:
        container.stop()


@pytest.fixture
def rows(postgres: PostgresContainer) -> Iterator[Callable[[], list[dict[str, Any]]]]:
    """Read the ledger, newest first, and leave it empty for the next test."""
    with psycopg.connect(_dsn(postgres), autocommit=True, row_factory=psycopg.rows.dict_row) as c:
        c.execute("TRUNCATE agent_runs")

        def read() -> list[dict[str, Any]]:
            return list(c.execute("SELECT * FROM agent_runs ORDER BY id"))

        yield read


@pytest.fixture
def settings(postgres: PostgresContainer, nats_url: str, tmp_path: Path) -> Settings:
    use_cases_file = tmp_path / "use-cases.yaml"
    use_cases_file.write_text(USE_CASES, encoding="utf-8")
    host = postgres.get_container_host_ip()
    return Settings(
        nats_servers=nats_url,
        use_cases_file=use_cases_file,
        db_host=host,
        db_port=int(postgres.get_exposed_port(5432)),
        db_name="homelab",
        db_username="test",
        db_password="test",
        hermes_url=HERMES_URL,
        hermes_api_key="k" * 32,
        # The poll loop is exercised, not waited on.
        hermes_poll_seconds=0.01,
        fetch_timeout_seconds=2.0,
    )


@pytest_asyncio.fixture
async def stream(nats_url: str) -> AsyncIterator[Any]:
    """A fresh EPISODE stream with the durable consumer the service binds to."""
    connection = await nats.connect(servers=[nats_url])
    js = connection.jetstream()
    with contextlib.suppress(Exception):
        await js.delete_stream("EPISODE")
    await js.add_stream(name="EPISODE", subjects=["episode.>"])
    await js.add_consumer(
        "EPISODE",
        ConsumerConfig(durable_name="agent-trigger", ack_policy=AckPolicy.EXPLICIT, ack_wait=60),
    )
    try:
        yield js
    finally:
        await connection.close()


@pytest.fixture
def publish_raw(stream: Any) -> Callable[[bytes], Awaitable[None]]:
    """Put an arbitrary body on the stream, for the payloads that are not the contract."""

    async def _publish(body: bytes) -> None:
        await stream.publish("episode.appeared", body)

    return _publish


@pytest.fixture
def publish(stream: Any) -> Callable[..., Awaitable[None]]:
    """Publish one episode event as the engine's adapter shapes it."""

    async def _publish(kind: str, severity: int, *, episode_id: int = 15510) -> None:
        body = {
            "episode_id": episode_id,
            "fault": "appliance_runtime",
            "subject": "2/1/197",
            "severity": severity,
            "kind": kind,
            "time": "2026-09-25T14:20:00+00:00",
        }
        await stream.publish(f"episode.{kind}", json.dumps(body).encode())

    return _publish


@pytest_asyncio.fixture
async def consumer(
    settings: Settings, stream: Any
) -> AsyncIterator[tuple[EpisodeConsumer, Metrics]]:
    """The service as it runs: real ledger, real consumer, one HTTP client to fake."""
    metrics = Metrics()
    ledger = Ledger(settings)
    await ledger.open()
    hermes = HermesClient(settings)
    runs = EventRuns(load_use_cases(settings.use_cases_file), ledger, hermes, metrics)
    episode_consumer = EpisodeConsumer(settings, runs, metrics)
    await episode_consumer.connect()
    try:
        yield episode_consumer, metrics
    finally:
        await episode_consumer.close()
        await hermes.aclose()
        await ledger.close()
