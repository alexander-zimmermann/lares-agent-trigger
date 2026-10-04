"""Delivery: the trigger carries a completed run's output to the targets its use case declares.

The model never delivers — on an API run the harness posts nothing itself. The
registry below holds one delivery per output target. `stored` is the ledger
row, which the event path writes before any other target sees the text; the
others post the text somewhere a person reads it and name what they created,
so the row can record it in `output_ref`. A target that refuses fails the run:
the others are still tried, the stored text stays, and the refusal's raw text
becomes the row's error.

`wiki_page` takes a structured output, validated before anything leaves: the
run opens with its sentence, as every run does, then names its page in a block
and ends with the page itself —

    <sentence>

    ---
    path: haus/wartungsplan
    title: Wartungsplan
    ---
    <the page in Markdown, to the end of the text>

A block that does not hold is a refusal like any other, and the wiki is never
called.

The GitHub targets — `github_pr`, `github_issue`, `github_comment` — take one
fenced block per thing to open, up to three a run, written as the write App:

    <sentence>

    ~~~github_pr
    repository: lares
    path: kubernetes/applications/lares-diagnostics-engine/base/config/faults.yaml
    title: Let the dryer run five hours before appliance_runtime fires
    labels: [topic/smart-home]
    body: |
      <the evidence, in Markdown>
    diff: |
      <a unified diff of that one file>
    ~~~

A tilde fence closes only on a line `~~~` of its own, so the backtick fences a
body quotes never end it, and the blocks are cut out of the text the other
targets carry. Every target checks the output before any of them is written
to, and one check that fails keeps them all from it; a GitHub target then
checks its labels against the repository and a pull request's diff against
the file on the default branch before it writes. A run with nothing for a
target says so in one block, `none: <why>`; a run that names none at all fails.

The targets the enabled event and schedule use cases declare are built, and
one of them without its settings — or without a delivery at all — stops the
pod at startup, the same rule as a use-case file that does not validate.
Every other target is built when its settings are there, for a run fed by
hand to a use case still dormant.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import smtplib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from importlib.metadata import version
from typing import Any, Protocol
from urllib.parse import quote

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .config import Settings
from .events import Occasion
from .failures import describe
from .faults import first_clause, load_fault_sentences
from .github import GitHubApp, GitHubError
from .ledger import OutputState, Usage
from .metrics import Metrics
from .patches import PatchError, apply_diff
from .use_cases import OutputTarget, UseCase

logger = logging.getLogger(__name__)

_DISCORD_API = "https://discord.com/api/v10"
# Discord refuses a message longer than this.
_DISCORD_LIMIT = 2000
# How the skill marks a proof line; Discord renders it small and grey.
_PROOF_PREFIX = "-# "
# The line that opens and closes a wiki page block.
_PAGE_BLOCK_DELIMITER = "---"
# A GitHub block opens with this and its target, `~~~github_pr`, and closes
# with this alone, both at the start of a line. Backtick fences inside the
# block — the diff quoted in a pull request's body — never close it.
_FENCE = "~~~"
# The most one run may open on one GitHub target.
_BLOCKS_PER_RUN = 3
# Every pull request a run opens carries this label, whatever its block names.
PROPOSAL_LABEL = "agent/proposal"
_GITHUB_FENCES = frozenset(
    f"{_FENCE}{target}" for target in ("github_pr", "github_issue", "github_comment")
)


class DeliveryError(RuntimeError):
    """A target refused the output; the message carries its raw answer.

    `refs` names what the target had created before it refused — the first
    message of a split post — so the row records it all the same.
    """

    def __init__(self, message: str, *, refs: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.refs = refs


@dataclass(frozen=True)
class RunOutput:
    """One completed run, as the targets see it."""

    run_id: int
    use_case: str
    # The event or the request a run on an episode was started for; None for
    # a cron run or a hand-fed one, which have no episode.
    occasion: Occasion | None
    text: str
    usage: Usage


class Delivery(Protocol):
    """One output target."""

    # The state a ref of this target starts in; only a pull request has one.
    state: OutputState | None

    def check(self, output: RunOutput, /) -> None:
        """Raise a `DeliveryError` when the output cannot be delivered here; nothing leaves."""
        ...

    async def deliver(self, output: RunOutput, /) -> tuple[str, ...]:
        """Carry the output; one ref per thing it created, for the row to record."""
        ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class Refusal:
    """One target that did not take the output, and its raw answer."""

    target: OutputTarget
    reason: str


@dataclass(frozen=True)
class Delivered:
    """What one run's targets made of it: the refs they created, and who refused.

    `states` stands beside `refs`, position for position: a pull request starts
    `open` and is followed by the read-back, everything else has no state.
    """

    refs: tuple[str, ...]
    states: tuple[OutputState | None, ...]
    refusals: tuple[Refusal, ...]

    @property
    def refused(self) -> str:
        """The targets that refused, for the alert's summary."""
        return ", ".join(refusal.target for refusal in self.refusals)

    @property
    def error(self) -> str:
        """Each refusal's raw answer under its target, for the row's error."""
        return "\n".join(f"{refusal.target}: {refusal.reason}" for refusal in self.refusals)


