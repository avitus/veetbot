"""Form memory with the model the owner chose (ADR-0118)."""

from __future__ import annotations

from collections.abc import Mapping

from agent_core.domain.agents import Principal
from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import MemoryCandidate, MemoryExtractionResult
from agent_core.domain.model_settings import ModelChoice, ModelSettingsCatalog
from agent_core.ports.memory import MemoryCandidateExtractor
from agent_core.ports.persistence import UnitOfWorkFactory


class OwnerSelectedCandidateExtractor:
    """Route each extraction to the evaluated tuple the owner chose.

    The owner's stored memory choice is read at every extraction, so a saved
    change applies to the next formation. A choice that is no longer offered
    falls back to the composition's default. A consolidation formed on an
    alternative records its policy and effort after the delegate's name.
    """

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        catalog: ModelSettingsCatalog,
        extractors: Mapping[ModelChoice, MemoryCandidateExtractor],
    ) -> None:
        self._uow_factory = uow_factory
        self._catalog = catalog
        self._extractors = dict(extractors)
        self._default = self._extractors[catalog.memory_default]
        self._last = self._default
        self.name = self._default.name

    @property
    def last_audit(self) -> object | None:
        return getattr(self._last, "last_audit", None)

    async def extract(
        self,
        events: list[EventEnvelope],
        *,
        principal: Principal,
        scope: str,
    ) -> list[MemoryCandidate] | MemoryExtractionResult:
        async with self._uow_factory() as uow:
            stored = await uow.model_settings.current(principal)
        choice = self._catalog.effective_memory(stored)
        extractor = self._extractors.get(choice, self._default)
        self._last = extractor
        if extractor is self._default:
            self.name = extractor.name
        else:
            effort = "default" if choice.reasoning_effort is None else choice.reasoning_effort.value
            self.name = f"{extractor.name}@{choice.model_policy}:{effort}"
        return await extractor.extract(events, principal=principal, scope=scope)
