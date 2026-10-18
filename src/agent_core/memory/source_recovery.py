"""Bounded source expansion from selected permitted originals."""

import asyncio
from collections.abc import Callable, Sequence
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.memory import MemoryRecord, RecallProfile, RecallQuery, Sensitivity
from agent_core.domain.memory_sources import RecalledSource, recovery_source_id
from agent_core.domain.people_sources import source_id
from agent_core.domain.reconsolidation import content_signature
from agent_core.domain.sessions import project_scope
from agent_core.ports.persistence import RepositoryUnitOfWork


class SourceRecoveryUnavailableError(Exception):
    """The caller must roll back this UOW before retrying ordinary recall."""


class BoundedSourceRecovery:
    def __init__(self, principal: Principal, *, admitted: Callable[[], bool]) -> None:
        self._principal = principal.model_copy(deep=True)
        self._admitted = admitted

    async def recover(
        self,
        uow: RepositoryUnitOfWork,
        query: RecallQuery,
        records: Sequence[MemoryRecord],
        *,
        surface_id: str,
    ) -> dict[UUID, tuple[RecalledSource, ...]]:
        try:
            async with asyncio.timeout(0.25):
                return await self._recover(uow, query, records, surface_id=surface_id)
        except Exception:
            # Do not keep using a transaction whose database read was cancelled.
            raise SourceRecoveryUnavailableError from None

    async def _recover(
        self,
        uow: RepositoryUnitOfWork,
        query: RecallQuery,
        records: Sequence[MemoryRecord],
        *,
        surface_id: str,
    ) -> dict[UUID, tuple[RecalledSource, ...]]:
        if (
            (query.tenant_id, query.principal_id)
            != (self._principal.tenant_id, self._principal.principal_id)
            or surface_id != "private"
            or query.sensitivity_ceiling is not Sensitivity.RESTRICTED
            or query.profile is RecallProfile.CORE
            or query.as_of is not None
            or query.known_at is not None
            or query.people_scope is not None
            or not records
            or not self._admitted()
        ):
            return {}
        # Rejoin current originals through the common hard predicates. A caller's
        # old record, broad portability or guessed source cannot confer authority.
        selected = {
            record.id: record
            for record in records[:3]
            if query.include_ids is None or record.id in query.include_ids
        }
        if not selected:
            return {}
        current = await uow.memories.query(
            query.model_copy(
                update={
                    "text": None,
                    "include_ids": tuple(selected),
                    "expand_ids": (),
                    "structured_belief_types": [],
                    "include_owned_resources": False,
                }
            )
        )
        visible = {record.id: record for record in current}
        recovered: dict[UUID, tuple[RecalledSource, ...]] = {}
        seen: set[tuple[UUID, int]] = set()
        total_bytes = 0
        count = 0
        for key, original in selected.items():
            record = visible.get(key)
            if record is None or content_signature(record) != content_signature(original):
                continue
            session = await uow.sessions.get(record.source_session_id, self._principal)
            if project_scope(session.metadata) != query.current_scope:
                continue  # A portable fact does not make its whole source portable.
            quotes = []
            for sequence in sorted(set(record.source_event_ids), reverse=True)[:2]:
                source_key = (record.source_session_id, sequence)
                if source_key in seen or count >= 3:
                    continue
                seen.add(source_key)
                people_key = source_id(self._principal, *source_key)
                if await uow.people.source_suppressed(self._principal, people_key):
                    continue
                excerpt = await uow.events.memory_source(
                    *source_key,
                    self._principal,
                    surface_id=surface_id,
                    ceiling=query.sensitivity_ceiling,
                )
                if excerpt is None or excerpt.created_at > record.last_evidence_at:
                    continue
                size = len(excerpt.text.encode())
                if total_bytes + size > 8192:
                    continue
                quotes.append(
                    RecalledSource(
                        **excerpt.model_dump(),
                        copy_id=recovery_source_id(self._principal, *source_key),
                        people_source_id=people_key,
                    )
                )
                total_bytes += size
                count += 1
            if quotes:
                recovered[key] = tuple(quotes)
        return recovered if self._admitted() else {}
