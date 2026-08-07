"""Single-principal identity adapter for the local Milestone 1 deployment."""

from __future__ import annotations

from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.runs import Run


class StaticPrincipalResolver:
    def __init__(self, principal: Principal) -> None:
        self._principal = principal

    async def for_run(self, run: Run) -> Principal:
        if run.tenant_id != self._principal.tenant_id:
            raise NotFoundError("principal not found for run")
        return self._principal.model_copy(update={"scopes": set(run.principal_scopes)}, deep=True)
