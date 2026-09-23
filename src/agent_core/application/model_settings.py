"""The owner's model settings over HTTP (ADR-0118)."""

from __future__ import annotations

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, ModelSettingsChoiceError
from agent_core.domain.events import ProcessEvent
from agent_core.domain.model_settings import (
    ModelChoice,
    ModelSettings,
    ModelSettingsCatalog,
    ModelSettingsView,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import UnitOfWorkFactory


class PublicModelSettingsService:
    """Read and save the owner's chat and memory model choices.

    A save must name offered choices only and the current version. Saving the
    values already stored returns them unchanged, so a retried save is safe.
    """

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        catalog: ModelSettingsCatalog,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._catalog = catalog

    async def get(self, principal: Principal) -> ModelSettingsView:
        require_scope(principal, "settings.read")
        async with self._uow_factory() as uow:
            stored = await uow.model_settings.current(principal)
        return ModelSettingsView.build(self._catalog, stored)

    async def update(
        self,
        principal: Principal,
        *,
        expected_version: int,
        chat: ModelChoice,
        memory: ModelChoice,
    ) -> ModelSettingsView:
        require_scope(principal, "settings.write")
        if not self._catalog.offers_chat(chat):
            raise ModelSettingsChoiceError("the chat model and effort are not offered")
        if not self._catalog.offers_memory(memory):
            raise ModelSettingsChoiceError("the memory model and effort are not offered")
        async with self._uow_factory() as uow:
            stored = await uow.model_settings.current(principal)
            if stored is not None and (stored.chat, stored.memory) == (chat, memory):
                return ModelSettingsView.build(self._catalog, stored)
            head = 0 if stored is None else stored.version
            if expected_version != head:
                raise ConflictError(
                    f"model settings expected version {expected_version} but head is {head}"
                )
            saved = await uow.model_settings.append_version(
                ModelSettings(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    version=head + 1,
                    chat=chat,
                    memory=memory,
                    created_at=self._clock.now(),
                ),
                expected_version=head,
            )
            await uow.process_events.append(
                ProcessEvent(
                    id=self._ids.new_id(),
                    event_type="settings.models.updated",
                    actor_type="principal",
                    actor_id=principal.principal_id,
                    payload={
                        "tenant_id": principal.tenant_id,
                        "principal_id": principal.principal_id,
                        "version": saved.version,
                        "chat_model_policy": chat.model_policy,
                        "chat_reasoning_effort": _effort(chat),
                        "memory_model_policy": memory.model_policy,
                        "memory_reasoning_effort": _effort(memory),
                    },
                    derivation_key=(
                        f"settings.models.updated:{principal.tenant_id}:"
                        f"{principal.principal_id}:{saved.version}"
                    ),
                    created_at=self._clock.now(),
                )
            )
        return ModelSettingsView.build(self._catalog, saved)


def _effort(choice: ModelChoice) -> str | None:
    return None if choice.reasoning_effort is None else choice.reasoning_effort.value
