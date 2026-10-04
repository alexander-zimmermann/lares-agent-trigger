"""The schedule use cases in the harness: the reconcile of their cron jobs, and run-now.

The harness runs the schedules itself; its own cron ledger, incidents and
overdue gauges are the signal that one did not run. What the trigger owns is
the job set: once per pod, it lists the harness's jobs and brings the managed
ones — named `lares:<use case>` — in line with the rendered `cron-jobs.yaml`.

- a declared job the harness lacks is created;
- a declared job whose schedule, prompt, skills or delivery differ is updated
  in place, its id kept — an unchanged one is not touched, because rewriting a
  schedule drops an occurrence the harness has not run yet;
- a managed job nothing declares any more is deleted, and so is the second of
  two jobs holding one managed name;
- every other job is left alone, and so is whether a managed job is paused:
  pausing one is a person's lever.

The pod rolls when the rendering changes, so startup is the only moment the
set can change. A gateway that does not answer then is asked again until it
does: both pods roll on the same commit, and a reconcile lost to that would
wait for the next one. A gateway that answers with a refusal is not asked
again: the declaration it refused changes only with a new commit, which rolls
the pod anyway.

A job a person paused is not run now either: the gateway's run-now resumes a
paused job for good. What a person asked a run now to look at goes into that
one run's prompt, never into the job's.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

import httpx

from .cron_jobs import CronJob, job_name
from .hermes import HarnessJob, HermesClient, HermesError
from .metrics import Metrics
from .turn_runs import TurnRuns
from .use_cases import MANAGED_JOB_PREFIX, UseCase

logger = logging.getLogger(__name__)


class MissingJobError(LookupError):
    """The harness holds no job for a schedule use case: the reconcile has not run yet."""


class PausedJobError(RuntimeError):
    """The job of a schedule use case is paused in the harness, and running it would resume it."""


class FocusRefusedError(ValueError):
    """The harness refused what a person asked the run to look at; the message says why."""


class Schedules:
    """The managed cron jobs, wired to the harness and to the hook path that records their runs."""

    def __init__(
        self,
        jobs: Sequence[CronJob],
        hermes: HermesClient,
        turns: TurnRuns,
        metrics: Metrics,
        *,
        retry_seconds: float,
    ) -> None:
        self._jobs = jobs
        self._hermes = hermes
        self._turns = turns
        self._metrics = metrics
        self._retry_seconds = retry_seconds

    async def keep_reconciling(self) -> None:
        """Reconcile until the gateway lets one pass through."""
        while True:
            try:
                await self._reconcile()
            except (HermesError, httpx.HTTPError, OSError) as exc:
                if isinstance(exc, HermesError) and _refusal(exc):
                    self._metrics.reconciles.labels(outcome="refused").inc()
                    logger.error(
                        "the harness refused the cron job set, fix the declaration: %s", exc
                    )
                    return
                self._metrics.reconciles.labels(outcome="failed").inc()
                logger.warning(
                    "could not reconcile the cron jobs, trying again in %.0f s: %s",
                    self._retry_seconds,
                    exc,
                )
                await asyncio.sleep(self._retry_seconds)
                continue
            self._metrics.reconciles.labels(outcome="done").inc()
            return

    async def run_now(self, use_case: UseCase, focus: str | None = None) -> str:
        """Have the harness run this use case's job now; the job's id.

        `focus`, what the person asked for, goes into that run's prompt. Its
        next cron turn is then recorded as started by a person.
        """
        name = job_name(use_case)
        job = next((job for job in await self._hermes.list_jobs() if job.name == name), None)
        if job is None:
            raise MissingJobError(f"the harness holds no job {name} yet")
        if not job.enabled:
            raise PausedJobError(f"{name} is paused in the harness; resume it there to run it")
        prompt = f"The owner asked for this run: {focus}" if focus is not None else None
        try:
            await self._hermes.run_job(job.id, prompt)
        except HermesError as exc:
            if prompt is not None and _refusal(exc):
                raise FocusRefusedError(f"the harness refused the focus: {exc}") from exc
            raise
        self._turns.expect_requested(job.id)
        logger.info("asked the harness to run %s (%s) now", name, job.id)
        return job.id

    async def _reconcile(self) -> None:
        managed: dict[str, list[HarnessJob]] = {}
        for job in await self._hermes.list_jobs():
            if job.name.startswith(MANAGED_JOB_PREFIX):
                managed.setdefault(job.name, []).append(job)

        for declared in self._jobs:
            held = managed.pop(declared.name, [])
            if not held:
                job_id = await self._hermes.create_job(declared)
                self._done("created", declared.name, job_id)
                continue
            kept, *extra = held
            changes = _changes(kept, declared)
            if changes:
                await self._hermes.update_job(kept.id, changes)
                self._done("updated", declared.name, kept.id, ", ".join(changes))
            for job in extra:
                await self._hermes.delete_job(job.id)
                self._done("deleted", job.name, job.id, "a second job of that name")
        for jobs in managed.values():
            for job in jobs:
                await self._hermes.delete_job(job.id)
                self._done("deleted", job.name, job.id, "no longer declared")

    def _done(self, action: str, name: str, job_id: str, why: str = "") -> None:
        self._metrics.cron_jobs.labels(action=action).inc()
        logger.info("%s cron job %s (%s)%s", action, name, job_id, f": {why}" if why else "")


def _refusal(exc: HermesError) -> bool:
    """True when the gateway answered and refused, rather than failing to answer."""
    code = exc.status_code
    return code is not None and 400 <= code < 500 and code != 429


def _changes(job: HarnessJob, declared: CronJob) -> dict[str, Any]:
    """The fields of a job that differ from its declaration, as the update sends them."""
    wanted: dict[str, Any] = {
        "schedule": declared.schedule,
        "prompt": declared.prompt,
        "skills": list(declared.skills),
        "deliver": declared.deliver,
    }
    held: dict[str, Any] = {
        "schedule": job.schedule,
        "prompt": job.prompt,
        "skills": list(job.skills),
        "deliver": job.deliver,
    }
    return {field: value for field, value in wanted.items() if held[field] != value}
