"""Governed deterministic first implementation of memory formation."""

from __future__ import annotations

import hashlib
import re
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError, ToolValidationError
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.memory import (
    BeliefRejection,
    BeliefType,
    ConsolidationResult,
    ConsolidationRun,
    MemoryAuthority,
    MemoryEdit,
    MemoryRecord,
    MemoryStatus,
    Polarity,
    Portability,
    RejectionKind,
    Sensitivity,
)
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import TrustLevel
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

FORMATION_POLICY_VERSION = "formation@1"
_SECRET = re.compile(
    r"(?:api[_-]?key|secret|password|token|authorization|credential|bearer)\s*[:=]\s*\S+",
    re.I,
)
_INJECTION = re.compile(
    r"(?:ignore\s+(?:all\s+)?previous|system\s+prompt|developer\s+message|"
    r"<\s*/?\s*(?:system|memory|untrusted)|override\s+(?:policy|instructions))",
    re.I,
)
_TRANSIENT = re.compile(r"\b(?:right now|this turn|temporary|today only)\b", re.I)


class DeterministicSalience:
    def eligible(self, statement: str, *, explicit: bool) -> bool:
        value = statement.strip()
        if not value or _SECRET.search(value) is not None or _INJECTION.search(value) is not None:
            return False
        if not explicit and _TRANSIENT.search(value) is not None:
            return False
        return explicit or len(value.split()) >= 3


class DeterministicConflictResolver:
    @staticmethod
    def _normalized(value: str) -> str:
        return " ".join(value.casefold().split())

    def relationship(
        self, existing: MemoryRecord, statement: str, source_event_ids: list[int]
    ) -> str:
        if set(source_event_ids).issubset(existing.source_event_ids):
            return "same_source"
        if self._normalized(existing.statement) == self._normalized(statement):
            return "duplicate"
        return "contradiction"


def portability_ceiling(belief_type: BeliefType) -> Portability:
    if belief_type in {
        BeliefType.PREFERENCE,
        BeliefType.USER_MODEL_ATTR,
        BeliefType.PROCEDURE_POINTER,
    }:
        return Portability.PORTABLE
    return Portability.CONTEXTUAL


def _portability_allowed(proposed: Portability, ceiling: Portability) -> bool:
    order = {Portability.LOCAL: 0, Portability.CONTEXTUAL: 1, Portability.PORTABLE: 2}
    return order[proposed] <= order[ceiling]


