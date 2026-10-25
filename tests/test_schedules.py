"""The cron job set end to end: the declaration in, the harness's job list brought in line."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx

from lares_agent_trigger.cron_jobs import CronJob, declared_jobs
from lares_agent_trigger.schedules import Schedules
from lares_agent_trigger.use_cases import load_use_case_file

from .conftest import USE_CASES, Service
from .fakes import FakeJobs, sample

# The fake registers every route of the Jobs API; a test uses a few.
pytestmark = pytest.mark.respx(assert_all_called=False)

FOREIGN = "Tägliche Erinnerung"


async def test_the_reconcile_brings_the_harness_in_line_with_the_rendering(
    service: Service, respx_mock: respx.MockRouter
) -> None:
    jobs = FakeJobs(respx_mock)
    foreign = jobs.add(FOREIGN, schedule="0 7 * * *", prompt="Erinnere mich an den Müll.")
    before = dict(jobs.jobs[foreign])
    stale = jobs.add("lares:propose-faults", schedule="0 4 * * 0")
    retired = jobs.add("lares:retired-use-case")

    await service.schedules.keep_reconciling()

    (probe,) = jobs.named("lares:restore-probe")
    (digest,) = jobs.named("lares:skill-digest")
    # In file order: the changed job updated in place, the missing ones created,
    # then the managed job nothing declares any more deleted.
    assert jobs.changes == [
        ("PATCH", stale),
        ("POST", probe["id"]),
        ("POST", digest["id"]),
        ("DELETE", retired),
    ]
    assert jobs.jobs[foreign] == before
    (propose,) = jobs.named("lares:propose-faults")
    assert propose["id"] == stale
    assert propose["schedule"]["expr"] == "0 3 * * 0"
    assert probe["schedule"]["expr"] == "0 8 * * 1"
    assert probe["skills"] == ["lares-restore-probe"]
    assert probe["prompt"] == "Answer in English. Stay within 30 tool calls and 10 minutes."
    assert probe["deliver"] == "local"
    # The script job: no prompt and no skill, the harness runs the script and delivers.
    assert digest["schedule"]["expr"] == "0 18 * * 0"
    assert digest["script"] == "skill-digest.py"
    assert digest["no_agent"] is True
    assert digest["prompt"] == ""
    assert digest["skills"] == []
    assert digest["deliver"] == "discord"

    metrics = service.metrics
    assert sample(metrics, "agent_trigger_cron_jobs_total", action="created") == 2.0
    for action in ("updated", "deleted"):
        assert sample(metrics, "agent_trigger_cron_jobs_total", action=action) == 1.0
    assert sample(metrics, "agent_trigger_reconciles_total", outcome="done") == 1.0


async def test_a_script_jobs_changed_script_is_updated_in_place(
    service: Service, respx_mock: respx.MockRouter
) -> None:
    jobs = FakeJobs(respx_mock)
    stale = jobs.add(
        "lares:skill-digest",
        schedule="0 18 * * 0",
        prompt="",
        skills=(),
        deliver="discord",
        script="digest.py",
        no_agent=True,
    )

    await service.schedules.keep_reconciling()

    assert ("PATCH", stale) in jobs.changes
    assert jobs.jobs[stale]["script"] == "skill-digest.py"
    assert jobs.jobs[stale]["no_agent"] is True


async def test_a_job_as_declared_is_left_as_it_is(
    service: Service, respx_mock: respx.MockRouter
) -> None:
    """Rewriting an unchanged schedule would drop an occurrence the harness has not run yet."""
    jobs = FakeJobs(respx_mock)

    await service.schedules.keep_reconciling()
    created = list(jobs.changes)
    await service.schedules.keep_reconciling()

    assert jobs.changes == created
    assert [method for method, _ in created] == ["POST", "POST", "POST"]


async def test_a_paused_job_is_found_and_stays_paused(
    service: Service, respx_mock: respx.MockRouter
) -> None:
    """Pausing a managed job is a person's lever; the reconcile neither hides nor undoes it."""
    jobs = FakeJobs(respx_mock)
    paused = jobs.add("lares:propose-faults", enabled=False)

    await service.schedules.keep_reconciling()

    (propose,) = jobs.named("lares:propose-faults")
    assert propose["id"] == paused
    assert propose["enabled"] is False
    assert ("PATCH", paused) not in jobs.changes


