"""The person declaration: the model sources and the default profile's source.

The file lives in lares beside the use-case file and is mounted here; the
generator renders the default profile's model and fallback from it, and the
trigger prices every model call by the source step that served it. Schema and
loader sit in this package for the same reason the use cases' do: rendering
and runtime read one schema.

A source is a provider and a model, how its account relates to the default
profile's, and what bounds its spend: a `subscription` costs nothing per call,
a `capped` step is metered against a hard limit and names its price in USD per
million tokens. Its fallback steps take over when it fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# How a source's spend is bounded: a flat plan, or metered use under a hard limit.
Bound = Literal["subscription", "capped"]
# Whose login a source runs on: the default profile's own, or the person's alone.
Account = Literal["shared-with-default", "per-person"]

# The harness's name for the machines' profile, which event runs and cron run on.
DEFAULT_PROFILE = "default"
# The provider of a ChatGPT login: a profile's own source, never a fallback.
_CODEX = "openai-codex"
_PER_MILLION = Decimal(1_000_000)


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


class _File(_Strict):
    default_profile: DefaultProfile
    model_sources: list[ModelSource] = Field(min_length=1)


@dataclass(frozen=True)
class PersonsFile:
    """The whole declaration: the sources keyed by name, in file order."""

    model_sources: dict[str, ModelSource]
    default_profile: DefaultProfile

    @property
    def default_source(self) -> ModelSource:
        """The source the default profile runs on; the loader refuses a file without it."""
        return self.model_sources[self.default_profile.model]


def load_persons_file(path: Path) -> PersonsFile:
    """Read and validate the person declaration.

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
    return PersonsFile(model_sources=sources, default_profile=parsed.default_profile)


def _check_reachable(path: Path, source: ModelSource) -> None:
    """Refuse a source a profile runs on whose calls could go without a cost.

    A source nobody runs on may leave its bound and its fallbacks' prices open:
    it renders nothing and prices nothing.
    """
    if source.bound is None:
        raise ValueError(
            f"{path}: model source {source.name}: a source a profile runs on names its `bound`"
        )
    for step in source.fallback:
        if step.price is None and step.bound != "subscription":
            raise ValueError(
                f"{path}: model source {source.name}: fallback {step.label} is neither priced "
                "nor a subscription; its calls would carry no cost"
            )
