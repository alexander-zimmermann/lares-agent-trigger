"""The declared use cases: one schema and one loader, for rendering and for runtime.

The file lives in lares and is mounted here; the generator renders the harness
configuration from the same models, which is why schema and loader sit in this
package rather than in the generator alone. Beside the use cases it declares
the tool servers they may name, the gateway every one of them sits behind, the
connectors a person signs in to on the gateway's public address, where the
harness reports every finished turn, the share of traces every service of
the platform keeps, and the scripts the harness's cron runs without a model.
A file that does not validate is refused at startup — a half-read catalogue
would start runs nobody declared.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated, ClassVar, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from .events import EventKind

# The harness mints cron job ids itself, so a job the trigger manages carries
# its use case or script in the name: `lares:propose-faults`.
MANAGED_JOB_PREFIX = "lares:"

# ISO 639-1 to the word an instruction line uses. A code without an entry is
# passed through, so a new language is a file change, not a code change.
_LANGUAGE_NAMES = {"de": "German", "en": "English"}

# Where a completed run's output is delivered; `deliveries.py` holds one delivery
# per target it can serve, and `stored` is the ledger row itself. `web_chat` is
# where the harness answers a chat besides Discord; no delivery serves it.
OutputTarget = Literal[
    "stored",
    "discord",
    "web_chat",
    "mail",
    "alert",
    "github_pr",
    "github_issue",
    "github_comment",
    "wiki_page",
]


# Where an event comes from. The trigger consumes the episode stream; the other
# sources are paths a dormant use case may wait for, never an enabled one.
EventSource = Literal["episode", "alert", "pull_request", "ets_export", "new_device"]
CONSUMED_EVENT_SOURCES: frozenset[EventSource] = frozenset({"episode"})

# Where a tool server may be granted: `read` anywhere, `write` only to the schedule
# use cases that name it, `request` (a request a person approves) only to the chat,
# `memory` (a use case's own memory) only to the use cases that keep one.
ToolAccess = Literal["read", "write", "request", "memory"]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EventTrigger(_Strict):
    """A run started by an event on the bus.

    `filter` maps an event kind to the lowest severity that deserves a run; a
    kind left out never runs. That is the whole filter: `{appeared: 2,
    escalated: 2}` explains an episode that opens at 2 or worse and again when
    it rises, and never explains one that ended. Only an episode event has
    kinds and severities, so only an episode trigger names a filter.
    """

    kind: Literal["event"]
    source: EventSource
    filter: dict[EventKind, int] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _episode_names_its_filter(self) -> EventTrigger:
        if (self.source == "episode") != (self.filter is not None):
            raise ValueError("filter: an episode trigger names one, any other source none")
        return self

    def wants(self, event_kind: EventKind, severity: int) -> bool:
        """True when this use case wants a run for that kind at that severity."""
        minimum = self.filter.get(event_kind) if self.filter is not None else None
        return minimum is not None and severity >= minimum


class ScheduleTrigger(_Strict):
    """A run started by the harness's own cron, on the rendered expression."""

    kind: Literal["schedule"]
    cron: str


class MessageTrigger(_Strict):
    """A run started by a person in conversation; the harness owns the turn."""

    kind: Literal["message"]


Trigger = Annotated[
    EventTrigger | ScheduleTrigger | MessageTrigger,
    Field(discriminator="kind"),
]


class Budget(_Strict):
    """What one run of this use case may spend, and how often it may run."""

    tool_calls: int = Field(gt=0)
    minutes: int = Field(gt=0)
    runs_per_day: int = Field(gt=0)


class _Declared(_Strict):
    """An entry that either runs or says why it does not."""

    # What a refusal calls the entry: `use case` or `script`.
    _noun: ClassVar[str]

    name: str = Field(min_length=1)
    sentence: str = Field(min_length=1)
    # `enabled: false` is not a state: switching an entry off is a dormant one with a reason.
    enabled: bool | None = None
    dormant: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _state_is_declared(self) -> _Declared:
        noun = self._noun
        if self.enabled is None and self.dormant is None:
            raise ValueError(
                f"{noun} {self.name}: exactly one of `enabled: true` or `dormant: <reason>`"
            )
        if self.enabled is not None and self.dormant is not None:
            raise ValueError(f"{noun} {self.name}: exactly one of `enabled` or `dormant`, not both")
        if self.enabled is False:
            raise ValueError(f"{noun} {self.name}: switching a {noun} off is `dormant: <reason>`")
        return self

    @property
    def is_enabled(self) -> bool:
        return self.enabled is True


