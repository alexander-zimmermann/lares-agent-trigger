"""The trigger's one seam: a real NATS stream and a real ledger, fakes at the edge.

Everything a neighbour would see is real here — the message on JetStream, the
durable consumer, the signed POST to the hook receiver, the rows in
Postgres — and only the outside services are replaced: the harness, because
a run is a model call; Alertmanager and Discord over HTTP (`fakes.py`),
because the alert and the message are asserted as they would arrive there;
and the mail relay by an SMTP server in this process, because the mail is
asserted as the relay would take it.
"""

from __future__ import annotations

import contextlib
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import nats
import psycopg
import pytest
import pytest_asyncio
import respx
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP, Envelope, Session
from nats.js.api import AckPolicy, ConsumerConfig
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs
from testcontainers.postgres import PostgresContainer

from lares_agent_trigger.alerts import Alertmanager
from lares_agent_trigger.config import Settings
from lares_agent_trigger.consumer import EpisodeConsumer
from lares_agent_trigger.deliveries import build_deliveries
from lares_agent_trigger.event_runs import EventRuns
from lares_agent_trigger.hermes import HermesClient
from lares_agent_trigger.ledger import Ledger
from lares_agent_trigger.metrics import Metrics
from lares_agent_trigger.receiver import create_app
from lares_agent_trigger.turn_runs import TurnRuns
from lares_agent_trigger.use_cases import load_use_cases

# The ledger lives in the TimescaleDB instance; the two tables themselves are
# plain, so the image matters only for fidelity with bootstrap.sql.
TIMESCALEDB_IMAGE = "timescale/timescaledb:latest-pg17"
NATS_IMAGE = "nats:2.10-alpine"

HERMES_URL = "http://hermes.test:8642"
ALERTMANAGER_URL = "http://alertmanager.test:9093"
HOOK_SECRET = "h" * 32
# Short enough to wait on, long enough to measure between two starts.
RETRY_DELAY_SECONDS = 0.2

DISCORD_TOKEN = "discord-bot-token-for-tests"
# The home channel, as the harness has it.
DISCORD_CHANNEL = "1548229055348736034"
# The relay's one accepted sender, and the owner it writes to.
MAIL_FROM = "Lares <admin@zimmermann.sh>"
ACCEPTED_SENDER = "admin@zimmermann.sh"
MAIL_TO = "admin@zimmermann.sh"
DASHBOARD_EPISODE_URL = "https://grafana.test/d/knx-episodes?var-fault={fault}"

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
    output: [stored, discord, mail]
    budget:
      tool_calls: 40
      minutes: 10
      runs_per_day: 10
    language: de
    memory: false
    enabled: true

  - name: messenger
    sentence: Answers a message from the phone.
    trigger:
      kind: message
    tools: [lares]
    output: [discord]
    budget:
      tool_calls: 40
      minutes: 10
      runs_per_day: 200
    language: de
    memory: true
    enabled: true

  - name: propose-faults
    sentence: Proposes changes to the fault list as pull requests.
    trigger:
      kind: schedule
      cron: "0 3 * * 0"
    skill: lares-propose
    tools: [lares]
    output: [github_pr]
    budget:
      tool_calls: 80
      minutes: 20
      runs_per_day: 1
    language: en
    memory: true
    enabled: true

  - name: summarise-week
    sentence: Summarises the week on Sunday evening.
    trigger:
      kind: schedule
      cron: "0 18 * * 0"
    skill: lares-summary
    tools: [lares]
    output: [discord]
    budget:
      tool_calls: 20
      minutes: 5
      runs_per_day: 1
    language: de
    memory: false
    dormant: Waits for its skill.
"""

# Two entries copied from the engine's faults.yaml: one sentence with a dash
# before its reason, one without. The rest of the schema is there to be ignored.
FAULTS = """
faults:
  - name: channel_silence
    sentence: "Ein Kanal, den die Engine lange genug kennt und der sonst
      regelmäßig sendet, schweigt länger als das Fünffache der Sendepause, die
      er sonst in 19 von 20 Fällen einhält."
    unit: "× der üblichen Sendepause"
    kind: silence
    parameters:
      gap_factor: 5
      gap_quantile: 0.95
  - name: appliance_runtime
    sentence: "Ein Gerät zieht ununterbrochen länger Strom, als seine je Gerät
      erlaubte Laufzeit zulässt — vergessen eingeschaltet oder hängen geblieben."
    unit: "min"
    kind: duration
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
def execute(postgres: PostgresContainer) -> Iterator[Callable[..., None]]:
    """Write to the ledger directly, for the state a crashed pod leaves behind."""
    with psycopg.connect(_dsn(postgres), autocommit=True) as conn:

        def run(statement: str, *params: Any) -> None:
            conn.execute(statement, params)

        yield run


