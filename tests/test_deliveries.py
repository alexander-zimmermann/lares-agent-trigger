"""Delivery: a completed run's text carried to every target its use case declares.

The use case here declares what lares declares — `stored`, `discord`, `mail`
— so one episode event ends in a row, a message on the home channel and a
mail through the relay. A target that refuses fails the run, keeps the text
and raises AgentRunFailed; the model is never asked again for it.
"""

from __future__ import annotations

import email
import json
from collections.abc import Awaitable, Callable
from email.message import EmailMessage
from email.policy import default as default_policy
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from lares_agent_trigger.config import Settings
from lares_agent_trigger.consumer import EpisodeConsumer
from lares_agent_trigger.deliveries import build_deliveries
from lares_agent_trigger.metrics import Metrics
from lares_agent_trigger.use_cases import load_use_cases

from .conftest import (
    DISCORD_CHANNEL,
    DISCORD_TOKEN,
    HERMES_URL,
    MAIL_FROM,
    MAIL_TO,
    USE_CASES,
    Relay,
)
from .fakes import COMPLETED, EXPLANATION, SESSION, fake_alertmanager, fake_hermes, sample

Publish = Callable[..., Awaitable[None]]
Rows = Callable[[], list[dict[str, Any]]]
Consumer = tuple[EpisodeConsumer, Metrics]

pytestmark = pytest.mark.respx(assert_all_called=False)

SUBJECT = (
    "[Explain] Ein Gerät zieht ununterbrochen länger Strom, als seine je Gerät erlaubte"
    " Laufzeit zulässt · 2/1/197"
)


def _mail(relay: Relay) -> EmailMessage:
    (envelope,) = relay.messages
    assert isinstance(envelope.content, bytes)
    # CRLF is the wire's line ending, not the text's.
    wire = envelope.content.replace(b"\r\n", b"\n")
    message = email.message_from_bytes(wire, policy=default_policy)
    assert isinstance(message, EmailMessage)
    return message


def _long(head: str, proofs: int, width: int, tail: str) -> str:
    lines = [f"-# Befund {n}: " + "x" * width for n in range(proofs)]
    return f"{head}\n\n" + "\n".join(lines) + f"\n\n{tail}"


