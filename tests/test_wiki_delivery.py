"""Delivery to the house wiki: a run's page block written to Wiki.js under the write key.

The use case here is the event use case of `USE_CASES` with `output: [stored,
wiki_page]`, so one episode event ends in a row and a page. Wiki.js is faked
over respx with the GraphQL answers of Wiki.js 2.5: `pages.list` for the
lookup, `pages.create` or `pages.update` for the write, a refusal in
`responseResult` rather than as a GraphQL error.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
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

from .conftest import USE_CASES
from .fakes import COMPLETED, fake_alertmanager, fake_hermes, sample

Publish = Callable[..., Awaitable[None]]
Rows = Callable[[], list[dict[str, Any]]]
Consumer = tuple[EpisodeConsumer, Metrics]

pytestmark = pytest.mark.respx(assert_all_called=False)

WIKI_URL = "http://wiki-js.test"
WIKI_TOKEN = "eyJhbGciOiJSUzI1NiJ9.write.token"

PAGE = (
    "Wartungsplan auf den Stand vom Oktober gebracht.\n"
    "\n"
    "---\n"
    "path: haus/wartungsplan\n"
    "title: Wartungsplan\n"
    "---\n"
    "# Wartungsplan\n"
    "\n"
    "- KWL-Filter: fällig 2026-11\n"
    "\n"
    "---\n"
    "\n"
    "Stand: 2026-10-03\n"
)
# What lands in the wiki: everything after the block, a rule inside it included.
CONTENT = "# Wartungsplan\n\n- KWL-Filter: fällig 2026-11\n\n---\n\nStand: 2026-10-03\n"


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    declared = tmp_path / "wiki-use-cases.yaml"
    declared.write_text(
        USE_CASES.replace("output: [stored, discord, mail]", "output: [stored, wiki_page]"),
        encoding="utf-8",
    )
    return settings.model_copy(
        update={"use_cases_file": declared, "wikijs_url": WIKI_URL, "wikijs_token": WIKI_TOKEN}
    )


def _listed(*pages: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"data": {"pages": {"list": list(pages)}}})


def _wiki(
    listed: httpx.Response, action: str, path: str, page_id: int, *, refused: str | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    """Wiki.js answering the lookup with `listed` and the write as Wiki.js 2.5 does.

    A write answers with the raw page row `createPage`/`updatePage` return: it
    has `localeCode`, never `locale`, so selecting a field the row lacks fails
    the answer after the write went through.
    """
    row = {"id": page_id, "path": path, "title": "Wartungsplan", "localeCode": "de"}

    def answer(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if "list(" in query:
            return listed
        if refused is not None:
            status = {"succeeded": False, "slug": "PageUpdateForbidden", "message": refused}
            result = {"responseResult": status, "page": None}
            return httpx.Response(200, json={"data": {"pages": {action: result}}})
        selection = re.search(r"page \{([^}]*)\}", query)
        assert selection is not None
        fields = selection.group(1).split()
        status = {"succeeded": True, "slug": "ok", "message": "ok"}
        missing = [field for field in fields if field not in row]
        if missing:
            errors = [
                {"message": f"Cannot return null for non-nullable field Page.{field}."}
                for field in missing
            ]
            result = {"responseResult": status, "page": None}
            return httpx.Response(200, json={"errors": errors, "data": {"pages": {action: result}}})
        page = {field: row[field] for field in fields}
        result = {"responseResult": status, "page": page}
        return httpx.Response(200, json={"data": {"pages": {action: result}}})

    return answer


def _bodies(route: Any) -> list[dict[str, Any]]:
    return [json.loads(call.request.content) for call in route.calls]


async def test_a_page_block_becomes_a_new_page(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": PAGE}])
    wiki = respx_mock.post(f"{WIKI_URL}/graphql").mock(
        side_effect=_wiki(_listed(), "create", "haus/wartungsplan", 31)
    )
    episode_consumer, metrics = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed"
    # The row keeps the whole output; its first line is the sentence.
    assert row["text"] == PAGE
    assert row["tldr"] == "Wartungsplan auf den Stand vom Oktober gebracht."
    assert row["output_ref"] == ["wiki:de/haus/wartungsplan"]
    assert all(
        call.request.headers["Authorization"] == f"Bearer {WIKI_TOKEN}" for call in wiki.calls
    )
    listed, created = _bodies(wiki)
    assert listed["variables"] == {"locale": "de"}
    assert "create(" in created["query"]
    assert created["variables"] == {
        "content": CONTENT,
        "locale": "de",
        "path": "haus/wartungsplan",
        "title": "Wartungsplan",
    }
    assert (
        sample(
            metrics,
            "agent_trigger_deliveries_total",
            use_case="explain-episode",
            target="wiki_page",
            outcome="sent",
        )
        == 1.0
    )


async def test_a_page_that_exists_keeps_what_the_block_does_not_name(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": PAGE}])
    existing = {
        "id": 31,
        "path": "haus/wartungsplan",
        "locale": "de",
        "description": "Was wann fällig ist",
        "isPublished": True,
        "tags": ["haus", "wartung"],
    }
    wiki = respx_mock.post(f"{WIKI_URL}/graphql").mock(
        side_effect=_wiki(
            _listed({**existing, "id": 1, "path": "home"}, existing),
            "update",
            "haus/wartungsplan",
            31,
        )
    )
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed"
    assert row["output_ref"] == ["wiki:de/haus/wartungsplan"]
    _, updated = _bodies(wiki)
    assert "update(" in updated["query"]
    # Wiki.js resets what an update leaves out: the flag would unpublish the page.
    assert updated["variables"] == {
        "id": 31,
        "content": CONTENT,
        "title": "Wartungsplan",
        "description": "Was wann fällig ist",
        "isPublished": True,
        "tags": ["haus", "wartung"],
    }


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        ("Nur ein Satz, keine Seite.", "no page block"),
        (
            "---\npath: haus/wartungsplan\ntitle: Wartungsplan\n---\n# Plan\n",
            "opens with its sentence",
        ),
        ("Ein Satz.\n\n---\npath: haus/wartungsplan\ntitle: Wartungsplan\n", "closing ---"),
        ("Ein Satz.\n\n---\npath: haus/wartungsplan\n---\n# Plan\n", "title"),
        (
            "Ein Satz.\n\n---\npath: haus/x\ntitle: X\ntags: [a]\n---\n# Plan\n",
            "tags",
        ),
        ("Ein Satz.\n\n---\npath: [haus]\ntitle: X\n---\n# Plan\n", "path"),
        ("Ein Satz.\n\n---\npath: haus/x\ntitle: X\n---\n\n", "content"),
        ("Ein Satz.\n\n---\npath: haus/x\ntitle: [unclosed\n---\n# Plan\n", "YAML"),
    ],
)
async def test_an_output_without_a_valid_page_block_fails_and_writes_nothing(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    respx_mock: respx.MockRouter,
    output: str,
    reason: str,
) -> None:
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": output}])
    alerted = fake_alertmanager(respx_mock)
    wiki = respx_mock.post(f"{WIKI_URL}/graphql")
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    assert row["text"] == output
    assert row["error"].startswith("wiki_page: ")
    assert reason in row["error"]
    assert not wiki.called
    (alert,) = json.loads(alerted.calls.last.request.content)
    assert alert["annotations"]["summary"] == (
        "explain-episode could not deliver episode 15510:appeared to wiki_page"
    )


async def test_a_page_the_wiki_refuses_fails_the_run_with_its_reason(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": PAGE}])
    fake_alertmanager(respx_mock)
    respx_mock.post(f"{WIKI_URL}/graphql").mock(
        side_effect=_wiki(
            _listed(), "create", "haus/wartungsplan", 0, refused="You are not authorized."
        )
    )
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    assert "PageUpdateForbidden" in row["error"]
    assert "You are not authorized." in row["error"]
    assert row["output_ref"] == []


async def test_a_wiki_that_cannot_be_reached_fails_the_run(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": PAGE}])
    fake_alertmanager(respx_mock)
    respx_mock.post(f"{WIKI_URL}/graphql").mock(side_effect=httpx.ConnectError("refused"))
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    assert "Wiki.js could not be reached" in row["error"]


def test_a_wiki_target_without_its_settings_refuses_to_start(settings: Settings) -> None:
    unconfigured = settings.model_copy(update={"wikijs_token": ""})

    with pytest.raises(ValueError, match="WIKIJS_TOKEN"):
        build_deliveries(unconfigured, load_use_cases(settings.use_cases_file), Metrics())


async def test_a_page_without_a_description_keeps_none(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter
) -> None:
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": PAGE}])
    draft = {
        "id": 31,
        "path": "haus/wartungsplan",
        "locale": "de",
        "description": None,
        "isPublished": False,
        "tags": [],
    }
    wiki = respx_mock.post(f"{WIKI_URL}/graphql").mock(
        side_effect=_wiki(_listed(draft), "update", "haus/wartungsplan", 31)
    )
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed"
    _, updated = _bodies(wiki)
    # Carried over as it is: a draft stays a draft.
    assert updated["variables"]["description"] is None
    assert updated["variables"]["isPublished"] is False
