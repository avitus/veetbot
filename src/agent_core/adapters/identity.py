"""Single-principal identity adapter for the local Milestone 1 deployment."""

from __future__ import annotations

import hashlib
import json

from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.runs import Run
from agent_core.domain.schedules import AuthoritySnapshot


class StaticPrincipalResolver:
    def __init__(self, principal: Principal) -> None:
        self._principal = principal

    async def for_run(self, run: Run) -> Principal:
        if run.tenant_id != self._principal.tenant_id:
            raise NotFoundError("principal not found for run")
        return self._principal.model_copy(update={"scopes": set(run.principal_scopes)}, deep=True)


class ConfiguredSchedulePrincipalDirectory:
    """Resolve current authority from the operator-managed principal record."""

    def __init__(
        self,
        principal: Principal,
        *,
        enabled: bool = True,
        source: str = "configured",
    ) -> None:
        self._principal = principal
        self._enabled = enabled
        self._source = source

    async def current(self, tenant_id: str, principal_id: str) -> AuthoritySnapshot | None:
        if tenant_id != self._principal.tenant_id or principal_id != self._principal.principal_id:
            return None
        encoded = json.dumps(
            {
                "tenant_id": self._principal.tenant_id,
                "principal_id": self._principal.principal_id,
                "roles": sorted(self._principal.roles),
                "scopes": sorted(self._principal.scopes),
                "enabled": self._enabled,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return AuthoritySnapshot(
            principal=self._principal.model_copy(deep=True),
            authority_version=f"{self._source}:{hashlib.sha256(encoded).hexdigest()}",
            enabled=self._enabled,
        )


class StaticSchedulePrincipalDirectory(ConfiguredSchedulePrincipalDirectory):
    """Development/test alias whose name makes its non-production role explicit."""

    def __init__(self, principal: Principal, *, enabled: bool = True) -> None:
        super().__init__(principal, enabled=enabled, source="static")