class StoredDelivery:
    """The ledger row itself, already written when the deliveries run."""

    state: OutputState | None = None

    def check(self, _output: RunOutput, /) -> None:
        return None

    async def deliver(self, _output: RunOutput, /) -> tuple[str, ...]:
        return ()

    async def aclose(self) -> None:
        return None


class DiscordDelivery:
    """The home channel, through the bot's REST API with the token the harness chats with."""

    state: OutputState | None = None

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.discord_bot_token or not settings.discord_home_channel:
            raise ValueError(
                "the discord output needs DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN_FILE,"
                " and DISCORD_HOME_CHANNEL"
            )
        self._channel = settings.discord_home_channel
        self._client = client or httpx.AsyncClient(
            base_url=_DISCORD_API,
            headers={
                "Authorization": f"Bot {settings.discord_bot_token}",
                # Discord asks a bot to name itself in this form.
                "User-Agent": (
                    "DiscordBot (https://github.com/alexander-zimmermann/lares-agent-trigger,"
                    f" {version('lares-agent-trigger')})"
                ),
            },
            timeout=settings.discord_request_timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def check(self, _output: RunOutput, /) -> None:
        return None

    async def deliver(self, output: RunOutput, /) -> tuple[str, ...]:
        """Post the text as `discord_messages` splits it; one ref per message posted."""
        refs: list[str] = []
        for message in discord_messages(prose(output.text), output.run_id):
            try:
                message_id = await self._post(message)
            except DeliveryError as exc:
                raise DeliveryError(str(exc), refs=tuple(refs)) from exc
            refs.append(f"discord:{self._channel}/{message_id}")
        return tuple(refs)

    async def _post(self, content: str) -> str:
        try:
            response = await self._client.post(
                f"/channels/{self._channel}/messages",
                # Nothing the model wrote may ping anyone.
                json={"content": content, "allowed_mentions": {"parse": []}},
            )
        except httpx.HTTPError as exc:
            raise DeliveryError(f"Discord could not be reached: {describe(exc)}") from exc
        if response.status_code >= 400:
            raise DeliveryError(f"Discord returned {response.status_code}: {response.text}")
        try:
            return str(response.json()["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise DeliveryError(
                f"Discord returned {response.status_code} without a message id: {response.text}"
            ) from exc


def discord_messages(text: str, run_id: int) -> list[str]:
    """The text as Discord messages: whole when it fits, else cause and proof first, the rest after.

    A part still too long is cut on a line and points to the run, whose row
    holds the whole text.
    """
    body = text.strip()
    if len(body) <= _DISCORD_LIMIT:
        return [body]
    head, *lines = body.splitlines()
    proofs = [line for line in lines if line.startswith(_PROOF_PREFIX)]
    rest = "\n".join(line for line in lines if not line.startswith(_PROOF_PREFIX)).strip()
    first = (f"{head}\n\n" + "\n".join(proofs)) if proofs else head
    return [_fit(part, run_id) for part in (first, rest) if part]


def _fit(part: str, run_id: int) -> str:
    if len(part) <= _DISCORD_LIMIT:
        return part
    marker = f"\n… (run {run_id})"
    cut = part[: _DISCORD_LIMIT - len(marker)]
    if (line_end := cut.rfind("\n")) > 0:
        cut = cut[:line_end]
    return cut + marker


class MailDelivery:
    """A mail through the cluster's relay, from its one accepted sender to the owner."""

    state: OutputState | None = None

    def __init__(self, settings: Settings) -> None:
        missing = [
            name
            for name, value in (
                ("SMTP_HOST", settings.smtp_host),
                ("MAIL_FROM", settings.mail_from),
                ("MAIL_TO", settings.mail_to),
                ("DASHBOARD_EPISODE_URL", settings.dashboard_episode_url),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"the mail output needs {', '.join(missing)}")
        try:
            settings.dashboard_episode_url.format(episode_id=0, fault="")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                "DASHBOARD_EPISODE_URL may name only {episode_id} and {fault}:"
                f" {settings.dashboard_episode_url} ({exc!r})"
            ) from exc
        self._settings = settings
        self._sentences = load_fault_sentences(settings.faults_file)
        self._domain = parseaddr(settings.mail_from)[1].rpartition("@")[2]

    async def aclose(self) -> None:
        return None

    def check(self, _output: RunOutput, /) -> None:
        return None

    async def deliver(self, output: RunOutput, /) -> tuple[str, ...]:
        """Send the mail; the ref is its Message-ID."""
        message = self._compose(output)
        try:
            await asyncio.to_thread(self._send, message)
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(
                f"the relay at {self._settings.smtp_host}:{self._settings.smtp_port}"
                f" did not take the mail: {describe(exc)}"
            ) from exc
        return (f"mail:{str(message['Message-ID']).strip('<>')}",)

    def _compose(self, output: RunOutput) -> EmailMessage:
        message = EmailMessage()
        body = _as_mail(prose(output.text))
        episode = output.occasion
        if episode is None:
            # No episode to name: the use case and the sentence the run opens with.
            message["Subject"] = f"[{output.use_case}] {body.splitlines()[0]}"
            footer = _footer(output.usage)
        else:
            sentence = self._sentences.get(episode.fault)
            # An episode can outlive its fault's entry; the fault's name still says what it was.
            what = first_clause(sentence) if sentence else episode.fault
            message["Subject"] = f"[Explain] {what} · {episode.subject}"
            link = self._settings.dashboard_episode_url.format(
                episode_id=episode.episode_id, fault=episode.fault
            )
            footer = f"{_footer(output.usage)}\n{link}"
        message["From"] = self._settings.mail_from
        message["To"] = self._settings.mail_to
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=self._domain)
        message.set_content(f"{body}\n\n-- \n{footer}\n")
        return message

    def _send(self, message: EmailMessage) -> None:
        # The relay speaks plaintext inside the cluster; no STARTTLS, no login.
        with smtplib.SMTP(
            self._settings.smtp_host,
            self._settings.smtp_port,
            timeout=self._settings.smtp_timeout_seconds,
        ) as smtp:
            smtp.send_message(message)


def _as_mail(text: str) -> str:
    """The text with its proof lines as a plain list: `-# ` is Discord's markup."""
    return "\n".join(
        f"• {line.removeprefix(_PROOF_PREFIX)}" if line.startswith(_PROOF_PREFIX) else line
        for line in text.splitlines()
    )


def _footer(usage: Usage) -> str:
    """The run's model and cost: what the explanation carries and only the trigger knows."""
    parts: list[str] = []
    if usage.model:
        parts.append(f"{usage.model} ({usage.model_source})" if usage.model_source else usage.model)
    if usage.tokens_in is not None and usage.tokens_out is not None:
        parts.append(f"{usage.tokens_in} + {usage.tokens_out} Tokens")
    # A run on a subscription costs nothing on its own; a zero would read as free.
    if usage.cost:
        parts.append(f"{usage.cost:.4f} USD")
    if usage.duration_seconds is not None:
        parts.append(f"{usage.duration_seconds:.0f} s")
    return " · ".join(parts)


class _PageHeader(BaseModel):
    """What a page block names: where the page lives and what it is called."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    title: str = Field(min_length=1)


@dataclass(frozen=True)
class WikiPage:
    """One page as a run delivers it."""

    path: str
    title: str
    content: str


def wiki_page(text: str) -> WikiPage:
    """The page the output's block names, or a `DeliveryError` saying what does not hold."""
    lines = text.strip().splitlines()
    try:
        opening = lines.index(_PAGE_BLOCK_DELIMITER)
    except ValueError:
        raise DeliveryError(
            "the output holds no page block: a line ---, its path and title, a line ---,"
            " then the page"
        ) from None
    if opening == 0:
        raise DeliveryError("the output opens with its sentence, then the page block")
    try:
        closing = lines.index(_PAGE_BLOCK_DELIMITER, opening + 1)
    except ValueError:
        raise DeliveryError("the page block has no closing --- line") from None
    try:
        raw = yaml.safe_load("\n".join(lines[opening + 1 : closing]))
    except yaml.YAMLError as exc:
        raise DeliveryError(f"the page block is not valid YAML: {exc}") from exc
    try:
        header = _PageHeader.model_validate(raw)
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'block'}: {error['msg']}"
            for error in exc.errors()
        )
        raise DeliveryError(f"the page block does not hold: {reasons}") from exc
    content = "\n".join(lines[closing + 1 :]).strip()
    if not content:
        raise DeliveryError("the page block is followed by no content")
    return WikiPage(path=header.path.strip("/"), title=header.title, content=f"{content}\n")