class Relay:
    """The cluster's mail relay as the trigger meets it: plaintext SMTP, one accepted sender.

    It keeps every message it took; `refuse` makes it answer the next DATA
    with that reply instead, as a relay whose upstream turned a mail away.
    """

    def __init__(self) -> None:
        self.messages: list[Envelope] = []
        self.refuse: str | None = None

    async def handle_MAIL(  # noqa: N802 — aiosmtpd's hook name
        self, _server: SMTP, _session: Session, envelope: Envelope, address: str, options: list[str]
    ) -> str:
        if address != ACCEPTED_SENDER:
            return "553 5.7.1 Sender address rejected: not owned by the account"
        envelope.mail_from = address
        envelope.mail_options.extend(options)
        return "250 OK"

    async def handle_DATA(  # noqa: N802 — aiosmtpd's hook name
        self, _server: SMTP, _session: Session, envelope: Envelope
    ) -> str:
        if self.refuse is not None:
            return self.refuse
        self.messages.append(envelope)
        return "250 OK"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="session")
def relay_server() -> Iterator[tuple[Relay, int]]:
    relay = Relay()
    port = _free_port()
    controller = Controller(relay, hostname="127.0.0.1", port=port)
    controller.start()
    try:
        yield relay, port
    finally:
        controller.stop()


@pytest.fixture
def relay(relay_server: tuple[Relay, int]) -> Relay:
    """The relay, emptied for this test."""
    server, _ = relay_server
    server.messages.clear()
    server.refuse = None
    return server


@pytest.fixture
def discord(respx_mock: respx.MockRouter) -> Any:
    """Discord's create-message endpoint on the home channel, as `fakes.fake_discord` keeps it."""
    from .fakes import fake_discord  # fakes imports this module's constants

    return fake_discord(respx_mock)


@pytest.fixture
def settings(
    postgres: PostgresContainer,
    nats_url: str,
    relay_server: tuple[Relay, int],
    tmp_path: Path,
) -> Settings:
    use_cases_file = tmp_path / "use-cases.yaml"
    use_cases_file.write_text(USE_CASES, encoding="utf-8")
    faults_file = tmp_path / "faults.yaml"
    faults_file.write_text(FAULTS, encoding="utf-8")
    _, relay_port = relay_server
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
        hook_secret=HOOK_SECRET,
        # An event run waits this long for its calls; the tests deliver them first.
        trace_wait_seconds=0.05,
        # The poll loop is exercised, not waited on.
        hermes_poll_seconds=0.01,
        alertmanager_url=ALERTMANAGER_URL,
        retry_delay_seconds=RETRY_DELAY_SECONDS,
        fetch_timeout_seconds=2.0,
        discord_bot_token=DISCORD_TOKEN,
        discord_home_channel=DISCORD_CHANNEL,
        smtp_host="127.0.0.1",
        smtp_port=relay_port,
        mail_from=MAIL_FROM,
        mail_to=MAIL_TO,
        faults_file=faults_file,
        dashboard_episode_url=DASHBOARD_EPISODE_URL,
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

    async def _publish(
        kind: str,
        severity: int,
        *,
        episode_id: int = 15510,
        fault: str = "appliance_runtime",
        subject: str = "2/1/197",
    ) -> None:
        body = {
            "episode_id": episode_id,
            "fault": fault,
            "subject": subject,
            "severity": severity,
            "kind": kind,
            "time": "2026-09-25T14:20:00+00:00",
        }
        await stream.publish(f"episode.{kind}", json.dumps(body).encode())

    return _publish


@dataclass(frozen=True)
class Service:
    """The service as `main.py` wires it: one ledger, one tally, both paths on it."""

    consumer: EpisodeConsumer
    client: httpx.AsyncClient
    metrics: Metrics


@pytest_asyncio.fixture
async def service(
    settings: Settings, stream: Any, relay: Relay, discord: Any
) -> AsyncIterator[Service]:
    """Real ledger, real consumer, the hook receiver in process, the edges faked."""
    metrics = Metrics()
    ledger = Ledger(settings)
    await ledger.open()
    hermes = HermesClient(settings)
    alertmanager = Alertmanager(settings, metrics)
    use_cases = load_use_cases(settings.use_cases_file)
    deliveries = build_deliveries(settings, use_cases, metrics)
    turns = TurnRuns(use_cases, ledger, hermes, metrics)
    runs = EventRuns(
        use_cases,
        ledger,
        hermes,
        alertmanager,
        deliveries,
        metrics,
        retry_delay_seconds=settings.retry_delay_seconds,
        traces=turns,
        trace_wait_seconds=settings.trace_wait_seconds,
    )
    episode_consumer = EpisodeConsumer(settings, runs, metrics)
    await episode_consumer.connect()
    app = create_app(turns, settings.hook_secret, metrics)
    # In-process: the ASGI app behind a real HTTP client, no port and no server.
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://trigger")
    try:
        yield Service(episode_consumer, client, metrics)
    finally:
        await client.aclose()
        await episode_consumer.close()
        await hermes.aclose()
        await alertmanager.aclose()
        await deliveries.aclose()
        await ledger.close()


@pytest.fixture
def consumer(service: Service) -> tuple[EpisodeConsumer, Metrics]:
    """The episode path of the service."""
    return service.consumer, service.metrics


@pytest.fixture
def receiver(service: Service) -> tuple[httpx.AsyncClient, Metrics]:
    """The hook receiver as Hermes reaches it: HTTP in, the real ledger behind it."""
    return service.client, service.metrics
