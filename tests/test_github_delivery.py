"""GitHub deliveries: a run's fenced blocks opened as pull requests, issues and comments.

The use case here is the event use case of `USE_CASES` with its output switched
to `[stored, github_pr]` (or an issue or comment), so one episode event ends in
a row and whatever the run's blocks ask for, opened as the App. GitHub is the
fake of `fake_github.py`, with GitHub's own rules on the App's tokens, refs,
contents and pull requests.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from lares_agent_trigger.config import Settings
from lares_agent_trigger.consumer import EpisodeConsumer
from lares_agent_trigger.deliveries import build_deliveries
from lares_agent_trigger.github import github_app
from lares_agent_trigger.metrics import Metrics
from lares_agent_trigger.use_cases import load_use_cases

from .conftest import API_KEY, USE_CASES
from .fake_github import FakeGitHub, FakeRepo
from .fakes import (
    COMPLETED,
    CRON_SESSION,
    CRON_TASK,
    fake_alertmanager,
    fake_hermes,
    fake_job,
    model_call,
    sample,
    sign,
    turn_ended,
)

Publish = Callable[..., Awaitable[None]]
Rows = Callable[[], list[dict[str, Any]]]
Consumer = tuple[EpisodeConsumer, Metrics]
Receiver = tuple[httpx.AsyncClient, Metrics]

pytestmark = pytest.mark.respx(assert_all_called=False)

FAULTS_PATH = "kubernetes/applications/lares-diagnostics-engine/base/config/faults.yaml"

# An excerpt of the engine's fault list, as it stands on the default branch.
FAULTS_FILE = """\
faults:
  - name: appliance_runtime
    kind: duration
    devices:
      KG.Hauswirtschaftsraum.K1-L3.Entfeuchter:
        max_run_hours: 24
      KG.Hauswirtschaftsraum.K3-L1.Trockner:
        max_run_hours: 4
      KG.Hauswirtschaftsraum.K4-L1.Waschmaschine:
        max_run_hours: 16
"""

DIFF = """\
--- a/kubernetes/applications/lares-diagnostics-engine/base/config/faults.yaml
+++ b/kubernetes/applications/lares-diagnostics-engine/base/config/faults.yaml
@@ -6,5 +6,5 @@
         max_run_hours: 24
       KG.Hauswirtschaftsraum.K3-L1.Trockner:
-        max_run_hours: 4
+        max_run_hours: 5
       KG.Hauswirtschaftsraum.K4-L1.Waschmaschine:
"""

TITLE = "Let the dryer run five hours before appliance_runtime fires"

BODY = """\
Five of six judged dryer episodes in eight weeks were `nonsense`: eco runs take 4.2 to 4.6 h.

```diff
-        max_run_hours: 4
+        max_run_hours: 5
```"""


def _indented(text: str) -> str:
    return "\n".join(f"  {line}" if line else "" for line in text.splitlines())


def pull_request_block(
    *,
    title: str = TITLE,
    diff: str = DIFF,
    labels: str = "[topic/smart-home]",
    path: str = FAULTS_PATH,
    repository: str = "lares",
) -> str:
    return (
        "~~~github_pr\n"
        f"repository: {repository}\n"
        f"path: {path}\n"
        f"title: {title}\n"
        f"labels: {labels}\n"
        f"body: |\n{_indented(BODY)}\n"
        f"diff: |\n{_indented(diff)}\n"
        "~~~"
    )


def run_text(*blocks: str) -> str:
    return "One proposal for the dryer.\n\n" + "\n\n".join(blocks) + "\n"


# A use case that waits for its spec: nothing runs it, but a run can be fed to it by hand.
CATALOG_HYGIENE = """
  - name: catalog-hygiene
    sentence: Proposes fixes to the catalog's names as pull requests.
    trigger:
      kind: schedule
      cron: "0 4 * * 6"
    skill: lares-catalog-hygiene
    tools: [lares]
    output: [stored, github_pr]
    budget:
      tool_calls: 40
      minutes: 10
      runs_per_day: 1
    language: en
    memory: false
    dormant: Waits for its own spec.
