"""A single job's owner and approved host map bound to the live secret store."""

from __future__ import annotations

from agent_core.application.integration_secrets import IntegrationSecrets
from agent_core.domain.agents import Principal
from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import NotFoundError
from agent_core.domain.jobs import JobAccess, JobBrokerError


class StoredJobCredentials:
    def __init__(
        self, service: IntegrationSecrets, principal: Principal, access: JobAccess
    ) -> None:
        self._service, self._principal = service, principal
        self._hosts = dict(access.hosts)

    async def resolve(self, reference: str, host: str) -> tuple[str, SecretValue]:
        if self._hosts.get(host) != reference:
            raise JobBrokerError("job.credential_refused")
        try:
            return await self._service.resolve(self._principal, reference, host)
        except (NotFoundError, ValueError, PermissionError):
            raise JobBrokerError("job.credential_unavailable") from None