async def test_an_explanation_reaches_the_row_discord_and_mail(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    relay: Relay,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(respx_mock)
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed"
    assert row["text"] == EXPLANATION

    # The message as Discord sees it: the bot's token, the home channel, the
    # whole text — it fits — and no mention the model wrote can ping anyone.
    (call,) = discord.calls
    assert call.request.headers["Authorization"] == f"Bot {DISCORD_TOKEN}"
    assert call.request.headers["User-Agent"].startswith("DiscordBot (")
    body = json.loads(call.request.content)
    assert body["content"] == EXPLANATION
    assert body["allowed_mentions"] == {"parse": []}

    # The mail as the relay takes it: from its one accepted sender, to the owner.
    (envelope,) = relay.messages
    assert envelope.mail_from == "admin@zimmermann.sh"
    assert envelope.rcpt_tos == [MAIL_TO]
    mail = _mail(relay)
    assert mail["From"] == MAIL_FROM
    assert mail["To"] == MAIL_TO
    assert mail["Subject"] == SUBJECT
    assert mail.get_content_type() == "text/plain"
    text = mail.get_content()
    # The proof lines read as a list in a mail; `-# ` is Discord's markup.
    assert text.startswith(EXPLANATION.replace("\n-# ", "\n• "))
    assert "-# " not in text
    # The run's model and cost, which only the trigger knows, and the dashboard.
    assert "gpt-6-sol (openai-codex) · 4200 + 310 Tokens · 0.0210 USD · 11 s" in text
    assert "https://grafana.test/d/knx-episodes?var-fault=appliance_runtime" in text

    # One entry per delivery, in declaration order; `stored` is the row itself.
    message_id = json.loads(call.response.content)["id"]
    assert row["output_ref"] == [
        f"discord:{DISCORD_CHANNEL}/{message_id}",
        f"mail:{mail['Message-ID'].strip('<>')}",
    ]
    assert row["output_state"] == [None, None]
    for target in ("discord", "mail"):
        assert (
            sample(
                metrics,
                "agent_trigger_deliveries_total",
                use_case="explain-episode",
                target=target,
                outcome="sent",
            )
            == 1.0
        )


async def test_a_long_explanation_goes_to_discord_as_proof_and_follow_up(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    head = "Der Sensor im Büro ist seit Montag tot."
    tail = "Offen: " + "o" * 400
    text = _long(head, proofs=4, width=400, tail=tail)
    assert len(text) > 2000
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": text}])
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    first, second = (json.loads(call.request.content)["content"] for call in discord.calls)
    # The cause and every proof line first; the rest follows on its own.
    assert first.startswith(f"{head}\n\n-# Befund 0: ")
    assert first.count("-# Befund") == 4
    assert second == tail
    (row,) = rows()
    assert row["status"] == "completed"
    # One ref per message, the one carrying the cause first.
    ids = [json.loads(call.response.content)["id"] for call in discord.calls]
    assert row["output_ref"][:2] == [f"discord:{DISCORD_CHANNEL}/{i}" for i in ids]


async def test_proof_lines_too_long_for_one_message_point_to_the_run(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    text = _long("Die Heizung liefert nicht.", proofs=6, width=400, tail="Offen: nichts.")
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": text}])
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    messages = [json.loads(call.request.content)["content"] for call in discord.calls]
    assert all(len(message) <= 2000 for message in messages)
    # Cut on a line, and the stored row holds what was cut.
    assert messages[0].endswith(f"-# Befund 3: {'x' * 400}\n… (run {row['id']})")
    assert messages[1] == "Offen: nichts."
    assert row["status"] == "completed"


async def test_a_run_on_the_subscription_names_no_cost(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    relay: Relay,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(respx_mock)
    # The Codex subscription bills nothing per run, and the session says so.
    flat = {**SESSION["session"], "actual_cost_usd": 0, "estimated_cost_usd": 0}
    respx_mock.get(f"{HERMES_URL}/api/sessions/sess_1").mock(
        return_value=httpx.Response(200, json={**SESSION, "session": flat})
    )
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert float(row["cost"]) == 0.0
    text = _mail(relay).get_content()
    assert "gpt-6-sol (openai-codex) · 4200 + 310 Tokens · 11 s" in text
    assert "USD" not in text


async def test_a_fault_sentence_without_a_dash_is_the_subject_whole(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    relay: Relay,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(respx_mock)
    episode_consumer, _ = consumer

    await publish("appeared", 2, fault="channel_silence", subject="15/2/0")
    await episode_consumer.run_once()

    assert _mail(relay)["Subject"] == (
        "[Explain] Ein Kanal, den die Engine lange genug kennt und der sonst regelmäßig"
        " sendet, schweigt länger als das Fünffache der Sendepause, die er sonst in 19 von"
        " 20 Fällen einhält · 15/2/0"
    )


async def test_a_fault_the_list_no_longer_holds_is_named_by_its_name(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    relay: Relay,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(respx_mock)
    episode_consumer, _ = consumer

    await publish("appeared", 2, fault="freezer_icing", subject="2/2/227")
    await episode_consumer.run_once()

    assert _mail(relay)["Subject"] == "[Explain] freezer_icing · 2/2/227"


async def test_a_target_that_refuses_fails_the_run_and_keeps_the_text(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    relay: Relay,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    started = fake_hermes(respx_mock)
    alerted = fake_alertmanager(respx_mock)
    discord.mock(
        return_value=httpx.Response(403, json={"message": "Missing Access", "code": 50001})
    )
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    assert row["text"] == EXPLANATION
    assert row["tldr"] == "Die Waschmaschine hängt seit 14:20 im Spülgang."
    assert "discord" in row["error"]
    assert "403" in row["error"]
    assert "Missing Access" in row["error"]
    # The mail went out regardless, and its ref is recorded.
    assert len(relay.messages) == 1
    assert [ref.split(":")[0] for ref in row["output_ref"]] == ["mail"]
    # The model is not asked again for a message Discord would not take.
    assert started.call_count == 1

    (alert,) = json.loads(alerted.calls.last.request.content)
    assert alert["labels"]["alertname"] == "AgentRunFailed"
    assert alert["annotations"]["summary"] == (
        "explain-episode could not deliver episode 15510:appeared to discord"
    )
    assert "Missing Access" in alert["annotations"]["description"]
    assert (
        sample(
            metrics,
            "agent_trigger_failures_total",
            use_case="explain-episode",
            **{"class": "delivery_failed"},
        )
        == 1.0
    )
    assert (
        sample(metrics, "agent_trigger_runs_total", use_case="explain-episode", status="failed")
        == 1.0
    )
    assert (
        sample(
            metrics,
            "agent_trigger_deliveries_total",
            use_case="explain-episode",
            target="discord",
            outcome="failed",
        )
        == 1.0
    )


async def test_a_split_post_that_breaks_halfway_keeps_the_message_it_posted(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    relay: Relay,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    text = _long("Der Sensor im Büro ist seit Montag tot.", 4, 400, "Offen: " + "o" * 400)
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": text}])
    fake_alertmanager(respx_mock)
    discord.mock(
        side_effect=[
            httpx.Response(200, json={"id": "1548300000000000001", "channel_id": DISCORD_CHANNEL}),
            httpx.Response(502, text="upstream connect error"),
        ]
    )
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    assert "502" in row["error"]
    # The first message is on the channel, so the row names it.
    assert row["output_ref"][0] == f"discord:{DISCORD_CHANNEL}/1548300000000000001"
    assert row["output_ref"][1].startswith("mail:")


async def test_an_answer_without_a_message_id_is_a_refusal(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(respx_mock)
    fake_alertmanager(respx_mock)
    discord.mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    assert "without a message id" in row["error"]


async def test_a_completed_run_without_output_fails_and_is_not_delivered(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    relay: Relay,
    discord: Any,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": None}])
    alerted = fake_alertmanager(respx_mock)
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    assert row["error"] == "the harness completed the run without output"
    assert row["text"] is None
    assert not discord.called
    assert relay.messages == []
    assert alerted.called


async def test_a_relay_that_refuses_fails_the_run(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    relay: Relay,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(respx_mock)
    alerted = fake_alertmanager(respx_mock)
    relay.refuse = "554 5.7.1 Message rejected by upstream"
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    assert "mail" in row["error"]
    assert "Message rejected by upstream" in row["error"]
    assert [ref.split(":")[0] for ref in row["output_ref"]] == ["discord"]
    (alert,) = json.loads(alerted.calls.last.request.content)
    assert alert["annotations"]["summary"] == (
        "explain-episode could not deliver episode 15510:appeared to mail"
    )


def test_a_declared_target_without_its_settings_refuses_to_start(settings: Settings) -> None:
    use_cases = load_use_cases(settings.use_cases_file)
    unconfigured = settings.model_copy(update={"discord_bot_token": ""})

    with pytest.raises(ValueError, match="DISCORD_BOT_TOKEN"):
        build_deliveries(unconfigured, use_cases, Metrics())


def test_a_declared_target_this_trigger_cannot_deliver_refuses_to_start(
    settings: Settings, tmp_path: Path
) -> None:
    declared = tmp_path / "wiki.yaml"
    declared.write_text(
        USE_CASES.replace("output: [stored, discord, mail]", "output: [stored, wiki_page]"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="wiki_page"):
        build_deliveries(settings, load_use_cases(declared), Metrics())