"""


@pytest.fixture
def targets() -> str:
    """What the event use case declares; a test about issues or comments names its own."""
    return "[stored, github_pr]"


@pytest.fixture
def settings(settings: Settings, tmp_path: Path, targets: str) -> Settings:
    declared = tmp_path / "github-use-cases.yaml"
    declared.write_text(
        USE_CASES.replace("output: [stored, discord, mail]", f"output: {targets}").replace(
            "skill: lares-propose\n    tools: [lares]\n    output: [stored]",
            "skill: lares-propose\n    tools: [lares]\n    output: [stored, github_pr]",
        )
        + CATALOG_HYGIENE,
        encoding="utf-8",
    )
    # No Discord here: a hand-fed run of a use case that needs it is refused.
    return settings.model_copy(
        update={
            "use_cases_file": declared,
            "discord_bot_token": "",
            "wikijs_url": "http://wiki-js.test",
            "wikijs_token": "wiki-write-token",
        }
    )


@pytest.fixture
def lares(github: FakeGitHub) -> FakeRepo:
    return github.add_repo(
        "lares", {FAULTS_PATH: FAULTS_FILE}, labels=("agent/proposal", "topic/smart-home")
    )


async def test_a_proposal_block_becomes_a_pull_request_as_the_app(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": run_text(pull_request_block())}])
    episode_consumer, metrics = consumer
    main_before = lares.branches["main"]

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed", row["error"]
    (number,) = lares.pulls
    pull = lares.pulls[number]
    # A fresh branch off the default branch's head, one commit with the diff applied.
    branch = pull["head"]["ref"]
    assert branch == f"agent/explain-episode/run-{row['id']}-1"
    assert pull["base"]["ref"] == "main"
    assert lares.branches["main"] == main_before
    assert lares.tree(branch)[FAULTS_PATH] == FAULTS_FILE.replace(
        "max_run_hours: 4\n", "max_run_hours: 5\n"
    )
    assert pull["title"] == TITLE
    assert pull["body"].startswith(BODY)
    assert f"run {row['id']}" in pull["body"]
    assert [label["name"] for label in pull["labels"]] == ["agent/proposal", "topic/smart-home"]
    assert github.writes == [
        ("POST", "lares/git/refs"),
        ("PUT", f"lares/contents/{FAULTS_PATH}"),
        ("POST", "lares/pulls"),
        ("POST", f"lares/issues/{number}/labels"),
    ]
    # The row names the pull request, and its state starts open.
    assert row["output_ref"] == [pull["html_url"]]
    assert row["output_state"] == ["open"]
    assert (
        sample(
            metrics,
            "agent_trigger_deliveries_total",
            use_case="explain-episode",
            target="github_pr",
            outcome="sent",
        )
        == 1.0
    )


async def _fails(
    consumer: Consumer, publish: Publish, rows: Rows, respx_mock: respx.MockRouter, text: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one event whose output is `text`; its row and the AgentRunFailed that went out."""
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": text}])
    alerted = fake_alertmanager(respx_mock)
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    # The text stays on the row; the model is not asked again.
    assert row["text"] == text
    (alert,) = json.loads(alerted.calls.last.request.content)
    assert alert["labels"]["alertname"] == "AgentRunFailed"
    assert alert["annotations"]["summary"] == (
        "explain-episode could not deliver episode 15510:appeared to github_pr"
    )
    return row, alert


async def test_a_diff_that_does_not_apply_opens_nothing_and_raises_the_alert(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    # The model remembered the dryer at three hours; the file says four.
    stale = DIFF.replace("-        max_run_hours: 4", "-        max_run_hours: 3")

    row, alert = await _fails(
        consumer, publish, rows, respx_mock, run_text(pull_request_block(diff=stale))
    )

    assert "the diff does not apply" in row["error"]
    assert "hunk 1" in row["error"]
    assert "the diff does not apply" in alert["annotations"]["description"]
    # No branch, no commit, no pull request: nothing broken lands on GitHub.
    assert github.writes == []
    assert lares.pulls == {}
    assert row["output_ref"] == []


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Nothing to propose.\n", "holds no ~~~github_pr block"),
        (run_text("~~~github_pr\nrepository: lares\ntitle: [unclosed"), "never closed"),
        (run_text("~~~github_pr\nrepository: lares\n  title: x\n~~~"), "not valid YAML"),
        (
            run_text(pull_request_block().replace(f"title: {TITLE}\n", "")),
            "~~~github_pr block 1 does not hold: title: Field required",
        ),
        (
            run_text(pull_request_block(path="../secrets.yaml")),
            "path: Value error, a path inside the repository",
        ),
        (
            run_text(pull_request_block(repository="lares/../other")),
            "repository: String should match pattern",
        ),
        (run_text(*[pull_request_block()] * 4), "4 ~~~github_pr blocks; a run opens at most 3"),
        (
            run_text("~~~github_pr\nnone: nothing this week\n~~~", pull_request_block()),
            "with `none` stands alone",
        ),
    ],
    ids=[
        "no-block",
        "unclosed",
        "not-yaml",
        "no-title",
        "path-outside",
        "repository-path",
        "too-many",
        "none-beside-a-proposal",
    ],
)
async def test_a_missing_or_broken_block_fails_the_run_before_github_is_written(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
    text: str,
    reason: str,
) -> None:
    row, _ = await _fails(consumer, publish, rows, respx_mock, text)

    assert reason in row["error"]
    assert github.writes == []