class UseCase(_Declared):
    """One declared entry: what starts it, what it may see, and what it costs."""

    _noun = "use case"

    trigger: Trigger
    # What an event or schedule run is told to run. A chat names none: the
    # person's message is the assignment.
    skill: str | None = Field(default=None, min_length=1)
    # The tool servers the use case may see; the harness gets them as
    # `mcp-<name>`, and each server's gateway route serves its tools only.
    tools: tuple[str, ...] = Field(min_length=1)
    output: tuple[OutputTarget, ...] = Field(min_length=1)
    budget: Budget
    # ISO 639-1; what the answer is written in, not what the skill is written in.
    language: str = Field(min_length=2, max_length=2)
    memory: bool
    model: str | None = None

    @model_validator(mode="after")
    def _skill_is_named(self) -> UseCase:
        if self.skill is None and not isinstance(self.trigger, MessageTrigger):
            raise ValueError(
                f"use case {self.name}: a {self.trigger.kind} use case names its skill"
            )
        return self

    @model_validator(mode="after")
    def _source_is_consumed(self) -> UseCase:
        trigger = self.trigger
        if (
            self.enabled
            and isinstance(trigger, EventTrigger)
            and trigger.source not in CONSUMED_EVENT_SOURCES
        ):
            raise ValueError(
                f"use case {self.name}: the trigger consumes episode events only; "
                f"a {trigger.source} use case stays dormant until its path is built"
            )
        return self

    @property
    def event_trigger(self) -> EventTrigger | None:
        """The event trigger of an enabled use case, or None for every other one."""
        if self.is_enabled and isinstance(self.trigger, EventTrigger):
            return self.trigger
        return None

    @property
    def is_chat(self) -> bool:
        """True for an enabled use case a person starts by writing in chat."""
        return self.is_enabled and isinstance(self.trigger, MessageTrigger)

    @property
    def delivers(self) -> bool:
        """True when its output goes somewhere besides its own row."""
        return any(target != "stored" for target in self.output)

    @property
    def is_schedule(self) -> bool:
        """True for an enabled use case the harness's cron starts."""
        return self.is_enabled and isinstance(self.trigger, ScheduleTrigger)


class Script(_Declared):
    """A script the harness's cron runs without a model, its output the job's.

    The harness delivers the output itself, so a script has no tool list, no
    budget and no row in the ledger; it is a managed job like a schedule use
    case's, named `lares:<name>`.
    """

    _noun = "script"

    trigger: ScheduleTrigger
    # The file under the default profile's `scripts/` directory.
    script: str = Field(min_length=1)
    # Where the harness posts the output: `discord` for its home channel.
    deliver: str = Field(min_length=1)


class Credential(_Strict):
    """What the gateway attaches on every call: a key of a Secret beside it, in a header.

    On `Authorization` the value goes as a bearer token, on any other header as it is.
    """

    secret: str = Field(min_length=1)
    key: str = Field(min_length=1)
    header: str = Field(min_length=1)


class ToolServer(_Strict):
    """An MCP endpoint behind its gateway route: the bridge's and a vendor's alike.

    The gateway attaches `credential`; the harness calls the route with its
    profile's gateway key. `tools` (names or `*`/`?` globs) is the route's tool
    list and the harness's include list on top. The gateway names the caller to a server
    in the cluster: the route, and the person behind the key; a server
    outside it, such as a vendor's hosted one, gets the credential only.
    """

    kind: Literal["http"]
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    credential: Credential
    timeout_seconds: int = Field(gt=0)
    access: ToolAccess
    tools: tuple[str, ...] = Field(min_length=1)


class Connector(_Strict):
    """A person's own MCP client, signed in through Authentik on a public path of the gateway.

    `issuer` is the Authentik application's issuer, a variable the gateway
    expands at start where it differs per cluster; `client_id` is the
    application's pre-registered client, the tokens' audience. The gateway
    calls `url` with `credential` and serves `tools` to the persons the
    person declaration names.
    """

    name: str = Field(min_length=1)
    path: str = Field(pattern=r"^/")
    issuer: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    credential: Credential
    tools: tuple[str, ...] = Field(min_length=1)


class Gateway(_Strict):
    """The one door between the harness and every tool server.

    `url` is where the harness reaches it, `otlp_endpoint` the OTLP/HTTP
    receiver it sends a span of every call to, and `key_env` the variable
    every profile holds its gateway key in: one key per profile, which opens
    the routes of the servers the profile holds and no other.
    """

    url: str = Field(min_length=1)
    otlp_endpoint: str = Field(min_length=1)
    key_env: str = Field(min_length=1)