class GovernedMemoryService:
    """Formation service and management surface over the structured store."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        principal: Principal,
        *,
        salience: DeterministicSalience | None = None,
        resolver: DeterministicConflictResolver | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._principal = principal
        self._salience = salience or DeterministicSalience()
        self._resolver = resolver or DeterministicConflictResolver()

    async def remember(
        self,
        *,
        session_id: UUID,
        run_id: UUID | None,
        statement: str,
        subject: str,
        scope: str,
        belief_type: BeliefType = BeliefType.FACT,
        portability: Portability | None = None,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
        source_event_ids: list[int] | None = None,
        origin_trust: TrustLevel = TrustLevel.USER,
        explicit: bool = True,
        authority: MemoryAuthority = MemoryAuthority.USER,
        trigger: str = "explicit",
    ) -> MemoryRecord:
        if origin_trust is not TrustLevel.USER:
            raise ToolValidationError("external content cannot directly write persistent memory")
        clean_statement = " ".join(statement.split())
        clean_subject = " ".join(subject.split())
        if not clean_subject or not self._salience.eligible(clean_statement, explicit=explicit):
            raise ToolValidationError("memory candidate failed eligibility and safety gates")
        effective_portability = portability or portability_ceiling(belief_type)
        if not _portability_allowed(effective_portability, portability_ceiling(belief_type)):
            raise ToolValidationError("memory portability exceeds the belief type ceiling")
        async with self._uow_factory() as uow:
            sources = source_event_ids or await self._latest_user_source(uow, session_id)
            await self._validate_sources(uow, session_id, sources)
            rejections = await uow.memories.outstanding_rejections(
                self._principal.tenant_id, self._principal.principal_id
            )
            statement_hash = hashlib.sha256(clean_statement.casefold().encode()).hexdigest()
            if any(rejection.statement_sha256 == statement_hash for rejection in rejections):
                raise ConflictError("a user deletion or correction blocks this memory")
            formation_run = ConsolidationRun(
                id=self._ids.new_id(),
                tenant_id=self._principal.tenant_id,
                principal_id=self._principal.principal_id,
                trigger=trigger,
                scope=scope,
                session_id=session_id,
                watermark_before=min(sources) - 1,
                watermark_after=max(sources),
                model="deterministic-formation-v1",
                policy_version=FORMATION_POLICY_VERSION,
                candidates_proposed=1,
                committed=0,
                reinforced=0,
                superseded=0,
                rejected=0,
                started_at=self._clock.now(),
            )
            related = await uow.memories.related(
                self._principal.tenant_id,
                self._principal.principal_id,
                clean_subject,
                belief_type,
            )
            for current in sorted(related, key=lambda item: item.store_position, reverse=True):
                relation = self._resolver.relationship(current, clean_statement, sources)
                if relation == "same_source":
                    return current
                if relation == "duplicate":
                    position = await uow.memories.next_position()
                    origin_scopes = list(dict.fromkeys([*current.origin_scopes, scope]))
                    promoted = (
                        current.portability is not Portability.LOCAL and len(origin_scopes) >= 2
                    )
                    reinforced = current.model_copy(
                        update={
                            "scope": "user" if promoted else current.scope,
                            "origin_scopes": origin_scopes,
                            "source_event_ids": sorted(
                                set(current.source_event_ids).union(sources)
                            ),
                            "corroboration_count": current.corroboration_count + 1,
                            "confidence": min(1.0, current.confidence + 0.1),
                            "status": MemoryStatus.ACTIVE,
                            "last_reinforced_at": self._clock.now(),
                            "store_position": position,
                            "updated_at": self._clock.now(),
                        },
                        deep=True,
                    )
                    stored = await uow.memories.reinforce(reinforced)
                    await self._append_event(
                        uow,
                        session_id,
                        run_id,
                        "memory.promoted" if promoted else "memory.reinforced",
                        stored,
                    )
                    await uow.memories.record_consolidation(
                        formation_run.model_copy(
                            update={
                                "reinforced": 1,
                                "finished_at": self._clock.now(),
                            }
                        )
                    )
                    return stored
                if relation == "contradiction":
                    replacement = await self._new_record(
                        uow,
                        formation_run.id,
                        clean_statement,
                        clean_subject,
                        scope,
                        belief_type,
                        effective_portability,
                        sensitivity,
                        session_id,
                        sources,
                        explicit,
                        authority,
                    )
                    position = await uow.memories.next_position()
                    superseded = current.model_copy(
                        update={
                            "status": MemoryStatus.SUPERSEDED,
                            "valid_to": self._clock.now(),
                            "superseded_by": replacement.id,
                            "store_position": position,
                            "updated_at": self._clock.now(),
                        },
                        deep=True,
                    )
                    _old, stored = await uow.memories.supersede(superseded, replacement)
                    await self._append_event(uow, session_id, run_id, "memory.superseded", stored)
                    await uow.memories.record_consolidation(
                        formation_run.model_copy(
                            update={
                                "committed": 1,
                                "superseded": 1,
                                "finished_at": self._clock.now(),
                            }
                        )
                    )
                    return stored
            record = await self._new_record(
                uow,
                formation_run.id,
                clean_statement,
                clean_subject,
                scope,
                belief_type,
                effective_portability,
                sensitivity,
                session_id,
                sources,
                explicit,
                authority,
            )
            stored = await uow.memories.upsert_belief(record)
            await self._append_event(uow, session_id, run_id, "memory.formed", stored)
            await uow.memories.record_consolidation(
                formation_run.model_copy(update={"committed": 1, "finished_at": self._clock.now()})
            )
            return stored

    async def run(
        self,
        *,
        trigger: str,
        scope: str,
        session_id: UUID | None,
        since_watermark: int | None = None,
    ) -> ConsolidationResult:
        if session_id is None:
            run = ConsolidationRun(
                id=self._ids.new_id(),
                tenant_id=self._principal.tenant_id,
                principal_id=self._principal.principal_id,
                trigger=trigger,
                scope=scope,
                watermark_before=0,
                watermark_after=0,
                model="deterministic-formation-v1",
                policy_version=FORMATION_POLICY_VERSION,
                candidates_proposed=0,
                committed=0,
                reinforced=0,
                superseded=0,
                rejected=0,
                started_at=self._clock.now(),
                finished_at=self._clock.now(),
            )
            async with self._uow_factory() as uow:
                await uow.memories.record_consolidation(run)
            return ConsolidationResult(run=run)
        async with self._uow_factory() as uow:
            watermark = (
                await uow.memories.consolidation_watermark(session_id, self._principal)
                if since_watermark is None
                else since_watermark
            )
            events = await uow.events.list_after(session_id, watermark, self._principal)
        candidates = [candidate for event in events if (candidate := _candidate(event))]
        beliefs: list[MemoryRecord] = []
        rejected = 0
        for event, subject, statement, belief_type in candidates:
            try:
                belief = await self.remember(
                    session_id=session_id,
                    run_id=event.run_id,
                    statement=statement,
                    subject=subject,
                    scope=scope,
                    belief_type=belief_type,
                    source_event_ids=[event.sequence],
                    origin_trust=TrustLevel.USER,
                    explicit=False,
                    authority=MemoryAuthority.USER,
                    trigger=trigger,
                )
            except (ConflictError, ToolValidationError):
                rejected += 1
            else:
                beliefs.append(belief)
        after = max((event.sequence for event in events), default=watermark)
        async with self._uow_factory() as uow:
            await uow.memories.set_consolidation_watermark(session_id, self._principal, after)
            audit = ConsolidationRun(
                id=self._ids.new_id(),
                tenant_id=self._principal.tenant_id,
                principal_id=self._principal.principal_id,
                trigger=trigger,
                scope=scope,
                session_id=session_id,
                watermark_before=watermark,
                watermark_after=after,
                model="deterministic-formation-v1",
                policy_version=FORMATION_POLICY_VERSION,
                candidates_proposed=len(candidates),
                committed=len(beliefs),
                reinforced=0,
                superseded=0,
                rejected=rejected,
                started_at=self._clock.now(),
                finished_at=self._clock.now(),
            )
            await uow.memories.record_consolidation(audit)
        return ConsolidationResult(run=audit, beliefs=beliefs)

    async def list_memories(
        self, *, include_inactive: bool = False, limit: int = 200
    ) -> list[MemoryRecord]:
        async with self._uow_factory() as uow:
            return await uow.memories.list_memories(
                self._principal, include_inactive=include_inactive, limit=limit
            )

    async def edit(self, belief_id: UUID, edit: MemoryEdit) -> MemoryRecord:
        async with self._uow_factory() as uow:
            current = await uow.memories.get(belief_id, self._principal)
            position = await uow.memories.next_position()
            updated = current.model_copy(
                update={
                    "statement": edit.statement,
                    "sensitivity": edit.sensitivity or current.sensitivity,
                    "portability": edit.portability or current.portability,
                    "scope": edit.scope or current.scope,
                    "authority": MemoryAuthority.USER,
                    "source_event_ids": current.source_event_ids,
                    "store_position": position,
                    "updated_at": self._clock.now(),
                    "last_reinforced_at": self._clock.now(),
                },
                deep=True,
            )
            stored = await uow.memories.edit(belief_id, self._principal, edit, updated)
            await self._append_event(uow, _source_session(current), None, "memory.edited", stored)
            return stored

    async def delete(self, belief_id: UUID, *, trace_id: UUID | None = None) -> None:
        async with self._uow_factory() as uow:
            current = await uow.memories.get(belief_id, self._principal)
            tombstone = BeliefRejection(
                id=self._ids.new_id(),
                tenant_id=current.tenant_id,
                principal_id=current.principal_id,
                belief_id=current.id,
                kind=RejectionKind.DELETED,
                subject=current.subject,
                statement=None,
                statement_sha256=hashlib.sha256(current.statement.casefold().encode()).hexdigest(),
                belief_type=current.belief_type,
                scope=current.scope,
                trace_id=trace_id,
                created_at=self._clock.now(),
            )
            await uow.memories.delete(belief_id, self._principal, tombstone)
            await self._append_event(uow, _source_session(current), None, "memory.deleted", current)

    async def reject(
        self,
        belief_id: UUID,
        kind: RejectionKind,
        *,
        replacement_statement: str | None = None,
        trace_id: UUID | None = None,
    ) -> MemoryRecord:
        if kind is RejectionKind.DELETED:
            await self.delete(belief_id, trace_id=trace_id)
            raise NotFoundError("memory was deleted")
        async with self._uow_factory() as uow:
            current = await uow.memories.get(belief_id, self._principal)
            position = await uow.memories.next_position()
            update: dict[str, object] = {
                "flagged_for_review": True,
                "confidence": max(0, current.confidence - 0.2),
                "store_position": position,
                "updated_at": self._clock.now(),
            }
            if kind is RejectionKind.UNTRUE:
                update.update({"status": MemoryStatus.RETIRED, "valid_to": self._clock.now()})
            elif kind is RejectionKind.NOT_HERE:
                update["portability"] = Portability.LOCAL
            elif kind is RejectionKind.CHANGED:
                if replacement_statement is None:
                    raise ToolValidationError("changed rejection requires replacement text")
                update.update({"status": MemoryStatus.SUPERSEDED, "valid_to": self._clock.now()})
            updated = current.model_copy(update=update, deep=True)
            rejection = BeliefRejection(
                id=self._ids.new_id(),
                tenant_id=current.tenant_id,
                principal_id=current.principal_id,
                belief_id=current.id,
                kind=kind,
                subject=current.subject,
                statement=current.statement,
                statement_sha256=hashlib.sha256(current.statement.casefold().encode()).hexdigest(),
                belief_type=current.belief_type,
                scope=current.scope,
                trace_id=trace_id,
                created_at=self._clock.now(),
            )
            stored = await uow.memories.reject(rejection, updated)
            await self._append_event(uow, _source_session(current), None, "memory.rejected", stored)
        if kind is RejectionKind.CHANGED and replacement_statement is not None:
            return await self.remember(
                session_id=_source_session(current),
                run_id=None,
                statement=replacement_statement,
                subject=current.subject,
                scope=current.scope,
                belief_type=current.belief_type,
                portability=current.portability,
                sensitivity=current.sensitivity,
                source_event_ids=current.source_event_ids,
                origin_trust=TrustLevel.USER,
                explicit=True,
            )
        return stored

    async def expire(self) -> list[MemoryRecord]:
        async with self._uow_factory() as uow:
            return await uow.memories.expire(self._principal)

    async def _new_record(
        self,
        uow: RepositoryUnitOfWork,
        formation_run_id: UUID,
        statement: str,
        subject: str,
        scope: str,
        belief_type: BeliefType,
        portability: Portability,
        sensitivity: Sensitivity,
        source_session_id: UUID,
        source_event_ids: list[int],
        explicit: bool,
        authority: MemoryAuthority,
    ) -> MemoryRecord:
        now = self._clock.now()
        return MemoryRecord(
            id=self._ids.new_id(),
            tenant_id=self._principal.tenant_id,
            principal_id=self._principal.principal_id,
            scope=scope,
            subject=subject,
            statement=statement,
            source_session_id=source_session_id,
            source_event_ids=sorted(set(source_event_ids)),
            confidence=0.9 if explicit else 0.55,
            sensitivity=sensitivity,
            valid_from=now,
            status=MemoryStatus.ACTIVE if explicit else MemoryStatus.PROVISIONAL,
            belief_type=belief_type,
            polarity=Polarity.ASSERT,
            portability=portability,
            origin_scopes=[scope],
            last_reinforced_at=now,
            formation_run_id=formation_run_id,
            consolidation_policy_version=FORMATION_POLICY_VERSION,
            authority=authority,
            store_position=await uow.memories.next_position(),
            created_at=now,
            updated_at=now,
        )

    async def _latest_user_source(self, uow: RepositoryUnitOfWork, session_id: UUID) -> list[int]:
        events = await uow.events.list_after(session_id, 0, self._principal)
        user_events = [
            event.sequence for event in events if event.event_type == "user.message.created"
        ]
        if not user_events:
            raise ToolValidationError("memory write requires a user-source event")
        return [user_events[-1]]

    async def _validate_sources(
        self, uow: RepositoryUnitOfWork, session_id: UUID, sources: list[int]
    ) -> None:
        if not sources:
            raise ToolValidationError("memory provenance cannot be empty")
        existing = await uow.events.existing_sequences(session_id, set(sources), self._principal)
        if existing != set(sources):
            raise ToolValidationError("memory provenance names missing source events")

    async def _append_event(
        self,
        uow: RepositoryUnitOfWork,
        session_id: UUID,
        run_id: UUID | None,
        event_type: str,
        belief: MemoryRecord,
    ) -> None:
        await uow.events.append(
            NewEvent(
                session_id=session_id,
                run_id=run_id,
                event_type=event_type,
                actor_type="principal" if belief.authority is MemoryAuthority.USER else "memory",
                actor_id=self._principal.principal_id,
                payload={"belief": belief.model_dump(mode="json")},
            )
        )


def _candidate(event: EventEnvelope) -> tuple[EventEnvelope, str, str, BeliefType] | None:
    if event.event_type != "user.message.created":
        return None
    content = event.payload.get("content")
    texts: list[str] = []
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        for raw in content:
            if isinstance(raw, dict):
                try:
                    part = TextPart.model_validate(raw)
                except ValueError:
                    continue
                texts.append(part.text)
    text = " ".join(texts).strip()
    lowered = text.casefold()
    if lowered.startswith("remember that "):
        return event, "user", text[len("remember that ") :].strip(), BeliefType.FACT
    match = re.match(r"(?:i|we)\s+(?:really\s+)?prefer\s+(.+)", text, re.I)
    if match is not None:
        return event, "user", f"Prefers {match.group(1).strip()}", BeliefType.PREFERENCE
    return None


def _source_session(record: MemoryRecord) -> UUID:
    return record.source_session_id