async def test_a_label_the_repository_does_not_hold_opens_nothing(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    # A label the sync would delete again, or a typo: GitHub would create it on the fly.
    text = run_text(pull_request_block(labels="[topic/smarthome]"))

    row, _ = await _fails(consumer, publish, rows, respx_mock, text)

    assert "lares has no label topic/smarthome" in row["error"]
    assert github.writes == []
    assert lares.labels == {"agent/proposal", "topic/smart-home"}


async def test_a_repository_the_app_cannot_see_opens_nothing(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    text = run_text(pull_request_block(repository="private-notes"))

    row, _ = await _fails(consumer, publish, rows, respx_mock, text)

    assert "the App sees no repository private-notes" in row["error"]
    assert github.writes == []


async def test_a_run_with_nothing_to_propose_says_so_and_completes(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    text = "No fault has the evidence this week.\n\n~~~github_pr\nnone: no verdicts\n~~~\n"
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": text}])
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed", row["error"]
    assert row["output_ref"] == []
    assert github.writes == []


async def test_three_proposals_open_three_pull_requests_on_their_own_branches(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    washer = """\
@@ -9,2 +9,2 @@
       KG.Hauswirtschaftsraum.K4-L1.Waschmaschine:
-        max_run_hours: 16
+        max_run_hours: 18
"""
    # A new file, from /dev/null.
    readme = """\
--- /dev/null
+++ b/docs/agents/proposals.md
@@ -0,0 +1,2 @@
+# Proposals
+Opened by Propose, one per pull request.
"""
    text = run_text(
        pull_request_block(),
        pull_request_block(title="Let the washer run 18 hours", diff=washer, labels="[]"),
        pull_request_block(
            title="Explain what a proposal is", diff=readme, path="docs/agents/proposals.md"
        ),
    )
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": text}])
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed", row["error"]
    first, second, third = (lares.pulls[number] for number in sorted(lares.pulls))
    assert [pull["head"]["ref"] for pull in (first, second, third)] == [
        f"agent/explain-episode/run-{row['id']}-{n}" for n in (1, 2, 3)
    ]
    # Each one against the default branch as it stood, never on top of another.
    assert "max_run_hours: 4\n" in lares.tree(second["head"]["ref"])[FAULTS_PATH]
    assert "max_run_hours: 18\n" in lares.tree(second["head"]["ref"])[FAULTS_PATH]
    assert lares.tree(third["head"]["ref"])["docs/agents/proposals.md"] == (
        "# Proposals\nOpened by Propose, one per pull request.\n"
    )
    assert [label["name"] for label in second["labels"]] == ["agent/proposal"]
    assert row["output_ref"] == [pull["html_url"] for pull in (first, second, third)]
    assert row["output_state"] == ["open", "open", "open"]
    # One token for the whole run, minted once.
    assert len(github.minted) == 1


async def test_github_breaking_halfway_keeps_the_pull_request_it_opened(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    washer = """\
@@ -9,2 +9,2 @@
       KG.Hauswirtschaftsraum.K4-L1.Waschmaschine:
-        max_run_hours: 16
+        max_run_hours: 18
"""
    text = run_text(pull_request_block(), pull_request_block(title="Washer", diff=washer))

    def second_branch_fails(request: httpx.Request) -> httpx.Response | None:
        if request.method == "POST" and request.url.path.endswith("/git/refs") and lares.pulls:
            return httpx.Response(502, json={"message": "Server Error"})
        return None

    github.interject = second_branch_fails

    row, alert = await _fails(consumer, publish, rows, respx_mock, text)

    (number,) = lares.pulls
    assert row["output_ref"] == [lares.pulls[number]["html_url"]]
    assert row["output_state"] == ["open"]
    assert "GitHub answered 502 to POST /repos/alexander-zimmermann/lares/git/refs" in row["error"]


ISSUE = """\
The restore probe could not read the newest snapshot.

~~~github_issue
repository: lares
title: Restore probe failed on the TimescaleDB snapshot of 2026-10-04
labels: [topic/smart-home]
body: |
  `pbs-1/timescaledb` from 03:00 did not verify:

  ```
  verification failed: chunk 4f2a… missing
  ```
~~~
"""


@pytest.mark.parametrize("targets", ["[stored, github_issue]"])
async def test_an_issue_block_becomes_an_issue_with_its_labels(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": ISSUE}])
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed", row["error"]
    (issue,) = lares.issues.values()
    assert issue["title"] == "Restore probe failed on the TimescaleDB snapshot of 2026-10-04"
    assert issue["body"].startswith("`pbs-1/timescaledb` from 03:00 did not verify:\n\n```\n")
    assert f"run {row['id']}" in issue["body"]
    # An issue is no proposal: only the labels its block names.
    assert [label["name"] for label in issue["labels"]] == ["topic/smart-home"]
    assert github.writes == [("POST", "lares/issues")]
    # An issue has no state to follow.
    assert row["output_ref"] == [issue["html_url"]]
    assert row["output_state"] == [None]


@pytest.mark.parametrize("targets", ["[stored, github_issue]"])
async def test_an_issue_with_a_label_the_repository_lacks_is_not_opened(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(
        respx_mock,
        states=[{**COMPLETED, "output": ISSUE.replace("[topic/smart-home]", "[backup]")}],
    )
    fake_alertmanager(respx_mock)
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    assert "lares has no label backup" in row["error"]
    assert github.writes == []


def _comment(number: int) -> str:
    return (
        "Renovate's bump is safe.\n\n"
        "~~~github_comment\n"
        "repository: lares\n"
        f"number: {number}\n"
        "body: |\n"
        "  The chart's only change is the default image tag; nothing here overrides it.\n"
        "~~~\n"
    )


@pytest.mark.parametrize("targets", ["[stored, github_comment]"])
async def test_a_comment_block_is_posted_on_its_issue(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    number = github.open_issue(lares, "fix(helm): update chart rustfs (1.0.0 → 1.0.1)")
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": _comment(number)}])
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed", row["error"]
    (comment,) = lares.comments
    assert comment["issue_number"] == number
    assert comment["body"].startswith("The chart's only change is the default image tag")
    assert github.writes == [("POST", f"lares/issues/{number}/comments")]
    assert row["output_ref"] == [comment["html_url"]]
    assert row["output_state"] == [None]


@pytest.mark.parametrize("targets", ["[stored, github_comment]"])
async def test_a_comment_on_an_issue_that_does_not_exist_is_refused(
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": _comment(4711)}])
    fake_alertmanager(respx_mock)
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    assert "lares has no issue or pull request #4711" in row["error"]
    assert github.writes == []


async def _settled(rows: Rows) -> dict[str, Any]:
    """The one row, once whatever delivers it has closed it."""
    for _ in range(500):
        found = rows()
        if found and found[0]["status"] not in {"queued", "running"}:
            (row,) = found
            return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"the row never closed: {rows()}")


async def _cron_turn(client: httpx.AsyncClient, text: str) -> None:
    """A cron execution of `lares:propose-faults` reported through the hook, as the gateway does."""
    for body in (
        model_call(1, session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK, content=text),
        turn_ended(session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK),
    ):
        response = await client.post("/hooks/hermes", content=body, headers=sign(body))
        assert response.status_code == 200, response.text


async def test_a_cron_run_of_propose_opens_its_pull_request(
    receiver: Receiver,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    client, metrics = receiver

    await _cron_turn(client, run_text(pull_request_block()))

    row = await _settled(rows)
    assert row["status"] == "completed", row["error"]
    assert (row["use_case"], row["trigger"]) == ("propose-faults", "schedule")
    (pull,) = lares.pulls.values()
    assert pull["head"]["ref"] == f"agent/propose-faults/run-{row['id']}-1"
    assert "propose-faults, run" in pull["body"]
    assert row["output_ref"] == [pull["html_url"]]
    assert row["output_state"] == ["open"]
    assert row["finished_at"] is not None
    assert (
        sample(
            metrics,
            "agent_trigger_recorded_runs_total",
            use_case="propose-faults",
            status="completed",
        )
        == 1.0
    )


async def test_a_cron_run_without_its_block_fails_and_raises_the_alert(
    receiver: Receiver,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    alerted = fake_alertmanager(respx_mock)
    client, metrics = receiver

    await _cron_turn(client, "Raise the dryer to five hours, the diff is obvious.")

    row = await _settled(rows)
    assert row["status"] == "failed"
    assert "holds no ~~~github_pr block" in row["error"]
    assert github.writes == []
    (alert,) = json.loads(alerted.calls.last.request.content)
    assert alert["annotations"]["summary"] == (
        f"propose-faults could not deliver run {row['id']} to github_pr"
    )
    assert (
        sample(
            metrics, "agent_trigger_recorded_runs_total", use_case="propose-faults", status="failed"
        )
        == 1.0
    )


HAND = {"Authorization": f"Bearer {API_KEY}"}


async def test_a_hand_fed_run_is_delivered_like_a_run_without_a_model(
    api: Receiver,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    client, metrics = api
    text = run_text(pull_request_block())

    response = await client.post(
        "/api/runs", json={"use_case": "catalog-hygiene", "output": text}, headers=HAND
    )

    assert response.status_code == 200, response.text
    (row,) = rows()
    (pull,) = lares.pulls.values()
    assert response.json() == {
        "use_case": "catalog-hygiene",
        "run_id": row["id"],
        "status": "completed",
        "output_ref": [pull["html_url"]],
    }
    # A row of its own: started by a hand, on no subject, with the text it was fed.
    assert (row["trigger"], row["subject_kind"], row["status"]) == ("manual", "none", "completed")
    assert row["subject_key"].startswith("manual:")
    assert row["text"] == text
    assert row["tldr"] == "One proposal for the dryer."
    assert row["model"] is None
    assert row["output_state"] == ["open"]
    assert pull["head"]["ref"] == f"agent/catalog-hygiene/run-{row['id']}-1"
    assert [label["name"] for label in pull["labels"]] == ["agent/proposal", "topic/smart-home"]
    assert (
        sample(metrics, "agent_trigger_runs_total", use_case="catalog-hygiene", status="completed")
        == 1.0
    )


async def test_a_hand_fed_run_whose_block_fails_answers_why_and_raises_the_alert(
    api: Receiver,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    alerted = fake_alertmanager(respx_mock)
    client, _ = api
    stale = DIFF.replace("-        max_run_hours: 4", "-        max_run_hours: 3")

    response = await client.post(
        "/api/runs",
        json={"use_case": "catalog-hygiene", "output": run_text(pull_request_block(diff=stale))},
        headers=HAND,
    )

    assert response.status_code == 200
    answer = response.json()
    (row,) = rows()
    assert (answer["status"], answer["output_ref"]) == ("failed", [])
    assert "the diff does not apply" in answer["error"]
    assert row["status"] == "failed"
    assert github.writes == []
    (alert,) = json.loads(alerted.calls.last.request.content)
    assert alert["annotations"]["summary"] == (
        f"catalog-hygiene could not deliver run {row['id']} to github_pr"
    )


@pytest.mark.parametrize(
    ("body", "status_code", "reason"),
    [
        ({"use_case": "messenger", "output": "x"}, 400, "the chat itself"),
        ({"use_case": "catalog-hygiene", "output": "  "}, 400, "output: the text"),
        ({"use_case": "catalog-hygiene", "output": 42}, 400, "output: the text"),
        (
            {"use_case": "catalog-hygiene", "output": "x", "subject": "15510"},
            400,
            "takes no subject",
        ),
        ({"use_case": "nobody", "output": "x"}, 404, "no use case named nobody"),
        ({"use_case": "summarise-week", "output": "x"}, 409, "summarise-week declares discord"),
    ],
    ids=["chat", "blank", "not-text", "subject", "unknown", "not-served"],
)
async def test_a_hand_fed_run_that_cannot_be_delivered_is_refused(
    settings: Settings,
    api: Receiver,
    rows: Rows,
    body: dict[str, Any],
    status_code: int,
    reason: str,
) -> None:
    client, _ = api

    response = await client.post("/api/runs", json=body, headers=HAND)

    assert response.status_code == status_code
    assert reason in response.json()["error"]
    assert rows() == []


async def test_a_cron_run_that_completed_without_output_fails(
    receiver: Receiver,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    alerted = fake_alertmanager(respx_mock)
    client, _ = receiver

    for body in (
        model_call(1, session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK, tool_calls=2),
        turn_ended(session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK),
    ):
        assert (await client.post("/hooks/hermes", content=body, headers=sign(body))).is_success

    row = await _settled(rows)
    assert row["status"] == "failed"
    assert row["error"] == "the harness completed the run without output"
    assert alerted.called
    assert github.writes == []


def test_a_github_target_without_the_app_refuses_to_start(settings: Settings) -> None:
    unconfigured = settings.model_copy(
        update={
            "github_app_id": "",
            "github_app_installation_id": None,
            "github_app_private_key": "",
        }
    )
    assert github_app(unconfigured) is None

    with pytest.raises(ValueError, match="the github_pr output needs GITHUB_APP_ID"):
        build_deliveries(unconfigured, load_use_cases(settings.use_cases_file), Metrics(), None)


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"github_app_private_key": ""}, "needs GITHUB_APP_PRIVATE_KEY"),
        ({"github_app_installation_id": None}, "needs GITHUB_APP_INSTALLATION_ID"),
        ({"github_app_private_key": "a key someone pasted half of"}, "no PEM private key"),
    ],
    ids=["no-key", "no-installation", "not-a-key"],
)
def test_half_an_app_refuses_to_start(
    settings: Settings, update: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        github_app(settings.model_copy(update=update))


PAGE_AND_PROPOSAL = (
    "Wiki and fault list disagree on the dryer.\n"
    "\n"
    "---\n"
    "path: haus/geraete/trockner\n"
    "title: Trockner\n"
    "---\n"
    "# Trockner\n"
    "\n"
    "Läuft im Eco-Programm bis 4,6 h.\n"
    "\n"
)


@pytest.mark.parametrize("targets", ["[stored, wiki_page, github_pr]"])
async def test_a_page_and_a_proposal_go_out_each_without_the_others_block(
    settings: Settings,
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    wiki = respx_mock.post("http://wiki-js.test/graphql").mock(
        side_effect=[
            httpx.Response(200, json={"data": {"pages": {"list": []}}}),
            httpx.Response(
                200,
                json={
                    "data": {
                        "pages": {
                            "create": {
                                "responseResult": {
                                    "succeeded": True,
                                    "slug": "ok",
                                    "message": "ok",
                                },
                                "page": {"id": 7, "path": "haus/geraete/trockner"},
                            }
                        }
                    }
                },
            ),
        ]
    )
    text = PAGE_AND_PROPOSAL + pull_request_block() + "\n"
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": text}])
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "completed", row["error"]
    created = json.loads(wiki.calls.last.request.content)["variables"]
    # The page ends where the block the trigger opens on GitHub begins.
    assert created["content"] == "# Trockner\n\nLäuft im Eco-Programm bis 4,6 h.\n"
    assert len(lares.pulls) == 1


@pytest.mark.parametrize("targets", ["[stored, wiki_page, github_pr]"])
async def test_a_broken_block_keeps_every_target_from_being_written(
    settings: Settings,
    consumer: Consumer,
    publish: Publish,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    wiki = respx_mock.post("http://wiki-js.test/graphql")
    text = PAGE_AND_PROPOSAL + pull_request_block(path="../secrets.yaml") + "\n"
    fake_hermes(respx_mock, states=[{**COMPLETED, "output": text}])
    fake_alertmanager(respx_mock)
    episode_consumer, _ = consumer

    await publish("appeared", 2)
    await episode_consumer.run_once()

    (row,) = rows()
    assert row["status"] == "failed"
    assert "github_pr: ~~~github_pr block 1 does not hold" in row["error"]
    # Checked before anything leaves: the page is not written either.
    assert not wiki.called
    assert github.writes == []


async def test_a_cron_turn_whose_calls_a_restart_lost_fails_saying_so(
    receiver: Receiver,
    rows: Rows,
    github: FakeGitHub,
    lares: FakeRepo,
    respx_mock: respx.MockRouter,
) -> None:
    fake_job(respx_mock, name="lares:propose-faults")
    fake_alertmanager(respx_mock)
    client, _ = receiver
    # Its calls came in before this pod started; only the turn's end reaches it.
    body = turn_ended(session_id=CRON_SESSION, platform="cron", task_id=CRON_TASK)

    assert (await client.post("/hooks/hermes", content=body, headers=sign(body))).is_success

    row = await _settled(rows)
    assert row["status"] == "failed"
    assert row["error"] == (
        "the turn's answer came in before the trigger restarted and was lost with it"
    )
    assert github.writes == []
