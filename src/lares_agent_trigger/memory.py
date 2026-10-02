"""Use-case memory: the working notes of one use case, appended by its runs, bounded here.

A note goes in as its own lines at the end. The row is then cut from the front
to `LIMIT_BYTES`, whole lines at a time, so the oldest notes go first and the
newest always stays whole: the dashboard shows notes, never half of one. A
note larger than the bound alone is refused before it gets here.
"""

from __future__ import annotations

# About 8 KB, in bytes as the row stores them; an umlaut counts twice.
LIMIT_BYTES = 8192


def appended(memory: str, note: str, *, limit: int = LIMIT_BYTES) -> str:
    """The memory with the note at its end, cut from the front to the bound."""
    lines = [*memory.splitlines(), *note.splitlines()]
    size = len("\n".join(lines).encode("utf-8"))
    while size > limit:
        size -= len(lines.pop(0).encode("utf-8")) + 1
    return "\n".join(lines)
