"""A unified diff applied to one file, strictly: a hunk that does not fit is refused, not guessed.

A pull request from a run carries the diff the model wrote, and the trigger
applies it to the file as it stands on the default branch before anything is
opened. Every hunk's context and removed lines must stand in the file exactly
as written. A hunk is looked for at the line its header names first; a model
that miscounted lines is forgiven when the hunk fits exactly one place after
the previous hunk, never when it fits several or none. The line counts in a
hunk header are not checked — the lines themselves are — so a hunk that only
adds lines needs a context line to show where they go, except in an empty file.

One diff touches one file: its `---`/`+++` header, if it has one, must name the
path the block names (`/dev/null` before a file the diff creates).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")
# Lines git writes before the first hunk that say nothing about the content.
_PREAMBLE = ("diff --git ", "index ", "new file mode ", "similarity index ")
_NO_NEWLINE = "\\ No newline at end of file"


class PatchError(ValueError):
    """The diff is not one this file can take; the message says where it broke."""


@dataclass
class _Hunk:
    number: int
    # The line the header names, 1-based; 0 for a hunk that adds to an empty file.
    old_start: int
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    # The new side ends without a newline.
    no_newline: bool = False


def apply_diff(original: str | None, diff: str, path: str) -> str:
    """The file after the diff; ``original`` is None for a file the default branch lacks."""
    hunks = _parse(diff, path, creates=original is None)
    lines = (original or "").splitlines()
    result: list[str] = []
    cursor = 0
    for hunk in hunks:
        at = _locate(hunk, lines, cursor)
        result.extend(lines[cursor:at])
        result.extend(hunk.new)
        cursor = at + len(hunk.old)
    result.extend(lines[cursor:])

    # The last line keeps its newline unless the diff says otherwise for a
    # hunk that reached the end, or the untouched end of the file had none.
    reached_end = cursor >= len(lines)
    bare = hunks[-1].no_newline if reached_end else not (original or "").endswith("\n")
    patched = "\n".join(result) + ("" if bare or not result else "\n")
    if patched == (original or ""):
        raise PatchError("the diff changes nothing")
    return patched


def _parse(diff: str, path: str, *, creates: bool) -> list[_Hunk]:
    lines = diff.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    hunks: list[_Hunk] = []
    previous = ""
    for number, line in enumerate(lines, start=1):
        if line.startswith("--- ") or line.startswith("+++ "):
            if hunks:
                raise PatchError(
                    f"line {number}: the diff touches a second file; one block, one file"
                )
            _check_header(line, path, creates=creates)
            continue
        if not hunks and line.startswith(_PREAMBLE):
            continue
        header = _HUNK_HEADER.match(line)
        if header is not None:
            hunks.append(_Hunk(number=len(hunks) + 1, old_start=int(header.group(1))))
            continue
        if not hunks:
            raise PatchError(f"line {number}: {line!r} comes before the first @@ hunk header")
        hunk = hunks[-1]
        if line == _NO_NEWLINE:
            # It follows the line it is about; only the new side's last line matters.
            hunk.no_newline = previous != "-"
            continue
        # A blank context line often loses its leading space on the way.
        marker, text = (line[:1], line[1:]) if line else (" ", "")
        previous = marker
        if marker == " ":
            hunk.old.append(text)
            hunk.new.append(text)
        elif marker == "-":
            hunk.old.append(text)
        elif marker == "+":
            hunk.new.append(text)
        else:
            raise PatchError(
                f"line {number}: {line!r} is neither context (' '), removal ('-')"
                " nor addition ('+')"
            )
    if not hunks:
        raise PatchError("the diff holds no @@ hunk")
    return hunks


def _check_header(line: str, path: str, *, creates: bool) -> None:
    named = line[4:].split("\t", 1)[0].strip()
    if named == "/dev/null":
        if line.startswith("--- ") and creates:
            return
        raise PatchError(f"{line!r}: the diff may not delete or create {path} here")
    bare = named.removeprefix("a/") if line.startswith("--- ") else named.removeprefix("b/")
    if bare != path:
        raise PatchError(f"{line!r} names {bare}, not the block's path {path}")
    if line.startswith("--- ") and creates:
        raise PatchError(
            f"{path} does not exist on the default branch; the diff starts from /dev/null"
        )


def _locate(hunk: _Hunk, lines: list[str], cursor: int) -> int:
    """Where the hunk's old lines stand: at its header's line, else at the one place they fit."""
    if not hunk.old:
        if lines:
            raise PatchError(
                f"hunk {hunk.number} adds lines with no context to show where they go;"
                " give it the lines around them"
            )
        return 0

    stated = hunk.old_start - 1
    if stated >= cursor and lines[stated : stated + len(hunk.old)] == hunk.old:
        return stated
    places = [
        at
        for at in range(cursor, len(lines) - len(hunk.old) + 1)
        if lines[at : at + len(hunk.old)] == hunk.old
    ]
    if len(places) == 1:
        return places[0]
    first = hunk.old[0]
    if not places:
        raise PatchError(
            f"hunk {hunk.number} does not apply: its context and removed lines, from {first!r} on,"
            f" stand nowhere in the file after line {cursor} as written"
        )
    raise PatchError(
        f"hunk {hunk.number} does not apply at line {hunk.old_start}, and its lines fit"
        f" {len(places)} other places (lines {', '.join(str(at + 1) for at in places)})"
    )
