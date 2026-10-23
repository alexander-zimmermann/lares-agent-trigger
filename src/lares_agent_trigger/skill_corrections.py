"""The owner's corrections of a repo skill, which no profile may write, as pull requests.

A refused `skill_manage` call (`hooks.SkillWrite`) holds an `operations`
list, each operation naming its skill; the harness's older flat shape is one
operation by itself. Hermes checks each write in full before it writes — the
skill read, the text to replace found, the header valid — and runs the
operations in order, so the first write on the read-only mount fails the call
and the rest never run. The call is therefore one change: its operations on
repo skills, a skill whose `SKILL.md` stands under the skills path on the
default branch, are applied here in order to the files as the repository
holds them, and if one does not apply, nothing is written. Operations on a
skill the profile made itself are none of the change.

A replacement is made exactly; one Hermes found only by its fuzzy match does
not apply. A whole file is the deployed skill with the change in it, so it
lands only where the file still stands as on the default branch: over an open
pull request's earlier correction it would undo that.

Each repo skill the call changes has its branch, `agent/skill/<skill>`, and a
commit per file on it. Without an open pull request the branch starts from the
default branch's head — the one a closed pull request left is set back, a
merged one's the repository deletes — and a pull request labelled
`agent/proposal` is opened; with one, the commits go onto it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import posixpath
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .deliveries import PROPOSAL_LABEL
from .github import GitHubApp, GitHubError, RepoFile
from .hooks import SkillWrite
from .metrics import Metrics
from .persons import PersonsFile

logger = logging.getLogger(__name__)

_SKILL_FILE = "SKILL.md"


@dataclass(frozen=True)
class _Operation:
    """One operation of the call on one file of a repo skill."""

    skill: str
    action: str
    # The file inside the skill's directory.
    file: str
    # The text to replace; None when `new` is the whole file.
    old: str | None
    # What replaces it, or the whole file.
    new: str
    replace_all: bool = False


@dataclass
class _SkillChange:
    """What the call makes of one repo skill: its files as they start and as they end."""

    skill: str
    branch: str
    # Whether a pull request from the branch is open; its branch is then where the files start.
    is_open: bool
    started: dict[str, RepoFile] = field(default_factory=dict)
    ended: dict[str, str] = field(default_factory=dict)
    operations: list[_Operation] = field(default_factory=list)


class _ApplyError(ValueError):
    """The call is no change this repository can take; the message says why."""


class SkillCorrections:
    """The write App's pull requests for the owner's refused skill writes, one call at a time."""

    def __init__(
        self,
        persons: PersonsFile,
        github: GitHubApp | None,
        metrics: Metrics,
        *,
        repository: str,
        path: str,
    ) -> None:
        self._persons = persons
        self._github = github
        self._metrics = metrics
        self._repository = repository
        self._path = path.strip("/")
        self._one_at_a_time = asyncio.Lock()
        # The calls still going, held so they are neither collected nor lost.
        self._running: set[asyncio.Task[None]] = set()

    def start(self, write: SkillWrite) -> None:
        """Make the owner's refused write a pull request, in the background past the hook.

        The owner is whoever `persons.yaml` makes `discord: admin`; another
        profile's attempt, the machines' included, is one log line here and
        stays in that profile's own Hermes log.
        """
        person = self._persons.enabled_person(write.profile)
        if person is None or not person.is_admin:
            logger.info(
                "%s's attempt at a repo skill is no pull request; only the owner's are",
                write.profile,
            )
            return
        if self._github is None:
            logger.warning(
                "%s's skill correction opens no pull request: the trigger has no GitHub App",
                write.profile,
            )
            return
        task = asyncio.create_task(self._run(self._github, write))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def aclose(self) -> None:
        """Stop the calls still being made into pull requests."""
        for task in self._running:
            task.cancel()
        await asyncio.gather(*self._running, return_exceptions=True)

    async def _run(self, github: GitHubApp, write: SkillWrite) -> None:
        async with self._one_at_a_time:
            try:
                await self._propose(github, write)
            except _ApplyError as exc:
                logger.warning(
                    "%s's skill correction opens no pull request: %s", write.profile, exc
                )
            except GitHubError as exc:
                self._metrics.skill_pull_requests.labels(outcome="failed").inc()
                logger.error("%s's skill correction did not reach GitHub: %s", write.profile, exc)
            except Exception:
                self._metrics.skill_pull_requests.labels(outcome="failed").inc()
                logger.exception("%s's skill correction broke off", write.profile)

    async def _propose(self, github: GitHubApp, write: SkillWrite) -> None:
        repo = f"/repos/{github.owner}/{self._repository}"
        named = _named(write.arguments)
        found = await github.call("GET", repo, expect={200})
        base = str(found.json()["default_branch"])
        ref = await github.call("GET", f"{repo}/git/ref/heads/{quote(base)}", expect={200})
        head_sha = str(ref.json()["object"]["sha"])

        changes: dict[str, _SkillChange] = {}
        for skill, raw in named:
            if skill not in changes:
                change = await self._change_of(github, repo, skill, head_sha)
                if change is None:
                    continue
                changes[skill] = change
            change = changes[skill]
            operation = _operation(skill, raw)
            await self._apply(github, change, operation, base, head_sha)
            change.operations.append(operation)
        if not changes:
            skills = ", ".join(dict.fromkeys(skill for skill, _ in named))
            raise _ApplyError(f"none of {skills} is a repo skill under {self._path} on {base}")
        for change in changes.values():
            change.ended = {
                path: content
                for path, content in change.ended.items()
                if content != change.started[path].content
            }
        if not any(change.ended for change in changes.values()):
            raise _ApplyError("the change changes nothing")

        if not await github.has_label(self._repository, PROPOSAL_LABEL):
            raise GitHubError(f"{self._repository} has no label {PROPOSAL_LABEL}")
        for change in changes.values():
            if change.ended:
                await self._write(github, repo, change, write, base, head_sha)

    async def _change_of(
        self, github: GitHubApp, repo: str, skill: str, head_sha: str
    ) -> _SkillChange | None:
        """The change the call starts on a repo skill; None for a skill the profile made itself."""
        if await github.file(self._repository, self._file(skill, _SKILL_FILE), head_sha) is None:
            return None
        branch = f"agent/skill/{skill}"
        pulls = await github.call(
            "GET",
            f"{repo}/pulls",
            params={"head": f"{github.owner}:{branch}", "state": "open"},
            expect={200},
        )
        return _SkillChange(skill, branch, is_open=bool(pulls.json()))

    async def _apply(
        self,
        github: GitHubApp,
        change: _SkillChange,
        operation: _Operation,
        base: str,
        head_sha: str,
    ) -> None:
        """Make one operation on its file as the call has left it so far."""
        path = self._file(change.skill, operation.file)
        if path not in change.started:
            on_main = await github.file(self._repository, path, head_sha)
            if on_main is None:
                raise _ApplyError(f"{base} holds no such file of the repo skills: {path}")
            current = (
                await github.file(self._repository, path, change.branch)
                if change.is_open
                else on_main
            )
            if current is None:
                raise _ApplyError(f"the open pull request from {change.branch} removed {path}")
            if operation.old is None and current.content != on_main.content:
                raise _ApplyError(
                    f"the open pull request from {change.branch} changed {path}, and a whole"
                    " file would undo that"
                )
            change.started[path] = current
            change.ended[path] = current.content
        change.ended[path] = _replaced(change.ended[path], operation, path)

    async def _write(
        self,
        github: GitHubApp,
        repo: str,
        change: _SkillChange,
        write: SkillWrite,
        base: str,
        head_sha: str,
    ) -> None:
        """Commit each changed file onto the skill's branch; open its pull request if none is."""
        title = f"fix(hermes): the {change.skill} skill as {write.profile} corrected it in chat"
        if not change.is_open:
            await self._branch_off(github, repo, change.branch, head_sha)
        for path, content in change.ended.items():
            await github.call(
                "PUT",
                f"{repo}/contents/{quote(path)}",
                json={
                    "message": title,
                    "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                    "branch": change.branch,
                    "sha": change.started[path].sha,
                },
                expect={200},
            )
        if change.is_open:
            self._metrics.skill_pull_requests.labels(outcome="committed").inc()
            logger.info(
                "%s's correction of skill %s went onto %s",
                write.profile,
                change.skill,
                change.branch,
            )
            return
        opened = await github.call(
            "POST",
            f"{repo}/pulls",
            json={
                "title": title,
                "head": change.branch,
                "base": base,
                "body": _body(change, write),
            },
            expect={201},
        )
        await github.call(
            "POST",
            f"{repo}/issues/{opened.json()['number']}/labels",
            json={"labels": [PROPOSAL_LABEL]},
            expect={200},
        )
        self._metrics.skill_pull_requests.labels(outcome="opened").inc()
        logger.info(
            "%s's correction of skill %s opened %s",
            write.profile,
            change.skill,
            opened.json()["html_url"],
        )

    async def _branch_off(self, github: GitHubApp, repo: str, branch: str, head_sha: str) -> None:
        """The skill's branch at the default branch's head: new, or set back from a closed pull."""
        held = await github.call("GET", f"{repo}/git/ref/heads/{quote(branch)}", expect={200, 404})
        if held.status_code == 404:
            await github.call(
                "POST",
                f"{repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": head_sha},
                expect={201},
            )
            return
        await github.call(
            "PATCH",
            f"{repo}/git/refs/heads/{quote(branch)}",
            json={"sha": head_sha, "force": True},
            expect={200},
        )

    def _file(self, skill: str, file: str) -> str:
        return f"{self._path}/{skill}/{file}"


