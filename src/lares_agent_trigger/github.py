"""The write App on GitHub: installation tokens from its private key, and calls made with them.

GitHub hands an App a token for one installation in exchange for a JWT the App
signs with its private key. The JWT is good for ten minutes at most, the token
for an hour; the token is kept and used until it has five minutes left. The
key stays in this pod: the harness reads GitHub as a different App that may
only read, and never holds a token that writes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .config import Settings
from .failures import describe

# GitHub refuses a JWT that lives longer than ten minutes; a minute is taken
# off the issue time for a clock running ahead of GitHub's.
_JWT_BACKDATE_SECONDS = 60
_JWT_LIFETIME_SECONDS = 540
# A token with less left than this is replaced before the next call.
_RENEW_BEFORE_SECONDS = 300
# What a refusal quotes of GitHub's answer; the row keeps the reason, not a page.
_ANSWER_LIMIT = 500


class GitHubError(RuntimeError):
    """GitHub did not answer, or answered what the call did not expect."""


@dataclass(frozen=True)
class _Token:
    value: str
    expires_at: float


class GitHubApp:
    """One installation of the write App, and an HTTP client against GitHub's API."""

    def __init__(
        self,
        *,
        app_id: str,
        installation_id: int,
        private_key: str,
        owner: str,
        api_url: str,
        timeout_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        try:
            load_pem_private_key(private_key.encode("utf-8"), password=None)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"GITHUB_APP_PRIVATE_KEY is no PEM private key: {exc}") from exc
        self.owner = owner
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key = private_key
        self._clock = clock
        self._token: _Token | None = None
        self._client = httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(
        self,
        method: str,
        path: str,
        *,
        expect: Collection[int],
        json: Any = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """One call as the App; a `GitHubError` unless GitHub answers one of ``expect``."""
        token = await self._installation_token()
        try:
            response = await self._client.request(
                method,
                path,
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub could not be reached: {describe(exc)}") from exc
        if response.status_code not in expect:
            raise GitHubError(
                f"GitHub answered {response.status_code} to {method} {path}:"
                f" {response.text[:_ANSWER_LIMIT]}"
            )
        return response

    async def _installation_token(self) -> str:
        now = self._clock()
        if self._token is not None and self._token.expires_at - now > _RENEW_BEFORE_SECONDS:
            return self._token.value
        issued = int(now) - _JWT_BACKDATE_SECONDS
        signed = jwt.encode(
            {"iat": issued, "exp": issued + _JWT_LIFETIME_SECONDS, "iss": self._app_id},
            self._private_key,
            algorithm="RS256",
        )
        path = f"/app/installations/{self._installation_id}/access_tokens"
        try:
            response = await self._client.post(path, headers={"Authorization": f"Bearer {signed}"})
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub could not be reached for a token: {describe(exc)}") from exc
        if response.status_code != httpx.codes.CREATED:
            raise GitHubError(
                f"GitHub refused the App a token with {response.status_code}:"
                f" {response.text[:_ANSWER_LIMIT]}"
            )
        try:
            body = response.json()
            expires_at = datetime.fromisoformat(body["expires_at"]).timestamp()
            self._token = _Token(value=str(body["token"]), expires_at=expires_at)
        except (ValueError, KeyError, TypeError) as exc:
            raise GitHubError(f"GitHub answered a token request with no token: {exc}") from exc
        return self._token.value


def github_app(settings: Settings) -> GitHubApp | None:
    """The write App the settings name; None for none, a ``ValueError`` for half of one."""
    named = {
        "GITHUB_APP_ID": settings.github_app_id,
        "GITHUB_APP_INSTALLATION_ID": settings.github_app_installation_id,
        "GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_FILE": settings.github_app_private_key,
    }
    missing = [name for name, value in named.items() if not value]
    if len(missing) == len(named):
        return None
    if missing:
        raise ValueError(f"the GitHub App needs {', '.join(missing)} as well")
    assert settings.github_app_installation_id is not None
    return GitHubApp(
        app_id=settings.github_app_id,
        installation_id=settings.github_app_installation_id,
        private_key=settings.github_app_private_key,
        owner=settings.github_owner,
        api_url=settings.github_api_url,
        timeout_seconds=settings.github_request_timeout_seconds,
    )
