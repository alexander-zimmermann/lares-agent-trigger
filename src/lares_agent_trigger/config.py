"""Runtime configuration: NATS from the shared base, plus the ledger and the harness."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from nats_bridge_core import NatsSettings
from pydantic import Field, model_validator


class Settings(NatsSettings):
    """Everything the trigger needs to read an event and record what it did.

    Secrets arrive as mounted files in the cluster (`*_file`); the literal
    fields exist so tests and local runs can set them directly.
    """

    # The stream the engine publishes episode events on, and the durable pull
    # consumer the nats-operator creates for this service.
    nats_stream_name: str = "EPISODE"
    consumer_name: str = "agent-trigger"
    # One message at a time: a run holds the fetch for as long as it takes, and
    # ten runs a day never need concurrency.
    fetch_timeout_seconds: float = 5.0

    # The declared use cases, mounted from lares as a hashed ConfigMap.
    use_cases_file: Path = Path("/etc/lares-agent-trigger/use-cases.yaml")

    # Ledger: the one table this service writes.
    db_host: str
    db_port: int = 5432
    db_name: str = "homelab"
    db_username: str = ""
    db_password: str = Field(default="", repr=False)
    db_username_file: Path | None = None
    db_password_file: Path | None = None
    db_pool_min: int = 1
    db_pool_max: int = 4
    # Which midnight the daily cap counts from. The house thinks in local days.
    timezone: str = "Europe/Berlin"

    # Harness: the Runs API of the Hermes gateway.
    hermes_url: str = "http://hermes.agents.svc.cluster.local:8642"
    hermes_api_key: str = Field(default="", repr=False)
    hermes_api_key_file: Path | None = None
    hermes_poll_seconds: float = 5.0
    hermes_request_timeout_seconds: float = 30.0
    # A transient failure is started once more after this long; a setting so
    # tests can shorten it.
    retry_delay_seconds: float = 300.0

    # Where a run that failed for good is reported as AgentRunFailed.
    alertmanager_url: str = "http://prometheus-alertmanager.prometheus.svc.cluster.local:9093"
    alertmanager_request_timeout_seconds: float = 10.0

    @model_validator(mode="after")
    def _resolve_secret_files(self) -> Settings:
        if self.db_username_file:
            self.db_username = self.db_username_file.read_text(encoding="utf-8").strip()
        if self.db_password_file:
            self.db_password = self.db_password_file.read_text(encoding="utf-8").strip()
        if self.hermes_api_key_file:
            self.hermes_api_key = self.hermes_api_key_file.read_text(encoding="utf-8").strip()
        missing = [
            name
            for name, value in (
                ("DB_USERNAME", self.db_username),
                ("DB_PASSWORD", self.db_password),
                ("HERMES_API_KEY", self.hermes_api_key),
            )
            if not value
        ]
        if missing:
            joined = ", ".join(f"{name} or {name}_FILE" for name in missing)
            raise ValueError(f"missing required configuration: {joined}")
        return self

    @property
    def db_dsn(self) -> str:
        # URL-encode user and password — a generated password routinely carries
        # `/`, `@`, `:` or `+`, which break psycopg's URI parser.
        user = quote(self.db_username, safe="")
        secret = quote(self.db_password, safe="")
        return f"postgresql://{user}:{secret}@{self.db_host}:{self.db_port}/{self.db_name}"
