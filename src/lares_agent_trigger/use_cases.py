"""The declared use cases: one schema and one loader, for rendering and for runtime.

The file lives in lares and is mounted here; the generator renders the harness
configuration from the same models, which is why schema and loader sit in this
package rather than in the generator alone. Beside the use cases it declares
the tool servers they may name and where the harness reports every finished
turn. A file that does not validate is refused at startup — a half-read
catalogue would start runs nobody declared.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    model_validator,
)

from .events import EventKind

# The gateway mints cron job ids itself, so a job the trigger manages carries
# its use case in the name: `lares:propose-faults`.
MANAGED_JOB_PREFIX = "lares:"

# Where a completed run's output is delivered; `deliveries.py` holds one delivery
# per target it can serve, and `stored` is the ledger row itself.
OutputTarget = Literal[
    "stored",
    "discord",
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


class UseCase(_Strict):
    """One declared entry: what starts it, what it may see, and what it costs."""

    name: str = Field(min_length=1)
    sentence: str = Field(min_length=1)
    trigger: Trigger
    # What an event or schedule run is told to run. A chat names none: the
    # person's message is the assignment.
    skill: str | None = Field(default=None, min_length=1)
    # The tool servers the use case may see; the harness gets them as
    # `mcp-<name>` and the bridge's own allowlist is the hard ceiling.
    tools: tuple[str, ...] = Field(min_length=1)
    output: tuple[OutputTarget, ...] = Field(min_length=1)
    budget: Budget
    # ISO 639-1; what the answer is written in, not what the skill is written in.
    language: str = Field(min_length=2, max_length=2)
    memory: bool
    model: str | None = None
    # A use case either runs or says why it does not. `enabled: false` is not a
    # state: switching one off is a dormant entry with a reason.
    enabled: bool | None = None
    dormant: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _state_is_declared(self) -> UseCase:
        if self.enabled is None and self.dormant is None:
            raise ValueError(
                f"use case {self.name}: exactly one of `enabled: true` or `dormant: <reason>`"
            )
        if self.enabled is not None and self.dormant is not None:
            raise ValueError(
                f"use case {self.name}: exactly one of `enabled` or `dormant`, not both"
            )
        if self.enabled is False:
            raise ValueError(
                f"use case {self.name}: switching a use case off is `dormant: <reason>`"
            )
        return self

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
    def is_enabled(self) -> bool:
        return self.enabled is True

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


class BridgeServer(_Strict):
    """A tool server on the bridge: one machine client, its key and its allowlist.

    `tools` (names or fnmatch globs) is both the bridge's allowlist for the
    client, the hard ceiling, and the harness's include list on top of it.
    """

    kind: Literal["bridge"] = "bridge"
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    # The bridge's machine client the key maps to, and where the harness holds the key.
    client: str = Field(min_length=1)
    key_env: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)
    access: ToolAccess
    tools: tuple[str, ...] = Field(min_length=1)


class GitHubServer(_Strict):
    """The official GitHub MCP server, which the harness spawns as a process of its own.

    It signs in as a GitHub App that may only read, minting and renewing its
    own installation tokens from the App's key, and runs read-only with the
    `toolsets` named here; `tools` is the harness's include list on top. A
    GitHub write is a delivery of the trigger, never a tool, so the server
    only reads.
    """

    kind: Literal["github"]
    name: str = Field(min_length=1)
    # The server binary and the App's key, as paths inside the harness container.
    command: str = Field(min_length=1)
    app_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    private_key_file: str = Field(min_length=1)
    toolsets: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)
    access: ToolAccess
    tools: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _only_reads(self) -> GitHubServer:
        if self.access != "read":
            raise ValueError(
                f"tool server {self.name}: the GitHub server only reads; a GitHub write is "
                "a delivery of the trigger"
            )
        return self


ToolServer = BridgeServer | GitHubServer


def _server_kind(value: Any) -> str:
    # A bridge entry may leave its kind out.
    if isinstance(value, Mapping):
        return str(value.get("kind", "bridge"))
    return str(value.kind)


_DeclaredServer = Annotated[
    Annotated[BridgeServer, Tag("bridge")] | Annotated[GitHubServer, Tag("github")],
    Discriminator(_server_kind),
]


class LedgerHook(_Strict):
    """Where the harness reports every finished turn, so each one gets its ledger row."""

    trigger_url: str = Field(min_length=1)
    # The harness's environment variable holding the HMAC secret both sides share.
    secret_env: str = Field(min_length=1)


class _File(_Strict):
    ledger_hook: LedgerHook
    tool_servers: list[_DeclaredServer] = Field(min_length=1)
    use_cases: list[UseCase] = Field(min_length=1)


@dataclass(frozen=True)
class UseCaseFile:
    """The whole declaration: use cases and tool servers keyed by name, in file order."""

    use_cases: dict[str, UseCase]
    tool_servers: dict[str, ToolServer]
    ledger_hook: LedgerHook


def chat_use_case(use_cases: Mapping[str, UseCase]) -> UseCase | None:
    """The use case a chat turn belongs to; the loader allows one."""
    return next((use_case for use_case in use_cases.values() if use_case.is_chat), None)


def scheduled_use_case(use_cases: Mapping[str, UseCase], job_name: str) -> UseCase | None:
    """The use case a cron job runs, known by the job's managed name."""
    if not job_name.startswith(MANAGED_JOB_PREFIX):
        return None
    use_case = use_cases.get(job_name.removeprefix(MANAGED_JOB_PREFIX))
    return use_case if use_case is not None and use_case.is_schedule else None


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
    clients: dict[str, str] = {}
    for server in parsed.tool_servers:
        if server.name in servers:
            raise ValueError(f"{path}: tool server {server.name} is declared twice")
        servers[server.name] = server
        if not isinstance(server, BridgeServer):
            continue
        if server.client in clients:
            raise ValueError(
                f"{path}: tool server {server.name}: client {server.client} is already "
                f"the client of {clients[server.client]}"
            )
        clients[server.client] = server.name
    _check_one_server_per_tool(path, servers)

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
    return UseCaseFile(use_cases=by_name, tool_servers=servers, ledger_hook=parsed.ledger_hook)


def _check_one_server_per_tool(path: Path, servers: Mapping[str, ToolServer]) -> None:
    """Refuse a tool two bridge servers grant, so each tool keeps the access of its one server.

    An entry of one server, read as a name, must match no entry of another;
    for globs that catches the overlaps a declaration can show (`get_*` over
    `get_write_request`), not every pair two globs could share. Only the
    bridge's servers share tools; GitHub's are GitHub's own.
    """
    bridge = [server for server in servers.values() if isinstance(server, BridgeServer)]
    for server in bridge:
        for other in bridge:
            if other is server:
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
