"""The managed cron job set: what the use-case file declares the harness's cron to run.

One job per enabled schedule use case and one per enabled script; at startup
the trigger brings the harness's job list in line with it through the Jobs API.

A job holds only what the Jobs API takes, and is one of two kinds. An agent
job names the skills a model runs with and its prompt; the API reads no tool
list and no model per job, so every such job sees the cron surface of the
harness configuration and runs on its default model, and the loader of the
use-case file refuses a schedule use case that would need either. A script job
names the file the harness runs without a model (`no_agent`), its stdout the
output the harness delivers itself.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .use_cases import (
    MANAGED_JOB_PREFIX,
    ScheduleTrigger,
    Script,
    UseCase,
    UseCaseFile,
    language_name,
)


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
        """The job in the fields of its kind: what a create sends."""
        return self.model_dump(mode="json", exclude_defaults=True)


def declared_jobs(declared: UseCaseFile) -> list[CronJob]:
    """One job per enabled schedule use case, then one per enabled script, in file order."""
    return [
        *(_agent_job(use_case) for use_case in declared.use_cases.values() if use_case.is_schedule),
        *(_script_job(script) for script in declared.scripts.values() if script.is_enabled),
    ]


def job_name(name: str) -> str:
    """The name the managed job of a schedule use case or a script carries in the harness."""
    return f"{MANAGED_JOB_PREFIX}{name}"


def _agent_job(use_case: UseCase) -> CronJob:
    trigger = use_case.trigger
    assert isinstance(trigger, ScheduleTrigger)
    # The loader refuses a schedule use case that names no skill.
    assert use_case.skill is not None
    budget = use_case.budget
    return CronJob(
        name=job_name(use_case.name),
        schedule=trigger.cron,
        skills=(use_case.skill,),
        prompt=(
            f"Answer in {language_name(use_case.language)}. "
            f"Stay within {budget.tool_calls} tool calls and {budget.minutes} minutes."
        ),
        # The trigger delivers; the harness keeps the output to itself.
        deliver="local",
    )


def _script_job(script: Script) -> CronJob:
    """A script's job: the harness runs the file without a model and delivers its output."""
    return CronJob(
        name=job_name(script.name),
        schedule=script.trigger.cron,
        script=script.script,
        no_agent=True,
        deliver=script.deliver,
    )
