"""The managed cron job set: one schema for the rendering and for the reconcile.

The generator renders one job per enabled schedule use case into
`cron-jobs.yaml`; lares mounts the file, and the trigger reads it back at
startup and brings the harness's job list in line with it through the Jobs API.

A job holds only what the Jobs API takes. The API reads no tool list and no
model per job, so every managed job sees the cron surface of the harness
configuration and runs on its default model; the loader of the use-case file
refuses a schedule use case that would need either.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .use_cases import MANAGED_JOB_PREFIX, UseCase, scheduled_use_case


class CronJob(BaseModel):
    """One managed job, in the fields the Jobs API creates and updates it with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # `lares:<use case>`: the gateway mints the id, so the name is what is matched on.
    name: str = Field(min_length=len(MANAGED_JOB_PREFIX) + 1, pattern=f"^{MANAGED_JOB_PREFIX}")
    schedule: str = Field(min_length=1)
    skills: tuple[str, ...] = Field(min_length=1)
    prompt: str = Field(min_length=1)
    deliver: str = Field(min_length=1)


class _File(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    jobs: list[CronJob]


def load_cron_jobs(path: Path, use_cases: Mapping[str, UseCase]) -> list[CronJob]:
    """Read the rendered job set and check it against the declaration it was rendered from.

    A file that does not parse, names a job twice, or disagrees with the
    use-case file is a ``ValueError`` naming the file: the two are rendered in
    one commit, and a pod that mounts them out of step must not delete a job
    the declaration still holds.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"{path}: cannot be read ({exc})") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: is not valid YAML ({exc})") from exc
    try:
        jobs = _File.model_validate(raw).jobs
    except ValidationError as exc:
        raise ValueError(f"{path}: {exc}") from exc

    names = [job.name for job in jobs]
    for name in names:
        if names.count(name) > 1:
            raise ValueError(f"{path}: job {name} is declared twice")
        if scheduled_use_case(use_cases, name) is None:
            raise ValueError(
                f"{path}: job {name} runs no enabled schedule use case; render it again"
            )
    for use_case in use_cases.values():
        if use_case.is_schedule and job_name(use_case) not in names:
            raise ValueError(
                f"{path}: no job for the schedule use case {use_case.name}; render it again"
            )
    return jobs


def job_name(use_case: UseCase) -> str:
    """The name the managed job of a schedule use case carries in the harness."""
    return f"{MANAGED_JOB_PREFIX}{use_case.name}"


def dump_cron_jobs(jobs: Sequence[CronJob]) -> dict[str, list[dict[str, object]]]:
    """The job set as the file holds it."""
    return {"jobs": [job.model_dump(mode="json") for job in jobs]}
