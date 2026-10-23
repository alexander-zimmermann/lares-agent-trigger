"""The owner's corrections of a repo skill, refused on the read-only mount, as pull requests."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from lares_agent_trigger.config import Settings
from lares_agent_trigger.metrics import Metrics

from .fake_github import FakeGitHub
from .fakes import PERSONS, refused_skill_write, sample, sign, until

Receiver = tuple[httpx.AsyncClient, Metrics]

HOOK = "/hooks/hermes"
SKILLS = "kubernetes/applications/hermes/base/skills"
SKILL_PATH = f"{SKILLS}/lares-explain/SKILL.md"
HEATING_PATH = f"{SKILLS}/lares-explain/references/heating.md"
PROPOSE_PATH = f"{SKILLS}/lares-propose/SKILL.md"
# The repo skill as main holds it; the captured call patches two places of it
# and the heading of its heating reference.
SKILL = (
    "---\n"
    "name: lares-explain\n"
    "description: Explains an episode of the house Lares.\n"
    "---\n"
    "\n"
    "Four steps, each with one tool call. The cap\n"
    "of ten tool calls is a ceiling, not a target.\n"
)
HEATING = "# Burner\n\nThe burner's modulation, in percent.\n"
PROPOSE = "---\nname: lares-propose\ndescription: Proposes faults.\n---\n\nOne proposal a week.\n"
FILES = {SKILL_PATH: SKILL, HEATING_PATH: HEATING, PROPOSE_PATH: PROPOSE}
CORRECTED = SKILL.replace("of ten tool calls", "of twelve tool calls").replace(
    "with one tool call", "with one or two tool calls"
)
BRANCH = "agent/skill/lares-explain"
# A further correction of the same skill, as one operation.
FIVE_STEPS = {
    "action": "patch",
    "name": "lares-explain",
    "old_string": "Four steps",
    "new_string": "Five steps",
}

pytestmark = pytest.mark.respx(assert_all_called=False)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The declaration with persons in it: kim the owner (`discord: admin`), lou not."""
    settings.persons_file.write_text(PERSONS, encoding="utf-8")
    return settings


async def _deliver(client: httpx.AsyncClient, body: bytes) -> None:
    response = await client.post(HOOK, content=body, headers=sign(body))
    assert response.status_code == 200, response.text


def _pulled(metrics: Metrics, outcome: str) -> float:
    return sample(metrics, "agent_trigger_skill_pull_requests_total", outcome=outcome)


async def test_the_owners_refused_write_opens_a_pull_request(
    receiver: Receiver, github: FakeGitHub
) -> None:
    """The captured call: a patch chain on SKILL.md and a patch on a reference, one commit each."""
    client, metrics = receiver
    repo = github.add_repo("lares", FILES)

    await _deliver(client, refused_skill_write(profile="kim"))
    await until(lambda: repo.pulls)

    (pull,) = repo.pulls.values()
    assert pull["head"]["ref"] == BRANCH
    assert pull["base"]["ref"] == "main"
    assert pull["title"] == "fix(hermes): the lares-explain skill as kim corrected it in chat"
    assert pull["labels"] == [{"name": "agent/proposal"}]
    assert repo.tree(BRANCH)[SKILL_PATH] == CORRECTED
    assert repo.tree(BRANCH)[HEATING_PATH] == HEATING.replace("# Burner", "# Heating: Burner")
    assert repo.tree("main") == FILES
    assert _pulled(metrics, "opened") == 1.0


async def test_one_call_over_two_repo_skills_opens_a_pull_request_for_each(
    receiver: Receiver, github: FakeGitHub
) -> None:
    """A skill the profile made itself is no repo skill: its operation is none of the change."""
    client, metrics = receiver
    repo = github.add_repo("lares", FILES)

    await _deliver(
        client,
        refused_skill_write(
            profile="kim",
            operations=[
                {"action": "create", "name": "kitchen-timer", "content": "---\nname: k\n---\n"},
                FIVE_STEPS,
                {
                    "action": "patch",
                    "name": "lares-propose",
                    "old_string": "One proposal",
                    "new_string": "Two proposals",
                },
            ],
        ),
    )
    await until(lambda: len(repo.pulls) == 2)

    assert {pull["head"]["ref"] for pull in repo.pulls.values()} == {
        BRANCH,
        "agent/skill/lares-propose",
    }
    assert repo.tree(BRANCH)[SKILL_PATH] == SKILL.replace("Four steps", "Five steps")
    assert repo.tree("agent/skill/lares-propose")[PROPOSE_PATH] == PROPOSE.replace(
        "One proposal", "Two proposals"
    )
    assert not any("kitchen-timer" in path for path in repo.tree(BRANCH))
    assert _pulled(metrics, "opened") == 2.0


async def test_a_further_correction_is_a_commit_on_the_open_pull_request(
    receiver: Receiver, github: FakeGitHub
) -> None:
    client, metrics = receiver
    repo = github.add_repo("lares", FILES)
    await _deliver(client, refused_skill_write(profile="kim"))
    await until(lambda: repo.pulls)
    first = repo.branches[BRANCH]

    await _deliver(client, refused_skill_write(profile="kim", operations=[FIVE_STEPS]))
    await until(lambda: repo.branches[BRANCH] != first)

    assert len(repo.pulls) == 1
    assert repo.tree(BRANCH)[SKILL_PATH] == CORRECTED.replace("Four steps", "Five steps")
    assert repo.tree("main") == FILES
    assert _pulled(metrics, "committed") == 1.0


