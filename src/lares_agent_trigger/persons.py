"""The person declaration: the model sources, the default profile's source, and the persons.

The file lives in lares beside the use-case file and is mounted here; the
generator renders every profile's model and fallback from it, and each
person's profile, and the trigger prices every model call by the source step
that served it. Schema and loader sit in this package for the same reason the
use cases' do: rendering and runtime read one schema.

A source is a provider and a model, how its account relates to the default
profile's, and what bounds its spend: a `subscription` costs nothing per call,
a `capped` step is metered against a hard limit and names its price in USD per
million tokens. Its fallback steps take over when it fails.

A person is a household member known by a handle, never by a legal name, an
e-mail or a Discord id: Git holds their rights, the sign-in provider their
identity. A person either runs (`enabled: true`) or says why not
(`dormant: <reason>`); a dormant person renders nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .use_cases import BridgeServer, UseCaseFile, chat_use_case

# How a source's spend is bounded: a flat plan, or metered use under a hard limit.
Bound = Literal["subscription", "capped"]
# Whose login a source runs on: the default profile's own, or the person's alone.
Account = Literal["shared-with-default", "per-person"]

# The harness's name for the machines' profile, which event runs and cron run on.
DEFAULT_PROFILE = "default"
# The provider of a ChatGPT login: a profile's own source, never a fallback.
_CODEX = "openai-codex"
# Its login state lands in the default profile's credentials whichever profile signs in.
_ANTHROPIC = "anthropic"
# The variable each provider reads its key from; a login keeps its token on the
# data volume and reads none.
_PROVIDER_KEYS: dict[str, str | None] = {_CODEX: None, "openai-api": "OPENAI_API_KEY"}
_PER_MILLION = Decimal(1_000_000)

# A handle names the person's Hermes profile and the volumes its files are
# mounted from, so it is a profile name that is also part of a DNS label.
_HANDLE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
# Profile names Hermes keeps for itself or a system binary.
_RESERVED_PROFILES = frozenset({"hermes", "test", "tmp", "root", "sudo"})


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Price(_Strict):
    """What a metered step charges, in USD per million tokens."""

    input: Decimal = Field(ge=0)
    # A cache read; a cache write is fresh input.
    cached_input: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)

    def cost(self, tokens_in: int, tokens_cached: int, tokens_out: int) -> Decimal:
        """What one call costs: `tokens_in` counts its cache reads, which `tokens_cached` are."""
        return (
            (tokens_in - tokens_cached) * self.input
            + tokens_cached * self.cached_input
            + tokens_out * self.output
        ) / _PER_MILLION


class SourceStep(_Strict):
    """One provider and model a source runs on: the source itself, or one of its fallbacks."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    bound: Bound | None = None
    price: Price | None = None

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def key_env(self) -> str | None:
        """The variable this step's key is read from; none for a login."""
        return _PROVIDER_KEYS[self.provider]

    @model_validator(mode="after")
    def _metered_steps_name_their_price(self) -> SourceStep:
        if self.bound == "capped" and self.price is None:
            raise ValueError(f"{self.label} is capped and names no `price`")
        if self.bound == "subscription" and self.price is not None:
            raise ValueError(
                f"{self.label} is a subscription and costs nothing per call; drop its `price`"
            )
        return self


class ModelSource(SourceStep):
    """A named source a profile runs on, and the steps that take over when it fails."""

    name: str = Field(min_length=1)
    account: Account
    fallback: tuple[SourceStep, ...] = ()

    @property
    def steps(self) -> tuple[SourceStep, ...]:
        """The source itself, then its fallbacks in order."""
        return (self, *self.fallback)

    @property
    def key_envs(self) -> tuple[str, ...]:
        """The variables the steps read their keys from, once each, in order."""
        keys = (step.key_env for step in self.steps)
        return tuple(dict.fromkeys(key for key in keys if key is not None))

    @model_validator(mode="after")
    def _no_fallback_on_a_codex_login(self) -> ModelSource:
        for step in self.fallback:
            if step.provider == _CODEX:
                raise ValueError(
                    f"model source {self.name}: a fallback never runs on {_CODEX}; "
                    f"{step.label} would draw on the Codex login it stands in for"
                )
        return self


class DefaultProfile(_Strict):
    """The machines' profile: event runs and cron."""

    model: str = Field(min_length=1)


class Person(_Strict):
    """A declared household member: the handle and the rights, never the identity."""

    # The handle: the profile's name, the bridge client's and the sign-in's.
    name: str = Field(min_length=1)
    # The default profile's bot routes the person's Discord messages to their
    # profile; `admin` also runs every slash command.
    discord: Literal[True, "admin"] | None = None
    # A subset of the chat use case's tool servers.
    tools: tuple[str, ...] = Field(min_length=1)
    # The model source the profile runs on.
    model: str = Field(min_length=1)
    # Whether the ledger keeps the answers' text, or the numbers only.
    keep_answers: bool = False
    enabled: bool | None = None
    dormant: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _state_is_declared(self) -> Person:
        if self.enabled is None and self.dormant is None:
            raise ValueError(
                f"person {self.name}: exactly one of `enabled: true` or `dormant: <reason>`"
            )
        if self.enabled is not None and self.dormant is not None:
            raise ValueError(f"person {self.name}: exactly one of `enabled` or `dormant`, not both")
        if self.enabled is False:
            raise ValueError(f"person {self.name}: switching a person off is `dormant: <reason>`")
        return self

    @model_validator(mode="after")
    def _handle_names_a_profile(self) -> Person:
        if self.name == DEFAULT_PROFILE:
            raise ValueError(
                f"person {DEFAULT_PROFILE}: the default profile belongs to the machines"
            )
        if not _HANDLE.fullmatch(self.name) or self.name in _RESERVED_PROFILES:
            raise ValueError(
                f"person {self.name}: a handle is a Hermes profile name of lower-case letters, "
                "digits and '-', starting with a letter or digit, up to 32 characters, and none "
                f"of {', '.join(sorted(_RESERVED_PROFILES))}"
            )
        return self

    @property
    def is_enabled(self) -> bool:
        return self.enabled is True


