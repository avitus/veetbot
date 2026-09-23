"""Owner model-settings store port (ADR-0119)."""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.agents import Principal
from agent_core.domain.model_settings import ModelSettings


class ModelSettingsStore(Protocol):
    """Versioned, principal-scoped model settings.

    `append_version` admits `settings` only when `expected_version` names the
    current head (0 before the first save) and `settings.version` is exactly
    the next version; anything else is a `ConflictError`. Another principal's
    settings read as absent.
    """

    async def current(self, principal: Principal) -> ModelSettings | None: ...

    async def append_version(
        self, settings: ModelSettings, *, expected_version: int
    ) -> ModelSettings: ...
