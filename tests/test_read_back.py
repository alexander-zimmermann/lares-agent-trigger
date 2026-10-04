"""The read-back: where the pull requests the ledger holds as open stand on GitHub now.

Rows are written as a delivery leaves them — a pull request's ref beside the
state `open`, a message's ref beside no state — and GitHub is the fake of
`fake_github.py`, where a person has merged one pull request and closed
another since.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import pytest_asyncio
import respx

from lares_agent_trigger.config import Settings
from lares_agent_trigger.github import GitHubApp, github_app
from lares_agent_trigger.ledger import Ledger
from lares_agent_trigger.metrics import Metrics
from lares_agent_trigger.read_back import ReadBack

from .fake_github import FakeGitHub, FakeRepo
from .fakes import sample

Rows = Callable[[], list[dict[str, Any]]]
Execute = Callable[..., None]

pytestmark = pytest.mark.respx(assert_all_called=False)

DISCORD_REF = "discord:1548229055348736034/1548300000000000001"


@pytest_asyncio.fixture
async def ledger(settings: Settings) -> AsyncIterator[Ledger]:
    opened = Ledger(settings)
    await opened.open()
    try:
        yield opened
    finally:
        await opened.close()


@pytest_asyncio.fixture
async def app(settings: Settings, github: FakeGitHub) -> AsyncIterator[GitHubApp]:
    made = github_app(settings)
    assert made is not None
    try:
        yield made
    finally:
        await made.aclose()


@pytest.fixture
def lares(github: FakeGitHub) -> FakeRepo:
    return github.add_repo("lares")


def _row(execute: Execute, use_case: str, refs: list[str], states: list[str | None]) -> None:
    execute(
        "INSERT INTO agent_runs (use_case, trigger, subject_kind, subject_key, status,"
        " output_ref, output_state) VALUES (%s, 'schedule', 'none', %s, 'completed', %s, %s)",
        use_case,
        f"job:{len(refs)}:{refs[0]}",
        refs,
        states,
    )


async def test_merged_and_closed_pull_requests_are_read_back_into_their_rows(
    ledger: Ledger,
    app: GitHubApp,
    github: FakeGitHub,
    lares: FakeRepo,
    rows: Rows,
    execute: Execute,
) -> None:
    merged, closed, still_open = (
        github.open_pull(lares, title) for title in ("Dryer", "Washer", "Freezer")
    )
    github.merge(lares, merged)
    github.close(lares, closed)
    _row(
        execute,
        "propose-faults",
        [DISCORD_REF, lares.html("pull", merged), lares.html("pull", closed)],
        [None, "open", "open"],
    )
    _row(execute, "propose-faults", [lares.html("pull", still_open)], ["open"])
    metrics = Metrics()

    await ReadBack(ledger, app, metrics, interval_seconds=86400).run_once()

    first, second = rows()
    # Position for position: the message keeps no state, the pull requests theirs.
    assert first["output_state"] == [None, "merged", "closed"]
    assert second["output_state"] == ["open"]
    for state in ("merged", "closed", "open"):
        assert (
            sample(
                metrics, "agent_trigger_read_backs_total", use_case="propose-faults", state=state
            )
            == 1.0
        )
    # Reading writes nothing on GitHub.
    assert github.writes == []


async def test_a_pull_request_github_does_not_answer_for_stays_open_and_is_counted(
    ledger: Ledger,
    app: GitHubApp,
    github: FakeGitHub,
    lares: FakeRepo,
    rows: Rows,
    execute: Execute,
) -> None:
    merged = github.open_pull(lares, "Dryer")
    github.merge(lares, merged)
    # Deleted, or a repository the App was taken off.
    _row(execute, "propose-faults", [lares.html("pull", 4711)], ["open"])
    _row(execute, "propose-faults", ["not a pull request"], ["open"])
    _row(execute, "propose-faults", [lares.html("pull", merged)], ["open"])
    metrics = Metrics()

    await ReadBack(ledger, app, metrics, interval_seconds=86400).run_once()

    gone, unreadable, read = rows()
    assert gone["output_state"] == ["open"]
    assert unreadable["output_state"] == ["open"]
    # One that failed does not keep the next from being read.
    assert read["output_state"] == ["merged"]
    assert (
        sample(metrics, "agent_trigger_read_backs_total", use_case="propose-faults", state="failed")
        == 2.0
    )


async def test_the_read_back_comes_round_again_and_outlives_a_github_outage(
    ledger: Ledger,
    app: GitHubApp,
    github: FakeGitHub,
    lares: FakeRepo,
    rows: Rows,
    execute: Execute,
    respx_mock: respx.MockRouter,
) -> None:
    number = github.open_pull(lares, "Dryer")
    _row(execute, "propose-faults", [lares.html("pull", number)], ["open"])
    # The first round finds GitHub down, even for a token.
    github.unavailable = 1
    metrics = Metrics()
    reading = asyncio.create_task(
        ReadBack(ledger, app, metrics, interval_seconds=0.05).keep_reading()
    )
    try:
        for _ in range(100):
            if sample(
                metrics, "agent_trigger_read_backs_total", use_case="propose-faults", state="open"
            ):
                break
            await asyncio.sleep(0.01)
        github.merge(lares, number)
        for _ in range(100):
            if rows()[0]["output_state"] == ["merged"]:
                break
            await asyncio.sleep(0.01)
    finally:
        reading.cancel()

    assert rows()[0]["output_state"] == ["merged"]
    assert (
        sample(metrics, "agent_trigger_read_backs_total", use_case="propose-faults", state="failed")
        >= 1.0
    )
