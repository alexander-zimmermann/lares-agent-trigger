"""The read-back: where the pull requests the ledger holds as open stand on GitHub now.

A pull request a run opened starts `open` in its row. Once a day the trigger
asks GitHub about every one still held as open and writes `merged` or
`closed` into its position of `output_state`, so the merged share of a use
case is a count over the ledger, never a guess. A pull request GitHub does not
answer for stays open and is asked about again the next day; the counter says
how often that happened.
"""

from __future__ import annotations

import asyncio
import logging
import re

from .github import GitHubApp, GitHubError
from .ledger import Ledger, OutputState
from .metrics import Metrics

logger = logging.getLogger(__name__)

# What a pull request delivery records as its ref.
_PULL_URL = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)$")


class ReadBack:
    """The ledger's open pull requests, looked up as the write App."""

    def __init__(
        self, ledger: Ledger, github: GitHubApp, metrics: Metrics, *, interval_seconds: float
    ) -> None:
        self._ledger = ledger
        self._github = github
        self._metrics = metrics
        self._interval_seconds = interval_seconds

    async def keep_reading(self) -> None:
        """Read back now, then once every interval; a round that breaks waits for the next."""
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("the read-back of the open pull requests broke")
            await asyncio.sleep(self._interval_seconds)

    async def run_once(self) -> None:
        """Ask GitHub about every output held as open and write what changed."""
        for output in await self._ledger.open_outputs():
            try:
                state = await self._state_of(output.ref)
            except (GitHubError, ValueError) as exc:
                logger.error(
                    "%s run %d: could not read back %s: %s",
                    output.use_case,
                    output.run_id,
                    output.ref,
                    exc,
                )
                self._metrics.read_backs.labels(use_case=output.use_case, state="failed").inc()
                continue
            self._metrics.read_backs.labels(use_case=output.use_case, state=state).inc()
            if state != "open":
                logger.info(
                    "%s run %d: %s is %s", output.use_case, output.run_id, output.ref, state
                )
                await self._ledger.set_output_state(output.run_id, output.position, state)

    async def _state_of(self, ref: str) -> OutputState:
        found = _PULL_URL.match(ref)
        if found is None:
            raise ValueError("the ref is no pull request URL")
        owner, repository, number = found.groups()
        response = await self._github.call(
            "GET", f"/repos/{owner}/{repository}/pulls/{number}", expect={200}
        )
        pull = response.json()
        if pull.get("merged"):
            return "merged"
        return "closed" if pull.get("state") == "closed" else "open"