_WIKI_LIST_QUERY = """\
query ($locale: String!) {
  pages {
    list(locale: $locale) { id path locale description isPublished tags }
  }
}"""

# A write answers with the raw page row, which has `localeCode` and no
# `locale`: selecting `locale` here fails the answer after the write went through.
_WIKI_RESULT = """\
      responseResult { succeeded slug message }
      page { id path }"""

_WIKI_CREATE = f"""\
mutation ($content: String!, $locale: String!, $path: String!, $title: String!) {{
  pages {{
    create(content: $content, description: "", editor: "markdown", isPublished: true,
           isPrivate: false, locale: $locale, path: $path, tags: [], title: $title) {{
{_WIKI_RESULT}
    }}
  }}
}}"""

_WIKI_UPDATE = f"""\
mutation ($id: Int!, $content: String!, $title: String!, $description: String,
          $isPublished: Boolean!, $tags: [String]!) {{
  pages {{
    update(id: $id, content: $content, title: $title, description: $description,
           isPublished: $isPublished, tags: $tags) {{
{_WIKI_RESULT}
    }}
  }}
}}"""


class WikiPageDelivery:
    """A page in the house wiki, through Wiki.js's GraphQL API under the write key.

    The path is looked up in the configured locale: a new path is created as a
    published page, an existing page gets the block's content and title and
    keeps its description, tags and publish flag — Wiki.js resets whatever an
    update leaves out. A publish window set in the editor is not in the page
    list, so an update clears it. Every update leaves the previous revision in
    the page's history.
    """

    state: OutputState | None = None

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        missing = [
            name
            for name, value in (
                ("WIKIJS_URL", settings.wikijs_url),
                ("WIKIJS_TOKEN or WIKIJS_TOKEN_FILE", settings.wikijs_token),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"the wiki_page output needs {', '.join(missing)}")
        self._locale = settings.wikijs_locale
        self._client = client or httpx.AsyncClient(
            base_url=settings.wikijs_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.wikijs_token}"},
            timeout=settings.wikijs_request_timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def check(self, output: RunOutput, /) -> None:
        wiki_page(prose(output.text))

    async def deliver(self, output: RunOutput, /) -> tuple[str, ...]:
        """Create or update the page the block names; the ref is its locale and path."""
        page = wiki_page(prose(output.text))
        listed = await self._graphql(_WIKI_LIST_QUERY, {"locale": self._locale})
        existing = next((entry for entry in listed["list"] if entry["path"] == page.path), None)
        if existing is None:
            action, mutation = "create", _WIKI_CREATE
            variables: dict[str, Any] = {
                "content": page.content,
                "locale": self._locale,
                "path": page.path,
                "title": page.title,
            }
        else:
            action, mutation = "update", _WIKI_UPDATE
            variables = {
                "id": existing["id"],
                "content": page.content,
                "title": page.title,
                "description": existing["description"],
                "isPublished": existing["isPublished"],
                "tags": existing["tags"],
            }
        result = (await self._graphql(mutation, variables))[action]
        status = result["responseResult"]
        if not status["succeeded"]:
            raise DeliveryError(
                f"Wiki.js refused to {action} {page.path}: {status['slug']}: {status['message']}"
            )
        return (f"wiki:{self._locale}/{result['page']['path']}",)

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """One GraphQL request; its `pages` payload, or a `DeliveryError` with Wiki.js's answer."""
        try:
            response = await self._client.post(
                "/graphql", json={"query": query, "variables": variables}
            )
        except httpx.HTTPError as exc:
            raise DeliveryError(f"Wiki.js could not be reached: {describe(exc)}") from exc
        if response.status_code >= 400:
            raise DeliveryError(f"Wiki.js returned {response.status_code}: {response.text}")
        try:
            body = response.json()
        except ValueError as exc:
            raise DeliveryError(
                f"Wiki.js returned {response.status_code} without JSON: {response.text}"
            ) from exc
        if body.get("errors"):
            messages = "; ".join(str(error.get("message")) for error in body["errors"])
            raise DeliveryError(f"Wiki.js refused the request: {messages}")
        pages: dict[str, Any] = body["data"]["pages"]
        return pages


def output_blocks(text: str, target: OutputTarget) -> list[Any]:
    """The YAML of every `~~~<target>` block in the text, in order; a broken one refuses."""
    opening = f"{_FENCE}{target}"
    lines = text.splitlines()
    found: list[Any] = []
    start = 0
    while True:
        try:
            first = next(i for i in range(start, len(lines)) if lines[i].rstrip() == opening)
        except StopIteration:
            return found
        try:
            last = next(i for i in range(first + 1, len(lines)) if lines[i].rstrip() == _FENCE)
        except StopIteration:
            raise DeliveryError(
                f"the {opening} block on line {first + 1} is never closed by a line {_FENCE}"
            ) from None
        try:
            found.append(yaml.safe_load("\n".join(lines[first + 1 : last])))
        except yaml.YAMLError as exc:
            raise DeliveryError(
                f"the {opening} block on line {first + 1} is not valid YAML: {exc}"
            ) from exc
        start = last + 1


def prose(text: str) -> str:
    """The text a person reads: without the GitHub blocks the trigger opens elsewhere."""
    kept: list[str] = []
    inside = False
    for line in text.splitlines():
        if not inside and line.rstrip() in _GITHUB_FENCES:
            inside = True
        elif inside and line.rstrip() == _FENCE:
            inside = False
        elif not inside:
            kept.append(line)
    return "\n".join(kept).strip()


class _Block(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _NothingBlock(_Block):
    """A run that has nothing for this target this time, and says why."""

    none: str = Field(min_length=1)


# A repository of the App's owner, by its bare name.
_REPOSITORY = r"^[A-Za-z0-9._-]+$"


class _GitHubBlock(_Block):
    repository: str = Field(pattern=_REPOSITORY)
    body: str = Field(min_length=1)


class _OpenedBlock(_GitHubBlock):
    """What a pull request and an issue have in common: a one-line title and labels."""

    title: str = Field(min_length=1, max_length=256)
    labels: tuple[str, ...] = ()

    @field_validator("title")
    @classmethod
    def _one_line(cls, title: str) -> str:
        if "\n" in title.strip():
            raise ValueError("a title is one line")
        return title.strip()

    @field_validator("labels")
    @classmethod
    def _named(cls, labels: tuple[str, ...]) -> tuple[str, ...]:
        if any(not label.strip() for label in labels):
            raise ValueError("a label has a name")
        return labels


class _PullRequestBlock(_OpenedBlock):
    """One pull request: the file it changes, and the diff against the default branch."""

    path: str = Field(min_length=1)
    diff: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _inside(cls, path: str) -> str:
        parts = path.split("/")
        if path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("a path inside the repository, without ./ or ../")
        return path


class _IssueBlock(_OpenedBlock):
    """One issue to open."""


class _CommentBlock(_GitHubBlock):
    """One comment on an issue or pull request that already exists."""

    number: int = Field(gt=0)


def _github_blocks[B: _GitHubBlock](text: str, target: OutputTarget, model: type[B]) -> list[B]:
    """The target's blocks, checked; empty for a run that says it has nothing for it."""
    raw = output_blocks(text, target)
    fence = f"{_FENCE}{target}"
    if not raw:
        raise DeliveryError(
            f"the output holds no {fence} block: a line {fence}, the fields in YAML,"
            f" a line {_FENCE} — or one block with `none: <why>` when there is nothing"
        )
    if any(isinstance(block, dict) and "none" in block for block in raw):
        if len(raw) > 1:
            raise DeliveryError(f"a {fence} block with `none` stands alone")
        _checked(_NothingBlock, raw[0], f"the {fence} block")
        return []
    if len(raw) > _BLOCKS_PER_RUN:
        raise DeliveryError(f"{len(raw)} {fence} blocks; a run opens at most {_BLOCKS_PER_RUN}")
    return [
        _checked(model, block, f"{fence} block {number}")
        for number, block in enumerate(raw, start=1)
    ]


def _checked[M: BaseModel](model: type[M], raw: Any, name: str) -> M:
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'block'}: {error['msg']}"
            for error in exc.errors()
        )
        raise DeliveryError(f"{name} does not hold: {reasons}") from exc