class LedgerHook(_Strict):
    """Where the harness reports every finished turn, so each one gets its ledger row."""

    trigger_url: str = Field(min_length=1)
    # The harness's environment variable holding the HMAC secret both sides share.
    secret_env: str = Field(min_length=1)


class _File(_Strict):
    ledger_hook: LedgerHook
    gateway: Gateway
    tool_servers: list[ToolServer] = Field(min_length=1)
    connectors: list[Connector] = []
    use_cases: list[UseCase] = Field(min_length=1)
    scripts: list[Script] = []


@dataclass(frozen=True)
class UseCaseFile:
    """The whole declaration by name in file order: use cases, tool servers, connectors, scripts."""

    use_cases: dict[str, UseCase]
    tool_servers: dict[str, ToolServer]
    connectors: dict[str, Connector]
    scripts: dict[str, Script]
    ledger_hook: LedgerHook
    gateway: Gateway


def chat_use_case(use_cases: Mapping[str, UseCase]) -> UseCase | None:
    """The use case a chat turn belongs to; the loader allows one."""
    return next((use_case for use_case in use_cases.values() if use_case.is_chat), None)


def scheduled_use_case(use_cases: Mapping[str, UseCase], job_name: str) -> UseCase | None:
    """The use case a cron job runs, known by the job's managed name."""
    if not job_name.startswith(MANAGED_JOB_PREFIX):
        return None
    use_case = use_cases.get(job_name.removeprefix(MANAGED_JOB_PREFIX))
    return use_case if use_case is not None and use_case.is_schedule else None


def scheduled_script(scripts: Mapping[str, Script], job_name: str) -> Script | None:
    """The script a cron job runs, known by the job's managed name."""
    if not job_name.startswith(MANAGED_JOB_PREFIX):
        return None
    script = scripts.get(job_name.removeprefix(MANAGED_JOB_PREFIX))
    return script if script is not None and script.is_enabled else None


def load_use_cases(path: Path) -> dict[str, UseCase]:
    """Read and validate the use-case file; the use cases keyed by name."""
    return load_use_case_file(path).use_cases


