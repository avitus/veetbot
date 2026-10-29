"""The one MVP credential: a read-only, repository-selected GitHub App."""

from collections.abc import Callable
from pathlib import Path

import httpx

from agent_core.adapters.cockpit.github import GitHubAppTokens, load_app_private_key
from agent_core.domain.credentials import CredentialRef, SecretValue
from agent_core.domain.jobs import JobBrokerError
from agent_core.ports.cockpit import WidgetRepositoryUnavailableError
from agent_core.ports.determinism import Clock


class GitHubReaderCredentials:
    """Read the systemd credential only when the broker actually needs a token."""

    def __init__(
        self,
        *,
        token_factory: Callable[..., GitHubAppTokens],
        app_id: int | None,
        installation_id: int | None,
        key_file: Path | None,
        clock: Clock,
    ) -> None:
        self._token_factory = token_factory
        self._app_id = app_id
        self._installation_id = installation_id
        self._key_file = key_file
        self._clock = clock
        self._client: httpx.AsyncClient | None = None
        self._tokens: GitHubAppTokens | None = None

    async def resolve(self, reference: CredentialRef) -> SecretValue:
        if reference.name != "github-reader":
            raise JobBrokerError("job.credential_refused")
        if self._app_id is None or self._installation_id is None or self._key_file is None:
            raise JobBrokerError("job.credential_unconfigured")
        if self._tokens is None:
            try:
                key = load_app_private_key(self._key_file)
            except ValueError:
                raise JobBrokerError("job.credential_unavailable") from None
            self._client = httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
            self._tokens = self._token_factory(
                self._client,
                app_id=self._app_id,
                installation_id=self._installation_id,
                private_key_pem=key,
                clock=self._clock,
                read_only=True,
            )
        try:
            return SecretValue(await self._tokens.token())
        except WidgetRepositoryUnavailableError:
            raise JobBrokerError("job.credential_unavailable", retryable=True) from None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