def _named(arguments: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Each operation of the call with the skill it names, in order."""
    listed = arguments.get("operations")
    operations = listed if isinstance(listed, list) else [arguments]
    named: list[tuple[str, dict[str, Any]]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise _ApplyError("an operation is no object")
        # The flat shape names the skill beside the operation.
        name = operation.get("name") or arguments.get("name")
        if not isinstance(name, str):
            raise _ApplyError("an operation names no skill")
        # A skill may be named with its category before it, `<category>/<name>`.
        skill = name.rpartition("/")[2]
        if skill in ("", ".", ".."):
            raise _ApplyError(f"{name!r} names no skill")
        named.append((skill, operation))
    return named


def _operation(skill: str, raw: dict[str, Any]) -> _Operation:
    """What one operation asks of its file, in the shapes that carry text."""
    action = str(raw.get("action"))
    content, file_path = raw.get("content"), raw.get("file_path")
    old, new = raw.get("old_string"), raw.get("new_string")
    if action in ("patch", "edit") and isinstance(content, str) and content:
        return _Operation(skill, action, _SKILL_FILE, None, content)
    if action == "patch" and isinstance(old, str) and isinstance(new, str):
        file = _inside(skill, file_path) if file_path else _SKILL_FILE
        return _Operation(skill, action, file, old, new, raw.get("replace_all") is True)
    if (
        action == "write_file"
        and isinstance(file_path, str)
        and isinstance(raw.get("file_content"), str)
    ):
        return _Operation(skill, action, _inside(skill, file_path), None, raw["file_content"])
    raise _ApplyError(f"{skill}: `{action}` writes no text to propose")


def _inside(skill: str, file_path: Any) -> str:
    """The file's path inside the skill's directory: the App could write anywhere else too."""
    file = posixpath.normpath(str(file_path))
    if file.startswith(("/", "../")) or file in (".", ".."):
        raise _ApplyError(f"{skill}: {file_path!r} is no file inside the skill")
    return file


def _replaced(content: str, operation: _Operation, path: str) -> str:
    if operation.old is None:
        return operation.new
    found = content.count(operation.old)
    if found == 0:
        raise _ApplyError(f"the text to replace stands nowhere in {path}")
    if found > 1 and not operation.replace_all:
        raise _ApplyError(f"the text to replace stands {found} times in {path}")
    return content.replace(operation.old, operation.new, -1 if operation.replace_all else 1)


def _body(change: _SkillChange, write: SkillWrite) -> str:
    """What the pull request says: whose change, to which files, and why it comes this way."""
    what = ", ".join(
        dict.fromkeys(
            f"`{operation.action}` on `{operation.file}`" for operation in change.operations
        )
    )
    return (
        f"{write.profile} changed the repo skill `{change.skill}` in chat: {what}. Repo skills"
        " are read-only to every profile, so the change comes as this pull request; once it is"
        " merged, every profile reads it.\n\n"
        f"A further change to `{change.skill}` while this is open is another commit here.\n"
        f"\n---\n<sub>skill_manage, session {write.session_id}</sub>\n"
    )
