"""Owner-isolated People storage with immutable historical revisions."""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleQuery, PeopleRecord


class PeopleStore(Protocol):
    def lock(self, principal: Principal) -> AbstractAsyncContextManager[None]: ...

    async def get(
        self,
        principal: Principal,
        record_id: UUID,
        *,
        ceiling: Sensitivity,
        known_at: datetime | None = None,
        at_revision: int | None = None,
    ) -> PeopleRecord | None: ...

    async def source_suppressed(self, principal: Principal, source_id: UUID) -> bool: ...

    async def is_erased(self, principal: Principal, record_id: UUID) -> bool: ...

    async def watermark(self, principal: Principal) -> int: ...

    async def query(self, query: PeopleQuery) -> list[PeopleRecord]: ...

    async def put(self, record: PeopleRecord, *, expected_revision: int) -> PeopleRecord: ...

    async def fence_for_erasure(self, principal: Principal, record_ids: Sequence[UUID]) -> int:
        """Hide roots and their historical dependents without removing revision payloads."""
        ...

    async def purge_erased(self, principal: Principal, *, limit: int = 256) -> bool:
        """Remove at most limit fenced revision payloads; return whether more remain."""
        ...

    async def erase(
        self,
        principal: Principal,
        record_ids: Sequence[UUID],
        *,
        preserve_independent: bool = False,
    ) -> int:
        """Erase records and everything that depends on them.

        By default every historical revision's links count as dependence. With
        ``preserve_independent`` only current heads do, and survivors lose the
        stale revisions that still named an erased record (ADR-0118 repair).
        """
        ...

    async def erase_email_source(
        self, principal: Principal, account_id: str, thread_id: str, message_ids: frozenset[str]
    ) -> int: ...

    async def erase_session(self, principal: Principal, session_id: UUID) -> int: ...

    async def erase_principal(self, principal: Principal) -> int: ...