def _signed(body: str, output: RunOutput) -> str:
    """A GitHub write's body, ending in the line that leads back to its row."""
    usage = output.usage
    parts = [f"{output.use_case}, run {output.run_id}"]
    if usage.model:
        parts.append(f"{usage.model} ({usage.model_source})" if usage.model_source else usage.model)
    return body.rstrip() + "\n\n---\n<sub>" + " · ".join(parts) + "</sub>\n"


@dataclass(frozen=True)
class _ReadyPullRequest:
    """A pull request ready to open: the diff applied, every label known to the repository."""

    block: _PullRequestBlock
    base: str
    head_sha: str
    blob_sha: str | None
    content: str
    labels: tuple[str, ...]


class _GitHubTarget[B: _GitHubBlock]:
    """What the three GitHub targets share: the App, the blocks, the checks before a write."""

    target: OutputTarget
    block: type[B]
    state: OutputState | None = None

    def __init__(self, github: GitHubApp | None) -> None:
        if github is None:
            raise ValueError(
                f"the {self.target} output needs GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID and"
                " GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_FILE"
            )
        self._github = github

    def check(self, output: RunOutput, /) -> None:
        self._blocks(output)

    def _blocks(self, output: RunOutput) -> list[B]:
        return _github_blocks(output.text, self.target, self.block)

    async def aclose(self) -> None:
        # The App is shared with the other targets and the read-back; its owner closes it.
        return None

    def _repo(self, repository: str) -> str:
        return f"/repos/{self._github.owner}/{repository}"

    async def _check_labels(self, repository: str, labels: Sequence[str]) -> None:
        for label in labels:
            found = await self._github.call(
                "GET", f"{self._repo(repository)}/labels/{quote(label, safe='')}", expect={200, 404}
            )
            if found.status_code == 404:
                raise DeliveryError(f"{repository} has no label {label}")


