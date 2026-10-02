"""The cron job set end to end: the rendered file in, the harness's job list brought in line."""

from __future__ import annotations

from pathlib import Path

import pytest
import respx

from lares_agent_trigger.cron_jobs import CronJob, load_cron_jobs
from lares_agent_trigger.schedules import Schedules
from lares_agent_trigger.use_cases import load_use_cases

from .conftest import CRON_JOBS, USE_CASES, Service
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
    # In file order: the changed job updated in place, the missing one created,
    # then the managed job nothing declares any more deleted.
    assert jobs.changes == [("PATCH", stale), ("POST", probe["id"]), ("DELETE", retired)]
    assert jobs.jobs[foreign] == before
    (propose,) = jobs.named("lares:propose-faults")
    assert propose["id"] == stale
    assert propose["schedule"]["expr"] == "0 3 * * 0"
    assert probe["schedule"]["expr"] == "0 8 * * 1"
    assert probe["skills"] == ["lares-restore-probe"]
    assert probe["prompt"] == "Answer in English. Stay within 30 tool calls and 10 minutes."
    assert probe["deliver"] == "local"

    metrics = service.metrics
    for action in ("created", "updated", "deleted"):
        assert sample(metrics, "agent_trigger_cron_jobs_total", action=action) == 1.0
    assert sample(metrics, "agent_trigger_reconciles_total", outcome="done") == 1.0


async def test_a_job_as_declared_is_left_as_it_is(
    service: Service, respx_mock: respx.MockRouter
) -> None:
    """Rewriting an unchanged schedule would drop an occurrence the harness has not run yet."""
    jobs = FakeJobs(respx_mock)

    await service.schedules.keep_reconciling()
    created = list(jobs.changes)
    await service.schedules.keep_reconciling()

    assert jobs.changes == created
    assert [method for method, _ in created] == ["POST", "POST"]


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
    """The reconcile runs once per pod; a gateway rolling at the same moment must not lose it."""
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


def _load(tmp_path: Path, cron_jobs: str) -> None:
    use_cases_file = tmp_path / "use-cases.yaml"
    use_cases_file.write_text(USE_CASES, encoding="utf-8")
    cron_jobs_file = tmp_path / "cron-jobs.yaml"
    cron_jobs_file.write_text(cron_jobs, encoding="utf-8")
    load_cron_jobs(cron_jobs_file, load_use_cases(use_cases_file))


def test_the_rendered_job_set_matches_the_declaration(tmp_path: Path) -> None:
    _load(tmp_path, CRON_JOBS)


def test_a_job_the_declaration_does_not_enable_is_refused(tmp_path: Path) -> None:
    """A rendering out of step with the use cases would delete or start what nobody declared."""
    stale = CRON_JOBS.replace("lares:restore-probe", "lares:summarise-week")

    with pytest.raises(ValueError, match="job lares:summarise-week runs no enabled schedule"):
        _load(tmp_path, stale)


def test_a_schedule_use_case_without_its_job_is_refused(tmp_path: Path) -> None:
    missing = CRON_JOBS[: CRON_JOBS.index("  - name: lares:restore-probe")]

    with pytest.raises(ValueError, match="no job for the schedule use case restore-probe"):
        _load(tmp_path, missing)


def test_a_job_declared_twice_is_refused(tmp_path: Path) -> None:
    twice = CRON_JOBS.replace("lares:restore-probe", "lares:propose-faults")

    with pytest.raises(ValueError, match="job lares:propose-faults is declared twice"):
        _load(tmp_path, twice)


def test_a_job_field_the_api_cannot_take_is_refused(tmp_path: Path) -> None:
    with_model = CRON_JOBS.replace("    deliver: local\n", "    deliver: local\n    model: x\n", 1)

    with pytest.raises(ValueError, match="model"):
        _load(tmp_path, with_model)
