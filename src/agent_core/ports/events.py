"""Append-only event repository port."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.persistence import ProjectionCursor, WorkerLease


class EventRepository(Protocol):
    async def append(
        self, event: NewEvent, *, lease: WorkerLease | None = None
    ) -> EventEnvelope: ...

    async def list_after(
        self, session_id: UUID, sequence: int, principal: Principal
    ) -> list[EventEnvelope]: ...


class Upcaster(Protocol):
    event_type: str
    from_version: int
    to_version: int

    def upcast(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class Projection(Protocol):
    name: str
    builder_version: str

    async def apply(
        self, events: Sequence[EventEnvelope], cursor: ProjectionCursor
    ) -> ProjectionCursor: ...

    async def rebuild(self, scope: str) -> ProjectionCursor: ...
