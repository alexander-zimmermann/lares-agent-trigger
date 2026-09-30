"""The declared use cases: one schema and one loader, for rendering and for runtime.

The file lives in lares and is mounted here; the generator (#2111) renders the
harness configuration from the same models, which is why schema and loader sit
in this package rather than in the generator alone. A file that does not
validate is refused at startup — a half-read catalogue would start runs nobody
declared.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .events import EventKind

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


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EventTrigger(_Strict):
    """A run started by an event on the bus.

    `filter` maps an event kind to the lowest severity that deserves a run; a
    kind left out never runs. That is the whole filter: `{appeared: 2,
    escalated: 2}` explains an episode that opens at 2 or worse and again when
    it rises, and never explains one that ended.
    """

    kind: Literal["event"]
    # Only the episode stream today. The alert path is designed on the spec and
    # built with the cluster use case; until then a declaration naming it would
    # be silently ignored, so the schema refuses it instead.
    source: Literal["episode"]
    filter: dict[EventKind, int] = Field(min_length=1)

    def wants(self, event_kind: EventKind, severity: int) -> bool:
        """True when this use case wants a run for that kind at that severity."""
        minimum = self.filter.get(event_kind)
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
    skill: str = Field(min_length=1)
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

    @property
    def is_enabled(self) -> bool:
        return self.enabled is True

    @property
    def event_trigger(self) -> EventTrigger | None:
        """The event trigger of an enabled use case, or None for every other one."""
        if self.is_enabled and isinstance(self.trigger, EventTrigger):
            return self.trigger
        return None


class _File(_Strict):
    use_cases: list[UseCase] = Field(min_length=1)


def load_use_cases(path: Path) -> dict[str, UseCase]:
    """Read and validate the use-case file, keyed by name.

    Every failure — missing file, unparsable YAML, a field the schema does not
    know, two entries of one name — is a ``ValueError`` naming the file, so the
    startup log says what to fix.
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

    by_name: dict[str, UseCase] = {}
    for use_case in parsed.use_cases:
        if use_case.name in by_name:
            raise ValueError(f"{path}: use case {use_case.name} is declared twice")
        by_name[use_case.name] = use_case
    return by_name
