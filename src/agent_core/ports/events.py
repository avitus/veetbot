"""Append-only event repository port."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.events import EventEnvelope, NewEvent


class EventRepository(Protocol):
    async def append(self, event: NewEvent) -> EventEnvelope: ...

    async def list_after(
        self, session_id: UUID, sequence: int, principal: Principal
    ) -> list[EventEnvelope]: ...
