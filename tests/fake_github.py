"""GitHub's REST API over respx, for the trigger's deliveries and the read-back.

It keeps GitHub's own rules on what the trigger calls, so a test can never pass
against a fake that is laxer than the live side: an installation token only for
a JWT signed RS256 by the App's key, issued by the App and living at most ten
minutes; every other call only with a token it minted and that has not
expired; repositories only of the owner the App is installed on; a ref that
already exists, a contents write without the blob it replaces or with a stale
one, a pull request without commits between its branches — each answered as
GitHub answers it. Every write that went through is kept in `writes` as
(method, path), so a test sees exactly what was opened.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

import httpx
import jwt
import respx

from .conftest import GITHUB_APP_ID, GITHUB_INSTALLATION_ID

GITHUB_API_URL = "https://api.github.com"
GITHUB_OWNER = "alexander-zimmermann"


def _sha(*parts: str) -> str:
    return hashlib.sha1("\0".join(parts).encode("utf-8")).hexdigest()


@dataclass
class FakeRepo:
    """One repository: its commits as file trees, its branches, labels, issues and pulls."""

    name: str
    default_branch: str = "main"
    commits: dict[str, dict[str, str]] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)
    labels: set[str] = field(default_factory=set)
    # Issues and pull requests share one number sequence, as on GitHub.
    issues: dict[int, dict[str, Any]] = field(default_factory=dict)
    pulls: dict[int, dict[str, Any]] = field(default_factory=dict)
    comments: list[dict[str, Any]] = field(default_factory=list)
    numbers: Iterator[int] = field(default_factory=lambda: itertools.count(101))

    def tree(self, branch: str) -> dict[str, str]:
        return self.commits[self.branches[branch]]

    def html(self, kind: str, number: int) -> str:
        return f"https://github.com/{GITHUB_OWNER}/{self.name}/{kind}/{number}"


def _blob_sha(content: str) -> str:
    raw = content.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


def _encoded(content: str) -> str:
    """Base64 as the contents API sends it: broken into lines of 60."""
    raw = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return "\n".join(raw[i : i + 60] for i in range(0, len(raw), 60)) + "\n"


def _not_found() -> httpx.Response:
    return httpx.Response(
        404,
        json={
            "message": "Not Found",
            "documentation_url": "https://docs.github.com/rest",
            "status": "404",
        },
    )


def _unprocessable(message: str, **extra: Any) -> httpx.Response:
    return httpx.Response(422, json={"message": message, "status": "422", **extra})


class FakeGitHub:
    """The App's installation and the repositories it may write to."""

    # What the App holds on its installation: the trigger writes, the harness reads.
    GRANTED = {"metadata": "read", "contents": "write", "issues": "write", "pull_requests": "write"}
    TOKEN_LIFETIME_SECONDS = 3600

    def __init__(
        self,
        respx_mock: respx.MockRouter,
        public_key: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.public_key = public_key
        self.clock = clock
        self.repos: dict[str, FakeRepo] = {}
        self.tokens: dict[str, float] = {}
        self.minted: list[dict[str, Any]] = []
        self.writes: list[tuple[str, str]] = []
        # How many requests answer 503 before GitHub is back.
        self.unavailable = 0
        # An answer of the test's own for a request it picks, before any rule.
        self.interject: Callable[[httpx.Request], httpx.Response | None] | None = None
        self._token_numbers = itertools.count(1)
        self._ids = itertools.count(9_000_000_001)
        self.route = respx_mock.route(url__startswith=GITHUB_API_URL).mock(side_effect=self._answer)

    def add_repo(
        self,
        name: str,
        files: dict[str, str] | None = None,
        *,
        labels: tuple[str, ...] = ("agent/proposal",),
        default_branch: str = "main",
    ) -> FakeRepo:
        """A repository with one commit holding these files on its default branch."""
        repo = FakeRepo(name=name, default_branch=default_branch, labels=set(labels))
        head = _sha(name, "initial")
        repo.commits[head] = dict(files or {})
        repo.branches[default_branch] = head
        self.repos[name] = repo
        return repo

    def open_issue(self, repo: FakeRepo, title: str) -> int:
        """An issue a person opened; its number."""
        number = next(repo.numbers)
        repo.issues[number] = {
            "number": number,
            "html_url": repo.html("issues", number),
            "title": title,
            "labels": [],
            "state": "open",
        }
        return number

    def open_pull(self, repo: FakeRepo, title: str) -> int:
        """A pull request already open, as a run left it; its number."""
        number = next(repo.numbers)
        repo.pulls[number] = {
            "number": number,
            "html_url": repo.html("pull", number),
            "state": "open",
            "merged": False,
            "merged_at": None,
            "closed_at": None,
            "title": title,
            "labels": [{"name": "agent/proposal"}],
        }
        return number

    def merge(self, repo: FakeRepo, number: int) -> None:
        repo.pulls[number].update(
            state="closed",
            merged=True,
            merged_at="2026-10-05T07:12:00Z",
            closed_at="2026-10-05T07:12:00Z",
        )

    def close(self, repo: FakeRepo, number: int) -> None:
        repo.pulls[number].update(state="closed", closed_at="2026-10-05T07:12:00Z")

    # --- dispatch -------------------------------------------------------------

    def _answer(self, request: httpx.Request) -> httpx.Response:
        if self.unavailable > 0:
            self.unavailable -= 1
            return httpx.Response(503, json={"message": "Service Unavailable"})
        if self.interject is not None and (answer := self.interject(request)) is not None:
            return answer
        path = request.url.raw_path.decode("ascii").split("?", 1)[0]
        minting = re.fullmatch(r"/app/installations/(\d+)/access_tokens", path)
        if minting is not None:
            if request.method != "POST":
                return _not_found()
            return self._mint(request, int(minting.group(1)))
        if not self._token_valid(request):
            return httpx.Response(401, json={"message": "Bad credentials", "status": "401"})
        found = re.fullmatch(rf"/repos/{GITHUB_OWNER}/([A-Za-z0-9._-]+)(/.*)?", path)
        if found is None or found.group(1) not in self.repos:
            return _not_found()
        repo = self.repos[found.group(1)]
        rest = found.group(2) or ""
        body = json.loads(request.content) if request.content else {}
        for method, pattern, handle in self._routes():
            matched = re.fullmatch(pattern, rest)
            if matched is not None and request.method == method:
                response = handle(request, repo, body, *(unquote(g) for g in matched.groups()))
                if method != "GET" and response.status_code < 300:
                    self.writes.append((method, f"{repo.name}{rest}"))
                return response
        return _not_found()

    def _routes(self) -> list[tuple[str, str, Callable[..., httpx.Response]]]:
        return [
            ("GET", r"", self._repository),
            ("GET", r"/git/ref/heads/(.+)", self._get_ref),
            ("POST", r"/git/refs", self._create_ref),
            ("GET", r"/contents/(.+)", self._get_contents),
            ("PUT", r"/contents/(.+)", self._put_contents),
            ("GET", r"/labels/([^/]+)", self._get_label),
            ("POST", r"/pulls", self._create_pull),
            ("GET", r"/pulls/(\d+)", self._get_pull),
            ("POST", r"/issues", self._create_issue),
            ("GET", r"/issues/(\d+)", self._get_issue),
            ("POST", r"/issues/(\d+)/labels", self._add_labels),
            ("POST", r"/issues/(\d+)/comments", self._create_comment),
        ]

    # --- the App's tokens -----------------------------------------------------

    def _mint(self, request: httpx.Request, installation: int) -> httpx.Response:
        if not self._jwt_valid(request):
            return httpx.Response(401, json={"message": "A JSON web token could not be decoded"})
        if installation != GITHUB_INSTALLATION_ID:
            return _not_found()
        body = json.loads(request.content or b"{}")
        asked: dict[str, str] = body.get("permissions") or self.GRANTED
        levels = {"read": 1, "write": 2}
        for name, level in asked.items():
            held = self.GRANTED.get(name)
            if held is None or levels.get(level, 3) > levels[held]:
                return _unprocessable("The permissions requested are not granted to this app.")
        now = self.clock()
        token = f"ghs_token_{next(self._token_numbers)}"
        self.tokens[token] = now + self.TOKEN_LIFETIME_SECONDS
        expires_at = datetime.fromtimestamp(now + self.TOKEN_LIFETIME_SECONDS, tz=UTC)
        answer = {
            "token": token,
            "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "permissions": asked,
            "repository_selection": "selected",
        }
        self.minted.append(answer)
        return httpx.Response(201, json=answer)

    def _jwt_valid(self, request: httpx.Request) -> bool:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme != "Bearer":
            return False
        try:
            claims = jwt.decode(
                token,
                self.public_key,
                algorithms=["RS256"],
                # GitHub judges iat and exp against its own clock, which is the test's.
                options={"verify_exp": False, "verify_iat": False},
            )
        except jwt.InvalidTokenError:
            return False
        now = self.clock()
        return (
            str(claims.get("iss")) == GITHUB_APP_ID
            and claims["iat"] <= now
            and now < claims["exp"] <= claims["iat"] + 600
        )

    def _token_valid(self, request: httpx.Request) -> bool:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() not in {"bearer", "token"}:
            return False
        expires = self.tokens.get(token)
        return expires is not None and self.clock() < expires

    # --- repository, refs, contents ------------------------------------------

    def _repository(self, _request: httpx.Request, repo: FakeRepo, _body: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": repo.name,
                "full_name": f"{GITHUB_OWNER}/{repo.name}",
                "default_branch": repo.default_branch,
                "private": False,
            },
        )

    def _get_ref(
        self, _request: httpx.Request, repo: FakeRepo, _body: Any, branch: str
    ) -> httpx.Response:
        sha = repo.branches.get(branch)
        if sha is None:
            return _not_found()
        return httpx.Response(
            200, json={"ref": f"refs/heads/{branch}", "object": {"type": "commit", "sha": sha}}
        )

    def _create_ref(self, _request: httpx.Request, repo: FakeRepo, body: Any) -> httpx.Response:
        ref, sha = str(body.get("ref", "")), str(body.get("sha", ""))
        if not ref.startswith("refs/") or ref.count("/") < 2:
            return _unprocessable("Reference name must start with 'refs/' and have two slashes.")
        if sha not in repo.commits:
            return _unprocessable("Object does not exist")
        branch = ref.removeprefix("refs/heads/")
        if branch in repo.branches:
            return _unprocessable("Reference already exists")
        repo.branches[branch] = sha
        return httpx.Response(201, json={"ref": ref, "object": {"type": "commit", "sha": sha}})

    def _get_contents(
        self, request: httpx.Request, repo: FakeRepo, _body: Any, path: str
    ) -> httpx.Response:
        ref = request.url.params.get("ref") or repo.default_branch
        tree = repo.commits.get(ref) or (repo.tree(ref) if ref in repo.branches else None)
        if tree is None or path not in tree:
            return _not_found()
        content = tree[path]
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "size": len(content.encode("utf-8")),
                "name": path.rsplit("/", 1)[-1],
                "path": path,
                "content": _encoded(content),
                "sha": _blob_sha(content),
            },
        )

    def _put_contents(
        self, _request: httpx.Request, repo: FakeRepo, body: Any, path: str
    ) -> httpx.Response:
        branch = str(body.get("branch") or repo.default_branch)
        if branch not in repo.branches:
            return _not_found()
        if not body.get("message"):
            return _unprocessable('Invalid request.\n\n"message" wasn\'t supplied.')
        tree = dict(repo.tree(branch))
        existing = tree.get(path)
        if existing is not None and "sha" not in body:
            return _unprocessable('Invalid request.\n\n"sha" wasn\'t supplied.')
        if existing is not None and body["sha"] != _blob_sha(existing):
            return httpx.Response(
                409, json={"message": f"{path} does not match {body['sha']}", "status": "409"}
            )
        content = base64.b64decode(body["content"]).decode("utf-8")
        tree[path] = content
        parent = repo.branches[branch]
        commit = _sha(parent, path, content)
        repo.commits[commit] = tree
        repo.branches[branch] = commit
        return httpx.Response(
            201 if existing is None else 200,
            json={
                "content": {"path": path, "sha": _blob_sha(content)},
                "commit": {"sha": commit, "message": body["message"], "parents": [{"sha": parent}]},
            },
        )

    # --- labels, pulls, issues, comments -------------------------------------

    def _get_label(
        self, _request: httpx.Request, repo: FakeRepo, _body: Any, name: str
    ) -> httpx.Response:
        if name not in repo.labels:
            return _not_found()
        return httpx.Response(200, json={"name": name, "color": "ededed"})

    def _create_pull(self, _request: httpx.Request, repo: FakeRepo, body: Any) -> httpx.Response:
        head, base = str(body.get("head", "")), str(body.get("base", ""))
        if not str(body.get("title", "")).strip():
            return _unprocessable(
                "Validation Failed", errors=[{"field": "title", "code": "missing"}]
            )
        if head not in repo.branches:
            return _unprocessable(
                "Validation Failed", errors=[{"field": "head", "code": "invalid"}]
            )
        if base not in repo.branches:
            return _unprocessable(
                "Validation Failed", errors=[{"field": "base", "code": "invalid"}]
            )
        if repo.branches[head] == repo.branches[base]:
            return _unprocessable(
                "Validation Failed",
                errors=[{"message": f"No commits between {base} and {head}", "code": "custom"}],
            )
        number = next(repo.numbers)
        pull = {
            "number": number,
            "html_url": repo.html("pull", number),
            "state": "open",
            "merged": False,
            "merged_at": None,
            "closed_at": None,
            "title": body["title"],
            "body": body.get("body"),
            "head": {"ref": head, "sha": repo.branches[head]},
            "base": {"ref": base, "sha": repo.branches[base]},
            "labels": [],
        }
        repo.pulls[number] = pull
        return httpx.Response(201, json=pull)

    def _get_pull(
        self, _request: httpx.Request, repo: FakeRepo, _body: Any, number: str
    ) -> httpx.Response:
        pull = repo.pulls.get(int(number))
        return httpx.Response(200, json=pull) if pull is not None else _not_found()

    def _create_issue(self, _request: httpx.Request, repo: FakeRepo, body: Any) -> httpx.Response:
        if not str(body.get("title", "")).strip():
            return _unprocessable(
                "Validation Failed", errors=[{"field": "title", "code": "missing"}]
            )
        number = next(repo.numbers)
        # A label the repository does not hold is created on the way; the trigger checks first.
        labels = [str(label) for label in body.get("labels") or []]
        repo.labels.update(labels)
        issue = {
            "number": number,
            "html_url": repo.html("issues", number),
            "state": "open",
            "title": body["title"],
            "body": body.get("body"),
            "labels": [{"name": label} for label in labels],
        }
        repo.issues[number] = issue
        return httpx.Response(201, json=issue)

    def _issue_or_pull(self, repo: FakeRepo, number: int) -> dict[str, Any] | None:
        return repo.issues.get(number) or repo.pulls.get(number)

    def _get_issue(
        self, _request: httpx.Request, repo: FakeRepo, _body: Any, number: str
    ) -> httpx.Response:
        found = self._issue_or_pull(repo, int(number))
        return httpx.Response(200, json=found) if found is not None else _not_found()

    def _add_labels(
        self, _request: httpx.Request, repo: FakeRepo, body: Any, number: str
    ) -> httpx.Response:
        found = self._issue_or_pull(repo, int(number))
        if found is None:
            return _not_found()
        labels = [str(label) for label in body.get("labels") or []]
        repo.labels.update(labels)
        held = [label["name"] for label in found["labels"]]
        found["labels"] = [{"name": name} for name in dict.fromkeys([*held, *labels])]
        return httpx.Response(200, json=found["labels"])

    def _create_comment(
        self, _request: httpx.Request, repo: FakeRepo, body: Any, number: str
    ) -> httpx.Response:
        found = self._issue_or_pull(repo, int(number))
        if found is None:
            return _not_found()
        if not str(body.get("body", "")).strip():
            return _unprocessable(
                "Validation Failed", errors=[{"field": "body", "code": "missing"}]
            )
        comment_id = next(self._ids)
        comment = {
            "id": comment_id,
            "issue_number": int(number),
            "body": body["body"],
            "html_url": f"{found['html_url']}#issuecomment-{comment_id}",
        }
        repo.comments.append(comment)
        return httpx.Response(201, json=comment)
