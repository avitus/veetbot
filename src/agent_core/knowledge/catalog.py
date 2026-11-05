"""The knowledge catalog source the context builder reads (ADR-0299)."""

from __future__ import annotations

from agent_core.domain.agents import Principal
from agent_core.domain.knowledge import KnowledgeCatalog, KnowledgeCatalogQuery
from agent_core.domain.memory import Sensitivity
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory


class KnowledgeCatalogReader:
    """List the documents a principal may read now, through the store's own rule."""

    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def catalog(
        self,
        principal: Principal,
        *,
        current_scope: str | None,
        ceiling: Sensitivity,
        max_documents: int,
    ) -> KnowledgeCatalog | None:
        query = KnowledgeCatalogQuery(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            current_scope=current_scope,
            as_of=self._clock.now(),
            sensitivity_ceiling=ceiling,
            max_documents=max_documents,
        )
        async with self._uow_factory() as uow:
            catalog = await uow.knowledge.catalog(query)
        if not catalog.lines and not catalog.omitted:
            return None
        return catalog