async def test_a_whole_file_never_undoes_the_open_pull_requests_correction(
    receiver: Receiver, github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    """The harness wrote the whole file from the deployed skill, without the first correction."""
    client, metrics = receiver
    repo = github.add_repo("lares", FILES)
    await _deliver(client, refused_skill_write(profile="kim"))
    await until(lambda: repo.pulls)
    first = repo.branches[BRANCH]

    with caplog.at_level(logging.WARNING):
        await _deliver(
            client,
            refused_skill_write(
                profile="kim",
                operations=[
                    {"action": "patch", "name": "lares-explain", "content": SKILL + "M.\n"}
                ],
            ),
        )
        await until(lambda: "opens no pull request" in caplog.text)

    assert "a whole file would undo" in caplog.text
    assert repo.branches[BRANCH] == first
    assert repo.tree(BRANCH)[SKILL_PATH] == CORRECTED
    assert _pulled(metrics, "committed") == 0.0


async def test_after_a_closed_pull_request_the_next_correction_starts_from_main(
    receiver: Receiver, github: FakeGitHub
) -> None:
    """lares deletes a merged branch, never a closed one: the old branch is set back to main."""
    client, metrics = receiver
    repo = github.add_repo("lares", FILES)
    await _deliver(client, refused_skill_write(profile="kim"))
    await until(lambda: repo.pulls)
    (closed,) = repo.pulls
    github.close(repo, closed)

    await _deliver(client, refused_skill_write(profile="kim", operations=[FIVE_STEPS]))
    await until(lambda: len(repo.pulls) == 2)

    assert repo.pulls[closed]["state"] == "closed"
    assert repo.tree(BRANCH)[SKILL_PATH] == SKILL.replace("Four steps", "Five steps")
    assert repo.tree(BRANCH)[HEATING_PATH] == HEATING
    assert _pulled(metrics, "opened") == 2.0
    assert _pulled(metrics, "failed") == 0.0


@pytest.mark.parametrize(
    ("operations", "why"),
    [
        # Main moved past the first operation; the reference's patch fits and goes nowhere either.
        (None, "stands nowhere"),
        (
            [
                {
                    "action": "write_file",
                    "name": "lares-explain",
                    "file_path": "references/new.md",
                    "file_content": "New.\n",
                }
            ],
            "holds no such file",
        ),
        (
            [
                {
                    "action": "remove_file",
                    "name": "lares-explain",
                    "file_path": "references/heating.md",
                }
            ],
            "writes no text",
        ),
        (
            [
                {
                    "action": "write_file",
                    "name": "lares-explain",
                    "file_path": "../../../../../../.github/workflows/ci.yaml",
                    "file_content": "on: pull_request\n",
                }
            ],
            "no file inside the skill",
        ),
    ],
    ids=["main-moved", "new-file", "no-text", "outside-the-skill"],
)
async def test_a_correction_that_does_not_apply_opens_nothing(
    receiver: Receiver,
    github: FakeGitHub,
    caplog: pytest.LogCaptureFixture,
    operations: list[dict[str, Any]] | None,
    why: str,
) -> None:
    client, metrics = receiver
    moved = SKILL.replace("of ten tool calls", "of eight tool calls")
    workflow = ".github/workflows/ci.yaml"
    repo = github.add_repo("lares", {**FILES, SKILL_PATH: moved, workflow: "on: push\n"})

    with caplog.at_level(logging.WARNING):
        await _deliver(client, refused_skill_write(profile="kim", operations=operations))
        await until(lambda: "opens no pull request" in caplog.text)

    assert why in caplog.text
    assert github.writes == []
    assert repo.tree("main")[SKILL_PATH] == moved
    for outcome in ("opened", "committed", "failed"):
        assert _pulled(metrics, outcome) == 0.0


async def test_a_correction_github_does_not_take_counts_as_failed(
    receiver: Receiver, github: FakeGitHub, caplog: pytest.LogCaptureFixture
) -> None:
    """What the alert fires on: the owner's correction is lost unless someone looks."""
    client, metrics = receiver
    github.add_repo("lares", FILES)
    github.unavailable = 100

    await _deliver(client, refused_skill_write(profile="kim"))
    await until(lambda: _pulled(metrics, "failed"))

    assert "did not reach GitHub" in caplog.text
    assert github.writes == []
    assert _pulled(metrics, "failed") == 1.0


@pytest.mark.parametrize("profile", ["lou", "default"])
async def test_another_profiles_attempt_opens_nothing(
    receiver: Receiver, github: FakeGitHub, caplog: pytest.LogCaptureFixture, profile: str
) -> None:
    """A person who is no admin, or the machines' profile: Hermes's log keeps the attempt."""
    client, metrics = receiver
    github.add_repo("lares", FILES)

    with caplog.at_level(logging.INFO):
        await _deliver(client, refused_skill_write(profile=profile))
        await until(lambda: "only the owner's" in caplog.text)

    assert f"{profile}'s attempt" in caplog.text
    assert github.writes == []
    assert sample(metrics, "agent_trigger_hook_events_total", outcome="ignored") == 1.0
    for outcome in ("opened", "committed", "failed"):
        assert _pulled(metrics, outcome) == 0.0
