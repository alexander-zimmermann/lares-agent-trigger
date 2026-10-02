"""Delivery: the trigger carries a completed run's output to the targets its use case declares.

The model never delivers — on an API run the harness posts nothing itself. The
registry below holds one delivery per output target. `stored` is the ledger
row, which the event path writes before any other target sees the text; the
others post the text somewhere a person reads it and name what they created,
so the row can record it in `output_ref`. A target that refuses fails the run:
the others are still tried, the stored text stays, and the refusal's raw text
becomes the row's error.

Only the targets an enabled use case declares are built, and a declared target
without its settings — or without a delivery at all — stops the pod at
startup, the same rule as a use-case file that does not validate.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from importlib.metadata import version
from typing import Protocol

import httpx

from .config import Settings
from .events import Occasion
from .failures import describe
from .faults import first_clause, load_fault_sentences
from .ledger import Usage
from .metrics import Metrics
from .use_cases import OutputTarget, UseCase

logger = logging.getLogger(__name__)

_DISCORD_API = "https://discord.com/api/v10"
# Discord refuses a message longer than this.
_DISCORD_LIMIT = 2000
# How the skill marks a proof line; Discord renders it small and grey.
_PROOF_PREFIX = "-# "


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
    # The event or the request the run was started for.
    occasion: Occasion
    text: str
    usage: Usage


class Delivery(Protocol):
    """One output target."""

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
    """What one run's targets made of it: the refs they created, and who refused."""

    refs: tuple[str, ...]
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

    async def deliver(self, _output: RunOutput, /) -> tuple[str, ...]:
        return ()

    async def aclose(self) -> None:
        return None


class DiscordDelivery:
    """The home channel, through the bot's REST API with the token the harness chats with."""

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

    async def deliver(self, output: RunOutput, /) -> tuple[str, ...]:
        """Post the text as `discord_messages` splits it; one ref per message posted."""
        refs: list[str] = []
        for message in discord_messages(output.text, output.run_id):
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
        episode = output.occasion
        sentence = self._sentences.get(episode.fault)
        # An episode can outlive its fault's entry; the fault's name still says what it was.
        what = first_clause(sentence) if sentence else episode.fault
        message = EmailMessage()
        message["Subject"] = f"[Explain] {what} · {episode.subject}"
        message["From"] = self._settings.mail_from
        message["To"] = self._settings.mail_to
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=self._domain)
        link = self._settings.dashboard_episode_url.format(
            episode_id=episode.episode_id, fault=episode.fault
        )
        body = _as_mail(output.text.strip())
        message.set_content(f"{body}\n\n-- \n{_footer(output.usage)}\n{link}\n")
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


class Deliveries:
    """The registry the event path hands a completed run to."""

    def __init__(self, registry: Mapping[OutputTarget, Delivery], metrics: Metrics) -> None:
        self._registry = dict(registry)
        self._metrics = metrics

    async def aclose(self) -> None:
        for delivery in self._registry.values():
            await delivery.aclose()

    async def deliver(self, targets: Sequence[OutputTarget], output: RunOutput) -> Delivered:
        """Every declared target in order; one that refuses does not keep the next from trying."""
        refs: list[str] = []
        refusals: list[Refusal] = []
        for target in targets:
            try:
                refs.extend(await self._registry[target].deliver(output))
            except DeliveryError as exc:
                logger.error(
                    "%s run %d: %s refused the output: %s",
                    output.use_case,
                    output.run_id,
                    target,
                    exc,
                )
                refs.extend(exc.refs)
                refusals.append(Refusal(target, str(exc)))
            except Exception as exc:
                # A fault in a delivery is still a failed run with its alert, never
                # a crash the redelivery would report as a restarted pod.
                logger.exception(
                    "%s run %d: delivering to %s broke", output.use_case, output.run_id, target
                )
                refusals.append(Refusal(target, f"{type(exc).__name__}: {describe(exc)}"))
            else:
                self._metrics.deliveries.labels(output.use_case, target, "sent").inc()
                continue
            self._metrics.deliveries.labels(output.use_case, target, "failed").inc()
        return Delivered(tuple(refs), tuple(refusals))


# One entry per target this trigger delivers.
_REGISTRY: dict[OutputTarget, Callable[[Settings], Delivery]] = {
    "stored": lambda _settings: StoredDelivery(),
    "discord": DiscordDelivery,
    "mail": MailDelivery,
}


def build_deliveries(
    settings: Settings, use_cases: Mapping[str, UseCase], metrics: Metrics
) -> Deliveries:
    """The deliveries the enabled event use cases declare; anything missing is a ``ValueError``.

    Only event use cases count: the trigger closes their rows, while a chat or
    cron run is answered by the harness in its own conversation.
    """
    declared: dict[OutputTarget, list[str]] = {}
    for use_case in use_cases.values():
        if use_case.event_trigger is None:
            continue
        for target in use_case.output:
            declared.setdefault(target, []).append(use_case.name)

    registry: dict[OutputTarget, Delivery] = {}
    for target, names in declared.items():
        factory = _REGISTRY.get(target)
        if factory is None:
            raise ValueError(
                f"use case {', '.join(names)} declares output {target},"
                " which this trigger does not deliver"
            )
        registry[target] = factory(settings)
    return Deliveries(registry, metrics)
