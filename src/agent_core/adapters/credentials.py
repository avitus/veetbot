"""Process-local credential broker adapter used by trusted worker integrations."""

from __future__ import annotations

from collections.abc import Mapping

from agent_core.domain.credentials import CredentialRef, SecretValue


class MappingCredentialResolver:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    async def resolve(self, reference: CredentialRef) -> SecretValue:
        try:
            return SecretValue(self._values[reference.name])
        except KeyError as exc:
            raise PermissionError("credential reference is unavailable") from exc