async def test_a_managed_name_held_twice_keeps_one_job(
    service: Service, respx_mock: respx.MockRouter
) -> None:
    jobs = FakeJobs(respx_mock)
    first = jobs.add("lares:propose-faults")
    second = jobs.add("lares:propose-faults")

    await service.schedules.keep_reconciling()

    assert [job["id"] for job in jobs.named("lares:propose-faults")] == [first]
    assert ("DELETE", second) in jobs.changes


async def test_a_harness_that_does_not_answer_is_asked_again(
    service: Service, respx_mock: respx.MockRouter
) -> None:
    """The reconcile runs once per pod; a harness rolling at the same moment must not lose it."""
    jobs = FakeJobs(respx_mock)
    jobs.unavailable = 2

    await service.schedules.keep_reconciling()

    assert len(jobs.named("lares:propose-faults")) == 1
    assert sample(service.metrics, "agent_trigger_reconciles_total", outcome="failed") == 2.0
    assert sample(service.metrics, "agent_trigger_reconciles_total", outcome="done") == 1.0


async def test_a_job_set_the_harness_refuses_is_not_asked_again(
    service: Service, respx_mock: respx.MockRouter
) -> None:
    """A refusal changes only with a new commit, which rolls the pod: asking again is noise."""
    jobs = FakeJobs(respx_mock)
    refused = Schedules(
        [CronJob.model_validate({**_PROPOSE, "schedule": "sonntags früh"})],
        service.hermes,
        service.turns,
        service.metrics,
        retry_seconds=0.01,
    )

    await refused.keep_reconciling()

    assert jobs.jobs == {}
    assert sample(service.metrics, "agent_trigger_reconciles_total", outcome="refused") == 1.0
    assert sample(service.metrics, "agent_trigger_reconciles_total", outcome="failed") == 0.0


_PROPOSE = {
    "name": "lares:propose-faults",
    "schedule": "0 3 * * 0",
    "skills": ["lares-propose"],
    "prompt": "Answer in English. Stay within 80 tool calls and 20 minutes.",
    "deliver": "local",
}


def _declared(tmp_path: Path, text: str = USE_CASES) -> list[CronJob]:
    use_cases_file = tmp_path / "use-cases.yaml"
    use_cases_file.write_text(text, encoding="utf-8")
    return declared_jobs(load_use_case_file(use_cases_file))


def test_the_job_set_is_one_job_per_enabled_schedule_use_case_and_script(tmp_path: Path) -> None:
    """In file order: the schedules with their skill, prompt and budget, then the scripts."""
    assert _declared(tmp_path) == [
        CronJob(
            name="lares:propose-faults",
            schedule="0 3 * * 0",
            skills=("lares-propose",),
            prompt="Answer in English. Stay within 80 tool calls and 20 minutes.",
            deliver="local",
        ),
        CronJob(
            name="lares:restore-probe",
            schedule="0 8 * * 1",
            skills=("lares-restore-probe",),
            prompt="Answer in English. Stay within 30 tool calls and 10 minutes.",
            deliver="local",
        ),
        CronJob(
            name="lares:skill-digest",
            schedule="0 18 * * 0",
            script="skill-digest.py",
            no_agent=True,
            deliver="discord",
        ),
    ]


def test_a_dormant_schedule_use_case_has_no_job(tmp_path: Path) -> None:
    probe = USE_CASES.index("  - name: restore-probe")
    head, tail = USE_CASES[:probe], USE_CASES[probe:]
    text = head + tail.replace("    enabled: true\n", "    dormant: Waits for its target.\n", 1)

    assert "lares:restore-probe" not in [job.name for job in _declared(tmp_path, text)]
