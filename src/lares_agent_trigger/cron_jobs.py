"""The managed cron job set: one schema for the rendering and for the reconcile.

The generator renders one job per enabled schedule use case and one per enabled
script into `cron-jobs.yaml`; lares mounts the file, and the trigger reads it
back at startup and brings the harness's job list in line with it through the
Jobs API.

A job holds only what the Jobs API takes, and is one of two kinds. An agent
job names the skills a model runs with and its prompt; the API reads no tool
list and no model per job, so every such job sees the cron surface of the
harness configuration and runs on its default model, and the loader of the
use-case file refuses a schedule use case that would need either. A script job
names the file the harness runs without a model (`no_agent`), its stdout the
output the harness delivers itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .use_cases import MANAGED_JOB_PREFIX, UseCaseFile, scheduled_script, scheduled_use_case


class CronJob(BaseModel):
    """One managed job, in the fields the Jobs API creates and updates it with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # `lares:<use case or script>`: the harness mints the id, so the name is what is matched on.
    name: str = Field(min_length=len(MANAGED_JOB_PREFIX) + 1, pattern=f"^{MANAGED_JOB_PREFIX}")
    schedule: str = Field(min_length=1)
    # An agent job: the skills its model runs with, and what it is told.
    skills: tuple[str, ...] = ()
    prompt: str = ""
    # A script job: the file the harness runs instead of a model.
    script: str | None = None
    no_agent: bool = False
    deliver: str = Field(min_length=1)

    @model_validator(mode="after")
    def _agent_or_script(self) -> CronJob:
        if self.script is None:
            if not self.skills or not self.prompt:
                raise ValueError(f"job {self.name}: an agent job names its skills and its prompt")
            if self.no_agent:
                raise ValueError(f"job {self.name}: `no_agent` needs a script")
        elif not self.no_agent or self.skills or self.prompt:
            raise ValueError(
                f"job {self.name}: a script job is `no_agent: true` with neither skills nor prompt"
            )
        return self

    def fields(self) -> dict[str, Any]:
        """The job in the fields of its kind: what the file holds and a create sends."""
        return self.model_dump(mode="json", exclude_defaults=True)


class _File(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    jobs: list[CronJob]


def load_cron_jobs(path: Path, declared: UseCaseFile) -> list[CronJob]:
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
        if (
            scheduled_use_case(declared.use_cases, name) is None
            and scheduled_script(declared.scripts, name) is None
        ):
            raise ValueError(
                f"{path}: job {name} runs no enabled schedule use case or script; render it again"
            )
    expected = {
        **{
            job_name(use_case.name): f"the schedule use case {use_case.name}"
            for use_case in declared.use_cases.values()
            if use_case.is_schedule
        },
        **{
            job_name(script.name): f"the script {script.name}"
            for script in declared.scripts.values()
            if script.is_enabled
        },
    }
    for name, owner in expected.items():
        if name not in names:
            raise ValueError(f"{path}: no job for {owner}; render it again")
    return jobs


def job_name(name: str) -> str:
    """The name the managed job of a schedule use case or a script carries in the harness."""
    return f"{MANAGED_JOB_PREFIX}{name}"


def dump_cron_jobs(jobs: Sequence[CronJob]) -> dict[str, list[dict[str, object]]]:
    """The job set as the file holds it."""
    return {"jobs": [job.fields() for job in jobs]}
