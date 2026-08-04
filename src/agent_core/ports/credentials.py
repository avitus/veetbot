"""Worker-side credential broker capability."""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.credentials import CredentialRef, SecretValue


class CredentialResolver(Protocol):
    async def resolve(self, reference: CredentialRef) -> SecretValue: ...


class UnavailableCredentialResolver:
    async def resolve(self, reference: CredentialRef) -> SecretValue:
        del reference
        raise PermissionError("credentials are unavailable to sandbox tools")