class _File(_Strict):
    default_profile: DefaultProfile
    model_sources: list[ModelSource] = Field(min_length=1)
    persons: list[Person] = []


@dataclass(frozen=True)
class PersonsFile:
    """The whole declaration: the sources and the persons keyed by name, in file order."""

    model_sources: dict[str, ModelSource]
    default_profile: DefaultProfile
    persons: dict[str, Person]

    @property
    def default_source(self) -> ModelSource:
        """The source the default profile runs on; the loader refuses a file without it."""
        return self.model_sources[self.default_profile.model]

    @property
    def enabled_persons(self) -> list[Person]:
        """The persons that run, in file order."""
        return [person for person in self.persons.values() if person.is_enabled]

    def source_of(self, person: Person) -> ModelSource:
        """The source a person runs on; the loader refuses an enabled person without one."""
        return self.model_sources[person.model]


def load_persons_file(path: Path, declared: UseCaseFile) -> PersonsFile:
    """Read and validate the person declaration against the use-case file.

    Every failure is a ``ValueError`` naming the file, so the startup log and
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

    sources: dict[str, ModelSource] = {}
    for source in parsed.model_sources:
        if source.name in sources:
            raise ValueError(f"{path}: model source {source.name} is declared twice")
        sources[source.name] = source

    default = parsed.default_profile.model
    if default not in sources:
        raise ValueError(f"{path}: default_profile: no model source named {default}")
    _check_reachable(path, sources[default])

    persons: dict[str, Person] = {}
    clients = {
        server.client: server.name
        for server in declared.tool_servers.values()
        if isinstance(server, BridgeServer)
    }
    for person in parsed.persons:
        if person.name in persons:
            raise ValueError(f"{path}: person {person.name} is declared twice")
        if person.name in clients:
            raise ValueError(
                f"{path}: person {person.name}: the handle is the bridge client of tool server "
                f"{clients[person.name]}; a person's client is named by the handle"
            )
        persons[person.name] = person
    enabled = [person for person in persons.values() if person.is_enabled]
    for person in enabled:
        _check_tools(path, person, declared)
        if person.model not in sources:
            raise ValueError(
                f"{path}: person {person.name}: model: no model source named {person.model}"
            )
        _check_person_source(path, person, sources[person.model])
    _check_one_shares_the_default_account(path, enabled, sources)
    _check_discord_admin(path, enabled)
    return PersonsFile(
        model_sources=sources, default_profile=parsed.default_profile, persons=persons
    )


def _check_tools(path: Path, person: Person, declared: UseCaseFile) -> None:
    """Refuse a tool server the chat use case does not hold: the chat is the person's ceiling."""
    chat = chat_use_case(declared.use_cases)
    if chat is None:
        raise ValueError(
            f"{path}: person {person.name}: no enabled message use case; a person chats through it"
        )
    for name in person.tools:
        if name not in chat.tools:
            raise ValueError(
                f"{path}: person {person.name}: tools: {name} is not one of the {chat.name} "
                f"use case's tools ({', '.join(chat.tools)})"
            )


def _check_person_source(path: Path, person: Person, source: ModelSource) -> None:
    for step in source.steps:
        if step.provider == _ANTHROPIC:
            raise ValueError(
                f"{path}: person {person.name}: model source {source.name}: {step.label}: "
                f"{_ANTHROPIC} keeps its login state in the default profile's credentials, "
                "never a person's"
            )
    _check_reachable(path, source)


def _check_one_shares_the_default_account(
    path: Path, persons: Iterable[Person], sources: dict[str, ModelSource]
) -> None:
    """Refuse a second person on the default profile's account: it has room for one."""
    sharing = [
        person.name for person in persons if sources[person.model].account == "shared-with-default"
    ]
    if len(sharing) > 1:
        raise ValueError(
            f"{path}: persons {', '.join(sharing)}: at most one person runs on a "
            "`shared-with-default` source"
        )


def _check_discord_admin(path: Path, persons: Iterable[Person]) -> None:
    """Refuse persons on Discord without an admin: nobody could run an admin command."""
    on_discord = [person for person in persons if person.discord is not None]
    if on_discord and not any(person.discord == "admin" for person in on_discord):
        raise ValueError(
            f"{path}: persons {', '.join(person.name for person in on_discord)} are on Discord, "
            "and none is `discord: admin`"
        )


def _check_reachable(path: Path, source: ModelSource) -> None:
    """Refuse a source a profile runs on whose calls could go without a cost or a key.

    A source nobody runs on may leave its bound and its fallbacks' prices open,
    and name any provider: it renders nothing and prices nothing.
    """
    if source.bound is None:
        raise ValueError(
            f"{path}: model source {source.name}: a source a profile runs on names its `bound`"
        )
    for step in source.steps:
        if step.provider not in _PROVIDER_KEYS:
            raise ValueError(
                f"{path}: model source {source.name}: {step.label}: no key variable is known "
                f"for {step.provider}; the profile's .env could not name its key"
            )
    for step in source.fallback:
        if step.price is None and step.bound != "subscription":
            raise ValueError(
                f"{path}: model source {source.name}: fallback {step.label} is neither priced "
                "nor a subscription; its calls would carry no cost"
            )