def load_use_case_file(path: Path) -> UseCaseFile:
    """Read and validate the use-case file.

    Every failure — missing file, unparsable YAML, a field the schema does not
    know, two entries of one name, two chats, a tool server an enabled use case
    may not hold — is a ``ValueError`` naming the file, so the startup log and
    the pre-commit hook say what to fix.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"{path}: cannot be read ({exc})") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: is not valid YAML ({exc})") from exc

    try:
        parsed = _File.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"{path}: {exc}") from exc

    servers: dict[str, ToolServer] = {}
    for server in parsed.tool_servers:
        if server.name in servers:
            raise ValueError(f"{path}: tool server {server.name} is declared twice")
        servers[server.name] = server
    _check_one_server_per_tool(path, servers)
    connectors = _connectors(path, parsed.connectors, servers)

    by_name: dict[str, UseCase] = {}
    for use_case in parsed.use_cases:
        if use_case.name in by_name:
            raise ValueError(f"{path}: use case {use_case.name} is declared twice")
        by_name[use_case.name] = use_case
        if use_case.is_enabled:
            _check_grants(path, use_case, servers)
            _check_cron_job(path, use_case)

    # The harness has one chat surface, so a turn it reports belongs to one use case.
    chats = [use_case.name for use_case in by_name.values() if use_case.is_chat]
    if len(chats) > 1:
        raise ValueError(f"{path}: only one enabled message use case, not {', '.join(chats)}")
    _check_shared_memory(path, by_name, servers)

    # A script's job carries the same prefix as a use case's, so the names share one space.
    scripts: dict[str, Script] = {}
    for script in parsed.scripts:
        if script.name in by_name:
            raise ValueError(f"{path}: script {script.name}: the name is already a use case's")
        if script.name in scripts:
            raise ValueError(f"{path}: script {script.name} is declared twice")
        scripts[script.name] = script
    return UseCaseFile(
        use_cases=by_name,
        tool_servers=servers,
        connectors=connectors,
        scripts=scripts,
        ledger_hook=parsed.ledger_hook,
        gateway=parsed.gateway,
    )


def _connectors(
    path: Path, declared: list[Connector], servers: Mapping[str, ToolServer]
) -> dict[str, Connector]:
    """The connectors by name; each route keeps a name and a path of its own."""
    routes = {f"/mcp/{name}": f"the route of tool server {name}" for name in servers}
    connectors: dict[str, Connector] = {}
    for connector in declared:
        if connector.name in servers or connector.name in connectors:
            raise ValueError(
                f"{path}: connector {connector.name}: the name is already that of another route"
            )
        if connector.path in routes:
            raise ValueError(
                f"{path}: connector {connector.name}: path {connector.path} is "
                f"{routes[connector.path]}"
            )
        routes[connector.path] = f"the path of connector {connector.name}"
        connectors[connector.name] = connector
    return connectors


def _check_one_server_per_tool(path: Path, servers: Mapping[str, ToolServer]) -> None:
    """Refuse a tool two servers on one endpoint grant, so it keeps the access of its one server.

    An entry of one server, read as a name, must match no entry of another on
    the same URL; for globs that catches the overlaps a declaration can show
    (`get_*` over `get_write_request`), not every pair two globs could share.
    Servers on other endpoints serve tools of their own.
    """
    for server in servers.values():
        for other in servers.values():
            if other is server or other.url != server.url:
                continue
            for pattern in server.tools:
                for entry in other.tools:
                    if fnmatchcase(entry, pattern):
                        raise ValueError(
                            f"{path}: tool server {server.name}: {pattern} also grants "
                            f"{entry} of tool server {other.name}; a tool sits on one server only"
                        )


def _check_grants(path: Path, use_case: UseCase, servers: Mapping[str, ToolServer]) -> None:
    """Refuse a tool server an enabled use case names but may not hold.

    A dormant use case may name servers still to be built: it renders nothing.
    """
    for name in use_case.tools:
        server = servers.get(name)
        if server is None:
            raise ValueError(
                f"{path}: use case {use_case.name}: tools: no tool server named {name}"
            )
        if server.access == "write" and not use_case.is_schedule:
            raise ValueError(
                f"{path}: use case {use_case.name}: tools: {name} writes and is granted "
                "to schedule use cases only"
            )
        if server.access == "write":
            raise ValueError(
                f"{path}: use case {use_case.name}: tools: {name} writes, and the Jobs API "
                "gives a cron job no tool list of its own; it stays dormant until the "
                "harness can carry one"
            )
        if server.access == "request" and not use_case.is_chat:
            raise ValueError(
                f"{path}: use case {use_case.name}: tools: {name} places requests and is "
                "granted to the chat only"
            )
        if server.access == "memory" and not use_case.memory:
            raise ValueError(
                f"{path}: use case {use_case.name}: tools: {name} holds a use case's memory, "
                f"and {use_case.name} keeps none"
            )


def _check_shared_memory(
    path: Path, use_cases: Mapping[str, UseCase], servers: Mapping[str, ToolServer]
) -> None:
    """Refuse a memory server on a surface a use case without a memory shares.

    The harness hands a surface's servers to every run on it: one list for
    all event runs, and one for all cron jobs, the Jobs API giving a job no
    list of its own. A memory server one of them names reaches them all, so
    every enabled use case on that surface has to keep a memory.
    """
    enabled = [use_case for use_case in use_cases.values() if use_case.is_enabled]
    surfaces = (
        ("event run", [use_case for use_case in enabled if use_case.event_trigger is not None]),
        ("cron job", [use_case for use_case in enabled if use_case.is_schedule]),
    )
    for run, sharing in surfaces:
        for holder in sharing:
            for name in holder.tools:
                if servers[name].access != "memory":
                    continue
                for other in sharing:
                    if not other.memory:
                        raise ValueError(
                            f"{path}: use case {other.name} keeps no memory, and every {run} "
                            f"sees {name}, which {holder.name} holds"
                        )


def _check_cron_job(path: Path, use_case: UseCase) -> None:
    """Refuse a schedule use case its managed job could not carry.

    The Jobs API creates a job with its name, schedule, prompt, skills and
    delivery, and with nothing else: no model of its own.
    """
    if use_case.is_schedule and use_case.model is not None:
        raise ValueError(
            f"{path}: use case {use_case.name}: model: the Jobs API gives a cron job no "
            "model of its own; a schedule use case runs on the harness's default"
        )


def language_name(language: str) -> str:
    """The word an instruction line uses for an ISO 639-1 code."""
    return _LANGUAGE_NAMES.get(language, language)
