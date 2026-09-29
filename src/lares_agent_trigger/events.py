"""The episode event as it arrives on the bus: a pointer, never a report.

The engine publishes one message per notification event on `episode.<kind>`;
`lares-diagnostics-engine.nats_publisher.publish_episode_event` is the other
side of this contract. `subject` is the episode's own subject column — the
channel, device or room measured — and never a NATS subject.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, get_args

# The three notification events an episode emits, as the engine publishes them
# on `episode.<kind>`.
EventKind = Literal["appeared", "escalated", "ended"]

_KINDS = get_args(EventKind)


@dataclass(frozen=True, slots=True)
class EpisodeEvent:
    """One notification event of one episode."""

    episode_id: int
    fault: str
    subject: str
    severity: int
    kind: EventKind
    time: datetime

    @property
    def subject_key(self) -> str:
        """How the ledger names this event: the episode id and the kind that started it.

        The colon is what lets one episode carry both an `appeared` and an
        `escalated` run under a unique key on use case and subject.
        """
        return f"{self.episode_id}:{self.kind}"

    def as_input(self) -> dict[str, Any]:
        """The pointer handed to the harness as the run's input."""
        return {
            "episode_id": self.episode_id,
            "fault": self.fault,
            "subject": self.subject,
            "severity": self.severity,
            "kind": self.kind,
            "time": self.time.isoformat(),
        }


def parse_episode_event(data: bytes) -> EpisodeEvent:
    """Parse one message body; a payload that is not the contract is a ``ValueError``."""
    try:
        raw = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"episode event is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"episode event is not an object: {type(raw).__name__}")

    missing = [
        field
        for field in ("episode_id", "fault", "subject", "severity", "kind", "time")
        if field not in raw
    ]
    if missing:
        raise ValueError(f"episode event misses {', '.join(missing)}")

    kind = raw["kind"]
    if kind not in _KINDS:
        raise ValueError(f"episode event has unknown kind {kind!r}")

    return EpisodeEvent(
        episode_id=int(raw["episode_id"]),
        fault=str(raw["fault"]),
        subject=str(raw["subject"]),
        severity=int(raw["severity"]),
        kind=kind,
        time=datetime.fromisoformat(str(raw["time"])),
    )