class GitHubPullRequestDelivery(_GitHubTarget[_PullRequestBlock]):
    """Each `~~~github_pr` block a pull request: a fresh branch, one commit, the labels.

    Every block is checked before anything is written — its fields, its labels
    against the repository, its diff against the file on the default branch —
    so a diff that does not apply opens nothing at all.
    """

    target = "github_pr"
    block = _PullRequestBlock
    # Followed by the read-back until it is merged or closed.
    state: OutputState | None = "open"

    async def deliver(self, output: RunOutput, /) -> tuple[str, ...]:
        blocks = self._blocks(output)
        try:
            ready = [
                await self._prepare(number, block) for number, block in enumerate(blocks, start=1)
            ]
        except GitHubError as exc:
            raise DeliveryError(str(exc)) from exc
        refs: list[str] = []
        for number, pull in enumerate(ready, start=1):
            branch = f"agent/{output.use_case}/run-{output.run_id}-{number}"
            try:
                await self._open(pull, branch, output, refs)
            except GitHubError as exc:
                raise DeliveryError(str(exc), refs=tuple(refs)) from exc
        return tuple(refs)

    async def _prepare(self, number: int, block: _PullRequestBlock) -> _ReadyPullRequest:
        repo = self._repo(block.repository)
        found = await self._github.call("GET", repo, expect={200, 404})
        if found.status_code == 404:
            raise DeliveryError(f"block {number}: the App sees no repository {block.repository}")
        base = str(found.json()["default_branch"])
        ref = await self._github.call("GET", f"{repo}/git/ref/heads/{quote(base)}", expect={200})
        head_sha = str(ref.json()["object"]["sha"])
        current = await self._github.call(
            "GET",
            f"{repo}/contents/{quote(block.path)}",
            params={"ref": head_sha},
            expect={200, 404},
        )
        original: str | None = None
        blob_sha: str | None = None
        if current.status_code == 200:
            file = current.json()
            if not isinstance(file, dict) or file.get("type") != "file":
                raise DeliveryError(
                    f"block {number}: {block.path} in {block.repository} is no file"
                )
            if file.get("encoding") != "base64":
                raise DeliveryError(
                    f"block {number}: {block.path} is too large for the contents API"
                )
            original = base64.b64decode(file["content"]).decode("utf-8")
            blob_sha = str(file["sha"])
        try:
            content = apply_diff(original, block.diff, block.path)
        except PatchError as exc:
            raise DeliveryError(
                f"block {number}: the diff does not apply to {block.path} on"
                f" {block.repository}@{base}: {exc}"
            ) from exc
        labels = tuple(dict.fromkeys((PROPOSAL_LABEL, *block.labels)))
        await self._check_labels(block.repository, labels)
        return _ReadyPullRequest(block, base, head_sha, blob_sha, content, labels)

    async def _open(
        self, pull: _ReadyPullRequest, branch: str, output: RunOutput, refs: list[str]
    ) -> None:
        block = pull.block
        repo = self._repo(block.repository)
        await self._github.call(
            "POST",
            f"{repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": pull.head_sha},
            expect={201},
        )
        commit: dict[str, Any] = {
            "message": block.title,
            "content": base64.b64encode(pull.content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if pull.blob_sha is not None:
            commit["sha"] = pull.blob_sha
        await self._github.call(
            "PUT", f"{repo}/contents/{quote(block.path)}", json=commit, expect={200, 201}
        )
        opened = await self._github.call(
            "POST",
            f"{repo}/pulls",
            json={
                "title": block.title,
                "head": branch,
                "base": pull.base,
                "body": _signed(block.body, output),
            },
            expect={201},
        )
        created = opened.json()
        # Opened is opened: a label that fails after this still leaves the row naming it.
        refs.append(str(created["html_url"]))
        await self._github.call(
            "POST",
            f"{repo}/issues/{created['number']}/labels",
            json={"labels": list(pull.labels)},
            expect={200},
        )


class GitHubIssueDelivery(_GitHubTarget[_IssueBlock]):
    """Each `~~~github_issue` block an issue with its labels, every label checked first."""

    target = "github_issue"
    block = _IssueBlock

    async def deliver(self, output: RunOutput, /) -> tuple[str, ...]:
        blocks = self._blocks(output)
        refs: list[str] = []
        try:
            for block in blocks:
                await self._check_labels(block.repository, block.labels)
            for block in blocks:
                opened = await self._github.call(
                    "POST",
                    f"{self._repo(block.repository)}/issues",
                    json={
                        "title": block.title,
                        "body": _signed(block.body, output),
                        "labels": list(block.labels),
                    },
                    expect={201},
                )
                refs.append(str(opened.json()["html_url"]))
        except GitHubError as exc:
            raise DeliveryError(str(exc), refs=tuple(refs)) from exc
        return tuple(refs)


class GitHubCommentDelivery(_GitHubTarget[_CommentBlock]):
    """Each `~~~github_comment` block a comment on an existing issue or pull request."""

    target = "github_comment"
    block = _CommentBlock

    async def deliver(self, output: RunOutput, /) -> tuple[str, ...]:
        blocks = self._blocks(output)
        refs: list[str] = []
        try:
            for block in blocks:
                found = await self._github.call(
                    "GET",
                    f"{self._repo(block.repository)}/issues/{block.number}",
                    expect={200, 404},
                )
                if found.status_code == 404:
                    raise DeliveryError(
                        f"{block.repository} has no issue or pull request #{block.number}"
                    )
            for block in blocks:
                posted = await self._github.call(
                    "POST",
                    f"{self._repo(block.repository)}/issues/{block.number}/comments",
                    json={"body": _signed(block.body, output)},
                    expect={201},
                )
                refs.append(str(posted.json()["html_url"]))
        except GitHubError as exc:
            raise DeliveryError(str(exc), refs=tuple(refs)) from exc
        return tuple(refs)


class Deliveries:
    """The registry a finished run is handed to, whatever started it."""

    def __init__(self, registry: Mapping[OutputTarget, Delivery], metrics: Metrics) -> None:
        self._registry = dict(registry)
        self._metrics = metrics

    def serves(self, target: OutputTarget) -> bool:
        """True when this trigger is set up to deliver to the target."""
        return target in self._registry

    async def aclose(self) -> None:
        for delivery in self._registry.values():
            await delivery.aclose()

    async def deliver(self, targets: Sequence[OutputTarget], output: RunOutput) -> Delivered:
        """Every declared target in order, once every one of them has checked the output.

        A target whose check fails keeps every target from being written to:
        nothing leaves before all of them hold. Past the checks, one that
        refuses does not keep the next from trying.
        """
        refusals = self._checked(targets, output)
        if refusals:
            for refusal in refusals:
                self._metrics.deliveries.labels(output.use_case, refusal.target, "failed").inc()
            return Delivered((), (), tuple(refusals))
        refs: list[str] = []
        states: list[OutputState | None] = []
        for target in targets:
            delivery = self._registry[target]
            refused = True
            try:
                created = await delivery.deliver(output)
                refused = False
            except DeliveryError as exc:
                logger.error(
                    "%s run %d: %s refused the output: %s",
                    output.use_case,
                    output.run_id,
                    target,
                    exc,
                )
                created = exc.refs
                refusals.append(Refusal(target, str(exc)))
            except Exception as exc:
                # A fault in a delivery is still a failed run with its alert, never
                # a crash the redelivery would report as a restarted pod.
                logger.exception(
                    "%s run %d: delivering to %s broke", output.use_case, output.run_id, target
                )
                created = ()
                refusals.append(Refusal(target, f"{type(exc).__name__}: {describe(exc)}"))
            refs.extend(created)
            states.extend(delivery.state for _ in created)
            outcome = "failed" if refused else "sent"
            self._metrics.deliveries.labels(output.use_case, target, outcome).inc()
        return Delivered(tuple(refs), tuple(states), tuple(refusals))

    def _checked(self, targets: Sequence[OutputTarget], output: RunOutput) -> list[Refusal]:
        """Every target's check of the output; the refusals."""
        refusals: list[Refusal] = []
        for target in targets:
            try:
                self._registry[target].check(output)
            except DeliveryError as exc:
                logger.error(
                    "%s run %d: %s refused the output: %s",
                    output.use_case,
                    output.run_id,
                    target,
                    exc,
                )
                refusals.append(Refusal(target, str(exc)))
        return refusals


# One entry per target this trigger delivers; the GitHub targets share the App.
_REGISTRY: dict[OutputTarget, Callable[[Settings, GitHubApp | None], Delivery]] = {
    "stored": lambda _settings, _github: StoredDelivery(),
    "discord": lambda settings, _github: DiscordDelivery(settings),
    "mail": lambda settings, _github: MailDelivery(settings),
    "wiki_page": lambda settings, _github: WikiPageDelivery(settings),
    "github_pr": lambda _settings, github: GitHubPullRequestDelivery(github),
    "github_issue": lambda _settings, github: GitHubIssueDelivery(github),
    "github_comment": lambda _settings, github: GitHubCommentDelivery(github),
}


def build_deliveries(
    settings: Settings,
    use_cases: Mapping[str, UseCase],
    metrics: Metrics,
    github: GitHubApp | None,
) -> Deliveries:
    """The deliveries the enabled event and schedule use cases declare; a gap is a ``ValueError``.

    Those two kinds are the runs the trigger closes: an event run it started
    itself, a cron run it hears of through the hook. A chat is answered by the
    harness in its own conversation. Every other target this trigger can
    deliver is built too when its settings are there, so a hand-fed run of a
    dormant use case reaches it; without them it is left out, and a hand-fed
    run that needs it is refused.
    """
    required: dict[OutputTarget, list[str]] = {}
    for use_case in use_cases.values():
        if use_case.event_trigger is None and not use_case.is_schedule:
            continue
        for target in use_case.output:
            required.setdefault(target, []).append(use_case.name)
    for target, names in required.items():
        if target not in _REGISTRY:
            raise ValueError(
                f"use case {', '.join(names)} declares output {target},"
                " which this trigger does not deliver"
            )

    registry: dict[OutputTarget, Delivery] = {}
    for target, factory in _REGISTRY.items():
        try:
            registry[target] = factory(settings, github)
        except ValueError as exc:
            if target in required:
                raise
            logger.info(
                "output %s is not set up, and a hand-fed run needing it is refused: %s", target, exc
            )
    return Deliveries(registry, metrics)
