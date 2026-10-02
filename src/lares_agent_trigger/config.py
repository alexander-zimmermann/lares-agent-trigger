"""Runtime configuration: NATS from the shared base, then ledger, harness, delivery, hook, API."""

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

    # The declared use cases and the cron job set rendered from them, both
    # mounted from lares as hashed ConfigMaps.
    use_cases_file: Path = Path("/etc/lares-agent-trigger/use-cases.yaml")
    cron_jobs_file: Path = Path("/etc/lares-agent-trigger/cron-jobs.yaml")

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
    # A reconcile of the cron jobs the gateway did not answer is tried again
    # after this long, until one goes through.
    reconcile_retry_seconds: float = 60.0

    # Where a run that failed for good is reported as AgentRunFailed.
    alertmanager_url: str = "http://prometheus-alertmanager.prometheus.svc.cluster.local:9093"
    alertmanager_request_timeout_seconds: float = 10.0

    # Delivery. Each target's settings are required only when an enabled use
    # case declares that target; the check runs when the deliveries are built.
    # Discord: the bot the harness chats as, posting into its home channel.
    discord_bot_token: str = Field(default="", repr=False)
    discord_bot_token_file: Path | None = None
    discord_home_channel: str = ""
    discord_request_timeout_seconds: float = 10.0
    # Mail: plaintext SMTP to the cluster's relay, from its one accepted sender.
    smtp_host: str = ""
    smtp_port: int = 25
    smtp_timeout_seconds: float = 30.0
    mail_from: str = ""
    mail_to: str = ""
    # The engine's fault list, for the sentence a mail names in its subject.
    faults_file: Path = Path("/etc/lares-agent-trigger/faults.yaml")
    # The dashboard row of an episode; `{episode_id}` and `{fault}` are filled in.
    dashboard_episode_url: str = ""

    # The receiver the harness's outbound hook posts every finished chat and
    # cron turn to, and the HMAC secret both sides share.
    http_port: int = 8080
    hook_secret: str = Field(default="", repr=False)
    hook_secret_file: Path | None = None
    # How long an event run waits, once over, for its model calls to arrive
    # through the hook before it is recorded without them.
    trace_wait_seconds: float = 5.0
    # The key the trigger's own API takes on the same port: the bridge holds
    # it too and forwards its start_run tool with it.
    api_key: str = Field(default="", repr=False)
    api_key_file: Path | None = None

    @model_validator(mode="after")
    def _resolve_secret_files(self) -> Settings:
        if self.db_username_file:
            self.db_username = self.db_username_file.read_text(encoding="utf-8").strip()
        if self.db_password_file:
            self.db_password = self.db_password_file.read_text(encoding="utf-8").strip()
        if self.hermes_api_key_file:
            self.hermes_api_key = self.hermes_api_key_file.read_text(encoding="utf-8").strip()
        if self.discord_bot_token_file:
            self.discord_bot_token = self.discord_bot_token_file.read_text(encoding="utf-8").strip()
        if self.hook_secret_file:
            self.hook_secret = self.hook_secret_file.read_text(encoding="utf-8").strip()
        if self.api_key_file:
            self.api_key = self.api_key_file.read_text(encoding="utf-8").strip()
        missing = [
            name
            for name, value in (
                ("DB_USERNAME", self.db_username),
                ("DB_PASSWORD", self.db_password),
                ("HERMES_API_KEY", self.hermes_api_key),
                ("HOOK_SECRET", self.hook_secret),
                ("API_KEY", self.api_key),
            )
            if not value
        ]
        if missing:
            joined = ", ".join(f"{name} or {name}_FILE" for name in missing)
            raise ValueError(f"missing required configuration: {joined}")
        if len(self.api_key) < 32:
            raise ValueError("API_KEY must be at least 32 characters")
        return self

    @property
    def db_dsn(self) -> str:
        # URL-encode user and password — a generated password routinely carries
        # `/`, `@`, `:` or `+`, which break psycopg's URI parser.
        user = quote(self.db_username, safe="")
        secret = quote(self.db_password, safe="")
        return f"postgresql://{user}:{secret}@{self.db_host}:{self.db_port}/{self.db_name}"
