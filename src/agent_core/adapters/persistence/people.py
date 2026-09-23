"""Revisioned People storage; PostgreSQL owns transaction commit and rollback."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Select,
    Text,
    and_,
    any_,
    bindparam,
    case,
    cast,
    delete,
    exists,
    func,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from agent_core.adapters.persistence.sqlalchemy_models import (
    MemoryRevisionRow,
    MemoryRow,
    PeopleHeadRow,
    PeopleLinkRow,
    PeopleRevisionRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.memory import LIVE_MEMORY_STATUSES, SENSITIVITY_ORDER, Sensitivity
from agent_core.domain.people import (
    OWNER_RELATIONSHIP_GROUPS,
    PEOPLE_RECORD,
    OrganizationReference,
    PeopleErasure,
    PeopleInteraction,
    PeopleQuery,
    PeopleRecord,
    PeopleSource,
    Person,
    PersonIdentifier,
    RelationshipAssertion,
    dependencies,
    event_time,
    normalize_identifier,
    referenced_assignments,
    referenced_organizations,
    referenced_people,
)
from agent_core.ports.determinism import Clock
from agent_core.ports.memory import MemoryStore


def _principal(record: PeopleRecord) -> Principal:
    return Principal(
        tenant_id=record.tenant_id, principal_id=record.principal_id, roles=set(), scopes=set()
    )


def _name(record: PeopleRecord) -> str:
    if isinstance(record, (Person, OrganizationReference)):
        return normalize_identifier("name", "people", record.display_name)
    if isinstance(record, PersonIdentifier):
        return normalize_identifier(record.identifier_kind, record.namespace, record.value)
    return ""


def _validate(record: PeopleRecord, current: PeopleRecord | None, expected: int) -> PeopleRecord:
    record = PEOPLE_RECORD.validate_python(record.model_dump())
    if expected < 0 or record.revision != expected + 1:
        raise ConflictError("people revision must follow expected revision")
    if (0 if current is None else current.revision) != expected:
        raise ConflictError("people revision changed")
    if current and (
        record.kind != current.kind
        or record.created_at != current.created_at
        or record.updated_at < current.updated_at
    ):
        raise ConflictError("people immutable identity or revision time changed")
    return record


def _copy_rebased(
    record: PeopleRecord, replacements: dict[UUID, UUID], now: datetime
) -> PeopleRecord | None:
    # Only complete metadata projections are interchangeable across verified
    # copies. Composite claims and exact source spans still follow full erasure.
    if not isinstance(record, (Person, PersonIdentifier, PeopleInteraction)):
        return None
    if not set(record.support_ids) & replacements.keys():
        return None
    return record.model_copy(
        update={
            "support_ids": list(
                dict.fromkeys(replacements.get(key, key) for key in record.support_ids)
            ),
            "revision": record.revision + 1,
            "updated_at": max(now, record.updated_at + timedelta(microseconds=1)),
        }
    )


class InMemoryPeopleStore:
    def __init__(self, clock: Clock, memories: MemoryStore | None = None) -> None:
        self._clock = clock
        self._memories = memories
        self._records: dict[tuple[str, str, UUID], list[PeopleRecord]] = {}
        self._erased: set[tuple[str, str, UUID]] = set()
        self._positions: dict[tuple[str, str], int] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    @asynccontextmanager
    async def lock(self, principal: Principal) -> AsyncIterator[None]:
        async with self._locks.setdefault(
            (principal.tenant_id, principal.principal_id), asyncio.Lock()
        ):
            yield

    async def get(
        self,
        principal: Principal,
        record_id: UUID,
        *,
        ceiling: Sensitivity,
        known_at: datetime | None = None,
        at_revision: int | None = None,
    ) -> PeopleRecord | None:
        key = (principal.tenant_id, principal.principal_id, record_id)
        if key in self._erased:
            return None
        values = self._records.get(key, [])
        if values and (
            SENSITIVITY_ORDER[values[-1].sensitivity] > SENSITIVITY_ORDER[ceiling]
            or (isinstance(values[-1], PeopleSource) and values[-1].excluded)
        ):
            return None
        current = next(
            (
                r
                for r in reversed(values)
                if (known_at is None or r.updated_at <= known_at)
                and (at_revision is None or r.revision == at_revision)
            ),
            None,
        )
        if current is None or SENSITIVITY_ORDER[current.sensitivity] > SENSITIVITY_ORDER[ceiling]:
            return None
        if isinstance(current, PeopleSource) and current.excluded:
            return None
        pending = list(dependencies(current))
        checked: set[UUID] = set()
        while pending:
            ref = pending.pop()
            if ref in checked:
                continue
            checked.add(ref)
            target_key = (principal.tenant_id, principal.principal_id, ref)
            revisions = self._records.get(target_key, [])
            if not revisions or target_key in self._erased:
                return None
            target = revisions[-1]
            if SENSITIVITY_ORDER[target.sensitivity] > SENSITIVITY_ORDER[ceiling]:
                return None
            if isinstance(target, PeopleSource) and target.excluded:
                return None
            pending.extend(dependencies(target))
        return current.model_copy(deep=True)

    async def source_suppressed(self, principal: Principal, source_id: UUID) -> bool:
        return await self.is_erased(principal, source_id) or any(
            key[:2] == (principal.tenant_id, principal.principal_id)
            and isinstance(rows[-1], PeopleErasure)
            and rows[-1].state != "preview"
            and source_id in rows[-1].blocked_source_ids
            for key, rows in self._records.items()
        )

    async def is_erased(self, principal: Principal, record_id: UUID) -> bool:
        return (principal.tenant_id, principal.principal_id, record_id) in self._erased

    async def watermark(self, principal: Principal) -> int:
        return self._positions.get((principal.tenant_id, principal.principal_id), 0)

    async def query(self, query: PeopleQuery) -> list[PeopleRecord]:
        query = PeopleQuery.model_validate(query.model_dump())
        owner = Principal(
            tenant_id=query.tenant_id, principal_id=query.principal_id, roles=set(), scopes=set()
        )
        relationship_matches: set[UUID] = set()
        if query.relationship is not None and self._memories is not None:
            instant = query.as_of or self._clock.now()
            for key in self._records:
                if key[:2] != (owner.tenant_id, owner.principal_id):
                    continue
                edge = await self.get(
                    owner, key[2], ceiling=query.sensitivity_ceiling, known_at=query.known_at
                )
                if (
                    not isinstance(edge, RelationshipAssertion)
                    or edge.unresolved
                    or (
                        edge.predicate not in OWNER_RELATIONSHIP_GROUPS[query.relationship]
                        or not any(
                            endpoint.kind == "owner" for endpoint in (edge.subject, edge.object)
                        )
                        or (edge.valid_from is not None and edge.valid_from > instant)
                        or (edge.valid_to is not None and edge.valid_to <= instant)
                    )
                ):
                    continue
                try:
                    belief = (
                        await self._memories.get_at(edge.belief_id, owner, known_at=query.known_at)
                        if query.known_at is not None
                        else await self._memories.get(edge.belief_id, owner)
                    )
                except NotFoundError:
                    continue
                if (
                    SENSITIVITY_ORDER[belief.sensitivity]
                    > SENSITIVITY_ORDER[query.sensitivity_ceiling]
                    or (instant >= self._clock.now() and belief.status not in LIVE_MEMORY_STATUSES)
                    or belief.valid_from > instant
                    or (belief.valid_to is not None and belief.valid_to <= instant)
                    or (belief.expires_at is not None and belief.expires_at <= instant)
                ):
                    continue
                relationship_matches.update(referenced_people(edge))
        results: list[PeopleRecord] = []
        alias_matches: set[UUID] = set()
        if query.search_aliases and query.text:
            for key in self._records:
                if key[:2] != (owner.tenant_id, owner.principal_id):
                    continue
                alias = await self.get(
                    owner, key[2], ceiling=query.sensitivity_ceiling, known_at=query.known_at
                )
                if (
                    isinstance(alias, PersonIdentifier)
                    and alias.person_id is not None
                    and query.text.casefold() in alias.value.casefold()
                    and (
                        query.as_of is None
                        or (
                            alias.valid_from <= query.as_of
                            and (alias.valid_to is None or alias.valid_to > query.as_of)
                        )
                    )
                ):
                    alias_matches.add(alias.person_id)
        review_excluded: set[UUID] = set()
        if query.needs_review:
            for key in self._records:
                if key[:2] != (owner.tenant_id, owner.principal_id):
                    continue
                alias = await self.get(
                    owner, key[2], ceiling=query.sensitivity_ceiling, known_at=query.known_at
                )
                if (
                    isinstance(alias, PersonIdentifier)
                    and alias.person_id is not None
                    and alias.verification in {"owner_confirmed", "channel_observed"}
                ):
                    review_excluded.add(alias.person_id)
        distinct_seen: set[tuple[object, ...]] = set()
        recent: dict[UUID, datetime] = {}
        if query.sort == "recent":
            for key in self._records:
                if key[:2] != (owner.tenant_id, owner.principal_id):
                    continue
                interaction = await self.get(
                    owner, key[2], ceiling=query.sensitivity_ceiling, known_at=query.known_at
                )
                if (
                    not isinstance(interaction, PeopleInteraction)
                    or interaction.occurred_at is None
                    or interaction.superseded_by is not None
                ):
                    continue
                if query.as_of and interaction.occurred_at > query.as_of:
                    continue
                for person_id in referenced_people(interaction):
                    recent[person_id] = max(
                        recent.get(person_id, interaction.occurred_at), interaction.occurred_at
                    )
        for tenant, principal, record_id in sorted(self._records, key=lambda key: key[2]):
            if tenant != owner.tenant_id or principal != owner.principal_id:
                continue
            if query.sort == "id" and query.after and record_id <= query.after:
                continue
            record = await self.get(
                owner, record_id, ceiling=query.sensitivity_ceiling, known_at=query.known_at
            )
            if record is None or record.kind not in query.kinds:
                continue
            if (
                not query.include_superseded
                and isinstance(record, PeopleInteraction)
                and record.superseded_by is not None
            ):
                continue
            if query.root_erasures_only and (
                not isinstance(record, PeopleErasure) or record.parent_id is not None
            ):
                continue
            if query.relationship is not None and record.id not in relationship_matches:
                continue
            if query.states is not None and getattr(record, "state", None) not in query.states:
                continue
            if query.identifier_value is not None and (
                _name(record).lower() != query.identifier_value.lower()
            ):
                continue
            if query.assigned != "any":
                if not isinstance(record, PersonIdentifier):
                    if query.assigned == "unattached":
                        continue
                elif (record.person_id is None) != (query.assigned == "unattached"):
                    continue
            if (
                query.valid_at is not None
                and isinstance(record, PersonIdentifier)
                and (
                    record.valid_from > query.valid_at
                    or (record.valid_to is not None and record.valid_to <= query.valid_at)
                )
            ):
                continue
            if query.needs_review and (
                not isinstance(record, Person)
                or record.state != "provisional"
                or record.pinned
                or record.id in review_excluded
            ):
                continue
            if query.pinned is not None and getattr(record, "pinned", None) is not query.pinned:
                continue
            if (
                query.belief_id is not None
                and getattr(record, "belief_id", None) != query.belief_id
            ):
                continue
            if (
                query.belief_ids is not None
                and getattr(record, "belief_id", None) not in query.belief_ids
            ):
                continue
            if query.person_id and query.person_id not in referenced_people(record):
                continue
            if query.source_id and query.source_id not in record.support_ids:
                continue
            if query.session_id and (
                not isinstance(record, PeopleSource) or record.session_id != query.session_id
            ):
                continue
            if (
                (
                    query.account_id is not None
                    and (
                        not isinstance(record, PeopleSource)
                        or record.account_id != query.account_id
                    )
                )
                or (
                    query.thread_id is not None
                    and (
                        not isinstance(record, PeopleSource) or record.thread_id != query.thread_id
                    )
                )
                or (
                    query.message_ids
                    and (
                        not isinstance(record, PeopleSource)
                        or record.message_id not in query.message_ids
                    )
                )
            ):
                continue
            if query.mentioned_in and (
                not _name(record) or _name(record).casefold() not in query.mentioned_in.casefold()
            ):
                continue
            if (
                query.text
                and query.text.casefold() not in _name(record).casefold()
                and record.id not in alias_matches
            ):
                continue
            occurred = (
                record.occurred_at if isinstance(record, PeopleInteraction) else event_time(record)
            )
            if query.channel and (
                not isinstance(record, PeopleInteraction) or record.channel != query.channel
            ):
                continue
            if query.interaction_kind and (
                not isinstance(record, PeopleInteraction)
                or record.interaction_kind != query.interaction_kind
            ):
                continue
            if (query.unknown_time == "only" and occurred is not None) or (
                query.unknown_time == "exclude" and occurred is None
            ):
                continue
            if query.since and (occurred is None or occurred < query.since):
                continue
            if query.until and (occurred is None or occurred >= query.until):
                continue
            if (
                query.as_of
                and isinstance(record, PeopleInteraction)
                and (occurred is None or occurred > query.as_of)
            ):
                continue
            if (
                query.as_of
                and isinstance(record, RelationshipAssertion)
                and (
                    (record.valid_from is not None and record.valid_from > query.as_of)
                    or (record.valid_to is not None and record.valid_to <= query.as_of)
                )
            ):
                continue
            if query.sort == "history" and query.after:
                if query.after_event_at is None:
                    if occurred is not None or record.id <= query.after:
                        continue
                elif occurred is not None and (
                    occurred > query.after_event_at
                    or (occurred == query.after_event_at and record.id <= query.after)
                ):
                    continue
            if query.distinct_assignments:
                # Records arrive in identifier order, so the lowest id of each
                # assignment is kept, matching PostgreSQL's DISTINCT ON.
                assignment: tuple[object, ...] = (
                    (
                        "identifier",
                        record.person_id,
                        record.identifier_kind,
                        record.namespace,
                        record.context,
                        record.verification,
                        _name(record).lower(),
                    )
                    if isinstance(record, PersonIdentifier)
                    else (record.kind, record.id)
                )
                if assignment in distinct_seen:
                    continue
                distinct_seen.add(assignment)
            results.append(record)
        if query.sort == "history":
            results.sort(
                key=lambda r: (
                    r.occurred_at is None if isinstance(r, PeopleInteraction) else False,
                    -(r.occurred_at or datetime.min.replace(tzinfo=UTC)).timestamp()
                    if isinstance(r, PeopleInteraction)
                    else -event_time(r).timestamp(),
                    r.id,
                )
            )
        if query.sort == "recent":
            floor = datetime.min.replace(tzinfo=UTC)
            results.sort(key=lambda row: (-recent.get(row.id, floor).timestamp(), row.id))
            if query.after:
                cursor_key = (-recent.get(query.after, floor).timestamp(), query.after)
                results = [
                    row
                    for row in results
                    if (-recent.get(row.id, floor).timestamp(), row.id) > cursor_key
                ]
        return results[: query.limit + 1]

    async def put(self, record: PeopleRecord, *, expected_revision: int) -> PeopleRecord:
        key = (record.tenant_id, record.principal_id, record.id)
        if key in self._erased:
            raise ConflictError("people record was erased")
        versions = self._records.get(key, [])
        record = _validate(record, versions[-1] if versions else None, expected_revision)
        for ref in record.support_ids:
            source = await self.get(_principal(record), ref, ceiling=Sensitivity.RESTRICTED)
            if not isinstance(source, PeopleSource):
                raise ConflictError("people source is missing or excluded")
        for ref in referenced_people(record):
            target = await self.get(_principal(record), ref, ceiling=Sensitivity.RESTRICTED)
            if not isinstance(target, Person):
                raise ConflictError("people reference is missing")
        for ref in referenced_organizations(record):
            target = await self.get(_principal(record), ref, ceiling=Sensitivity.RESTRICTED)
            if not isinstance(target, OrganizationReference):
                raise ConflictError("organization reference is missing")
        for ref in referenced_assignments(record):
            if await self.get(_principal(record), ref, ceiling=Sensitivity.RESTRICTED) is None:
                raise ConflictError("people assignment reference is missing")
        self._records.setdefault(key, []).append(record.model_copy(deep=True))
        owner_key = (record.tenant_id, record.principal_id)
        self._positions[owner_key] = self._positions.get(owner_key, 0) + 1
        return record.model_copy(deep=True)

    async def fence_for_erasure(self, principal: Principal, record_ids: Sequence[UUID]) -> int:
        return self.erase_locked(principal, record_ids, fence_only=True)

    async def purge_erased(self, principal: Principal, *, limit: int = 256) -> bool:
        if not 1 <= limit <= 256:
            raise ValueError("People erasure batches contain at most 256 revisions")
        remaining = limit
        owner = (principal.tenant_id, principal.principal_id)
        for key in sorted(self._erased):
            if key[:2] != owner or key not in self._records:
                continue
            versions = self._records[key]
            take = min(remaining, len(versions))
            del versions[:take]
            remaining -= take
            if not versions:
                del self._records[key]
            if not remaining:
                break
        return any(key[:2] == owner and key in self._records for key in self._erased)

    async def erase(
        self,
        principal: Principal,
        record_ids: Sequence[UUID],
        *,
        preserve_independent: bool = False,
    ) -> int:
        return self.erase_locked(principal, record_ids, preserve_independent=preserve_independent)

    def erase_locked(
        self,
        principal: Principal,
        record_ids: Sequence[UUID],
        *,
        preserve_independent: bool = False,
        fence_only: bool = False,
    ) -> int:
        doomed = set(record_ids)
        if preserve_independent:
            owned = {
                key[2]: rows[-1]
                for key, rows in self._records.items()
                if key[:2] == (principal.tenant_id, principal.principal_id)
            }
            copies = {
                row.copy_group: row.id
                for row in owned.values()
                if isinstance(row, PeopleSource)
                and row.copy_group
                and not row.excluded
                and row.id not in doomed
                and not row.support_ids
            }
            replacements = {
                row.id: copies[row.copy_group]
                for row in owned.values()
                if isinstance(row, PeopleSource) and row.id in doomed and row.copy_group in copies
            }
            for row in owned.values():
                replacement = _copy_rebased(row, replacements, self._clock.now())
                if replacement is not None:
                    self._records[(principal.tenant_id, principal.principal_id, row.id)].append(
                        replacement
                    )
                    owner_key = (principal.tenant_id, principal.principal_id)
                    self._positions[owner_key] = self._positions.get(owner_key, 0) + 1
        while True:
            dependent = {
                key[2]
                for key, revisions in self._records.items()
                if key[:2] == (principal.tenant_id, principal.principal_id)
                and any(
                    dependencies(row) & doomed
                    for row in (revisions[-1:] if preserve_independent else revisions)
                )
            }
            if dependent <= doomed:
                break
            doomed.update(dependent)
        count = 0
        for record_id in doomed:
            key = (principal.tenant_id, principal.principal_id, record_id)
            if key in self._records:
                count += int(key not in self._erased) if fence_only else 1
                if not fence_only:
                    del self._records[key]
                self._erased.add(key)
        if preserve_independent:
            for key, revisions in list(self._records.items()):
                if key[:2] == (principal.tenant_id, principal.principal_id):
                    self._records[key] = [
                        row for row in revisions if not dependencies(row) & doomed
                    ]
        owner_key = (principal.tenant_id, principal.principal_id)
        self._positions[owner_key] = self._positions.get(owner_key, 0) + count
        return count

    def erase_session_locked(self, principal: Principal, session_id: UUID) -> int:
        ids = [
            key[2]
            for key, revisions in self._records.items()
            if key[:2] == (principal.tenant_id, principal.principal_id)
            and any(
                isinstance(row, PeopleSource) and row.session_id == session_id for row in revisions
            )
        ]
        return self.erase_locked(principal, ids, preserve_independent=True)

    async def erase_email_source(
        self, principal: Principal, account_id: str, thread_id: str, message_ids: frozenset[str]
    ) -> int:
        return self.erase_email_source_locked(principal, account_id, thread_id, message_ids)

    def erase_email_source_locked(
        self, principal: Principal, account_id: str, thread_id: str, message_ids: frozenset[str]
    ) -> int:
        ids = [
            key[2]
            for key, revisions in self._records.items()
            if key[:2] == (principal.tenant_id, principal.principal_id)
            and any(
                isinstance(row, PeopleSource)
                and row.source_kind == "email"
                and row.account_id == account_id
                and row.thread_id == thread_id
                and (not message_ids or row.message_id in message_ids)
                for row in revisions
            )
        ]
        return self.erase_locked(principal, ids, preserve_independent=True)

    async def erase_session(self, principal: Principal, session_id: UUID) -> int:
        return self.erase_session_locked(principal, session_id)

    async def erase_principal(self, principal: Principal) -> int:
        owned = [k for k in self._records if k[:2] == (principal.tenant_id, principal.principal_id)]
        for key in owned:
            del self._records[key]
        self._erased = {
            k for k in self._erased if k[:2] != (principal.tenant_id, principal.principal_id)
        }
        self._positions.pop((principal.tenant_id, principal.principal_id), None)
        return len(owned)


class PostgresPeopleStore:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def _prepare_read(self) -> None:
        # Recursive privacy checks have high estimated costs even when their
        # indexed hidden-head seed is empty. LLVM compilation costs more than
        # these bounded OLTP reads. Limit this setting to the current transaction;
        # it does not change the connection's or server's persistent defaults.
        await self._session.execute(text("SET LOCAL jit = off"))

    @asynccontextmanager
    async def lock(self, principal: Principal) -> AsyncIterator[None]:
        key = f"people:{len(principal.tenant_id)}:{principal.tenant_id}:{principal.principal_id}"
        number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], signed=True)
        await self._session.execute(select(func.pg_advisory_xact_lock(number)))
        yield

    def _query(
        self,
        principal: Principal,
        ceiling: Sensitivity,
        known_at: datetime | None,
        at_revision: int | None = None,
    ) -> Select[tuple[PeopleRevisionRow]]:
        latest = aliased(PeopleRevisionRow)
        maximum = select(func.max(latest.revision)).where(
            latest.tenant_id == PeopleRevisionRow.tenant_id,
            latest.principal_id == PeopleRevisionRow.principal_id,
            latest.entity_id == PeopleRevisionRow.entity_id,
        )
        if known_at is not None:
            maximum = maximum.where(latest.recorded_at <= known_at)
        # Compute the current forbidden dependency closure once, starting from
        # hidden heads and following incoming current links. A per-candidate
        # forward recursive walk prevents PostgreSQL from bounding page work.
        # Historical rows still test their own version's direct links against
        # this closure, so a later added edge does not hide an independent past
        # version, while current privacy always overrides historical visibility.
        target = aliased(PeopleHeadRow)
        following = aliased(PeopleLinkRow)
        hidden = (
            select(PeopleHeadRow.id.label("id"))
            .where(
                PeopleHeadRow.tenant_id == principal.tenant_id,
                PeopleHeadRow.principal_id == principal.principal_id,
                PeopleHeadRow.erased
                | PeopleHeadRow.excluded
                | (PeopleHeadRow.sensitivity > SENSITIVITY_ORDER[ceiling]),
            )
            .cte("people_hidden", recursive=True, nesting=True)
        )
        hidden = hidden.union(
            select(following.entity_id)
            .select_from(hidden)
            .join(
                following,
                (following.target_id == hidden.c.id)
                & (following.tenant_id == principal.tenant_id)
                & (following.principal_id == principal.principal_id),
            )
            .join(
                target,
                (target.id == following.entity_id)
                & (target.tenant_id == following.tenant_id)
                & (target.principal_id == following.principal_id)
                & (target.revision == following.revision),
            )
        )
        forbidden = (
            select(PeopleLinkRow.entity_id, PeopleLinkRow.revision)
            .join(hidden, hidden.c.id == PeopleLinkRow.target_id)
            .where(
                PeopleLinkRow.tenant_id == principal.tenant_id,
                PeopleLinkRow.principal_id == principal.principal_id,
            )
        )
        current_head = (
            select(1)
            .where(
                PeopleHeadRow.tenant_id == principal.tenant_id,
                PeopleHeadRow.principal_id == principal.principal_id,
                PeopleHeadRow.id == PeopleRevisionRow.entity_id,
                ~PeopleHeadRow.erased,
                ~PeopleHeadRow.excluded,
                PeopleHeadRow.sensitivity <= SENSITIVITY_ORDER[ceiling],
            )
            .correlate(PeopleRevisionRow)
        )
        if known_at is None and at_revision is None:
            current_head = current_head.where(PeopleHeadRow.revision == PeopleRevisionRow.revision)
        # Preserve the indexed head probe after the candidate predicates. Without
        # this fence PostgreSQL can reverse the join and compare every owner head
        # with a materialized page of matching belief links (millions of pairs).
        statement = (
            select(PeopleRevisionRow)
            .add_cte(hidden, nest_here=True)
            .where(
                PeopleRevisionRow.tenant_id == principal.tenant_id,
                PeopleRevisionRow.principal_id == principal.principal_id,
                PeopleRevisionRow.sensitivity <= SENSITIVITY_ORDER[ceiling],
                exists(current_head.offset(0)),
                ~tuple_(PeopleRevisionRow.entity_id, PeopleRevisionRow.revision).in_(forbidden),
            )
        )
        if at_revision is not None or known_at is not None:
            statement = statement.where(
                PeopleRevisionRow.revision
                == (at_revision if at_revision is not None else maximum.scalar_subquery())
            )
        return statement

    async def get(
        self,
        principal: Principal,
        record_id: UUID,
        *,
        ceiling: Sensitivity,
        known_at: datetime | None = None,
        at_revision: int | None = None,
    ) -> PeopleRecord | None:
        await self._prepare_read()
        row = (
            await self._session.execute(
                self._query(principal, ceiling, known_at, at_revision).where(
                    PeopleRevisionRow.entity_id == record_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else PEOPLE_RECORD.validate_python(row.payload)

    async def source_suppressed(self, principal: Principal, source_id: UUID) -> bool:
        if await self.is_erased(principal, source_id):
            return True
        return bool(
            await self._session.scalar(
                select(
                    exists(
                        select(1)
                        .select_from(PeopleRevisionRow)
                        .join(
                            PeopleHeadRow,
                            and_(
                                PeopleHeadRow.tenant_id == PeopleRevisionRow.tenant_id,
                                PeopleHeadRow.principal_id == PeopleRevisionRow.principal_id,
                                PeopleHeadRow.id == PeopleRevisionRow.entity_id,
                                PeopleHeadRow.revision == PeopleRevisionRow.revision,
                            ),
                        )
                        .where(
                            PeopleRevisionRow.tenant_id == principal.tenant_id,
                            PeopleRevisionRow.principal_id == principal.principal_id,
                            PeopleRevisionRow.kind == "erasure",
                            PeopleRevisionRow.payload["state"].astext != "preview",
                            PeopleRevisionRow.payload["blocked_source_ids"].contains(
                                [str(source_id)]
                            ),
                        )
                    )
                )
            )
        )

    async def is_erased(self, principal: Principal, record_id: UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(PeopleHeadRow.erased).where(
                    PeopleHeadRow.tenant_id == principal.tenant_id,
                    PeopleHeadRow.principal_id == principal.principal_id,
                    PeopleHeadRow.id == record_id,
                )
            )
        )

    async def watermark(self, principal: Principal) -> int:
        from sqlalchemy import case

        value = await self._session.scalar(
            select(
                func.coalesce(
                    func.sum(PeopleHeadRow.revision + case((PeopleHeadRow.erased, 1), else_=0)), 0
                )
            ).where(
                PeopleHeadRow.tenant_id == principal.tenant_id,
                PeopleHeadRow.principal_id == principal.principal_id,
            )
        )
        return int(value or 0)

    async def query(self, query: PeopleQuery) -> list[PeopleRecord]:
        await self._prepare_read()
        query = PeopleQuery.model_validate(query.model_dump())
        owner = Principal(
            tenant_id=query.tenant_id, principal_id=query.principal_id, roles=set(), scopes=set()
        )
        statement = self._query(owner, query.sensitivity_ceiling, query.known_at).where(
            PeopleRevisionRow.kind.in_(query.kinds)
        )
        if query.relationship is not None:
            instant = query.as_of or self._clock.now()
            edges = (
                self._query(owner, query.sensitivity_ceiling, query.known_at)
                .where(
                    PeopleRevisionRow.kind == "relationship",
                    PeopleRevisionRow.payload["predicate"].astext.in_(
                        OWNER_RELATIONSHIP_GROUPS[query.relationship]
                    ),
                    PeopleRevisionRow.payload["unresolved"].astext == "false",
                    or_(
                        PeopleRevisionRow.payload["subject"]["kind"].astext == "owner",
                        PeopleRevisionRow.payload["object"]["kind"].astext == "owner",
                    ),
                    or_(
                        PeopleRevisionRow.payload["valid_from"].astext.is_(None),
                        cast(
                            PeopleRevisionRow.payload["valid_from"].astext, DateTime(timezone=True)
                        )
                        <= instant,
                    ),
                    or_(
                        PeopleRevisionRow.payload["valid_to"].astext.is_(None),
                        cast(PeopleRevisionRow.payload["valid_to"].astext, DateTime(timezone=True))
                        > instant,
                    ),
                )
                .subquery("visible_owner_relationships")
            )
            related = (
                select(PeopleLinkRow.target_id)
                .join(
                    edges,
                    and_(
                        PeopleLinkRow.tenant_id == edges.c.tenant_id,
                        PeopleLinkRow.principal_id == edges.c.principal_id,
                        PeopleLinkRow.entity_id == edges.c.entity_id,
                        PeopleLinkRow.revision == edges.c.revision,
                    ),
                )
                .join(
                    MemoryRow,
                    and_(
                        cast(MemoryRow.id, Text) == edges.c.payload["belief_id"].astext,
                        MemoryRow.tenant_id == edges.c.tenant_id,
                        MemoryRow.principal_id == edges.c.principal_id,
                    ),
                )
                .where(
                    PeopleLinkRow.role == "person",
                    ~MemoryRow.erasure_pending,
                    MemoryRow.sensitivity.in_(
                        [
                            key.value
                            for key, rank in SENSITIVITY_ORDER.items()
                            if rank <= SENSITIVITY_ORDER[query.sensitivity_ceiling]
                        ]
                    ),
                )
            )
            if query.known_at is not None:
                latest = (
                    select(MemoryRevisionRow)
                    .where(
                        MemoryRevisionRow.tenant_id == owner.tenant_id,
                        MemoryRevisionRow.principal_id == owner.principal_id,
                        MemoryRevisionRow.recorded_at <= query.known_at,
                    )
                    .distinct(MemoryRevisionRow.belief_id)
                    .order_by(
                        MemoryRevisionRow.belief_id,
                        MemoryRevisionRow.recorded_at.desc(),
                        MemoryRevisionRow.id.desc(),
                    )
                    .subquery("historical_relationship_beliefs")
                )
                historical_payload = latest.c.payload
                related = related.join(latest, latest.c.belief_id == MemoryRow.id).where(
                    historical_payload["sensitivity"].astext.in_(
                        [
                            key.value
                            for key, rank in SENSITIVITY_ORDER.items()
                            if rank <= SENSITIVITY_ORDER[query.sensitivity_ceiling]
                        ]
                    ),
                    cast(historical_payload["valid_from"].astext, DateTime(timezone=True))
                    <= instant,
                    or_(
                        historical_payload["valid_to"].astext.is_(None),
                        cast(historical_payload["valid_to"].astext, DateTime(timezone=True))
                        > instant,
                    ),
                    or_(
                        historical_payload["expires_at"].astext.is_(None),
                        cast(historical_payload["expires_at"].astext, DateTime(timezone=True))
                        > instant,
                    ),
                )
                if instant >= self._clock.now():
                    related = related.where(
                        historical_payload["status"].astext.in_(
                            [status.value for status in LIVE_MEMORY_STATUSES]
                        )
                    )
            else:
                related = related.where(
                    MemoryRow.valid_from <= instant,
                    or_(MemoryRow.valid_to.is_(None), MemoryRow.valid_to > instant),
                    or_(MemoryRow.expires_at.is_(None), MemoryRow.expires_at > instant),
                )
                if instant >= self._clock.now():
                    related = related.where(
                        MemoryRow.status.in_([status.value for status in LIVE_MEMORY_STATUSES])
                    )
            statement = statement.where(PeopleRevisionRow.entity_id.in_(related))
        if query.root_erasures_only:
            statement = statement.where(
                PeopleRevisionRow.kind == "erasure",
                PeopleRevisionRow.payload["parent_id"].astext.is_(None),
            )
        if query.states is not None:
            statement = statement.where(PeopleRevisionRow.payload["state"].astext.in_(query.states))
        if query.identifier_value is not None:
            statement = statement.where(
                func.lower(PeopleRevisionRow.search_text) == query.identifier_value.lower()
            )
        is_identifier = PeopleRevisionRow.kind == "identifier"
        attached = PeopleRevisionRow.payload["person_id"].astext.is_not(None)
        if query.assigned == "attached":
            statement = statement.where(or_(~is_identifier, attached))
        elif query.assigned == "unattached":
            statement = statement.where(is_identifier, ~attached)
        if query.valid_at is not None:
            statement = statement.where(
                or_(
                    ~is_identifier,
                    and_(
                        cast(
                            PeopleRevisionRow.payload["valid_from"].astext, DateTime(timezone=True)
                        )
                        <= query.valid_at,
                        or_(
                            PeopleRevisionRow.payload["valid_to"].astext.is_(None),
                            cast(
                                PeopleRevisionRow.payload["valid_to"].astext,
                                DateTime(timezone=True),
                            )
                            > query.valid_at,
                        ),
                    ),
                )
            )
        if query.needs_review:
            verified = self._query(owner, query.sensitivity_ceiling, query.known_at).subquery(
                "verified_aliases"
            )
            statement = statement.where(
                PeopleRevisionRow.kind == "person",
                PeopleRevisionRow.payload["state"].astext == "provisional",
                PeopleRevisionRow.payload["pinned"].astext == "false",
                ~exists(
                    select(1).where(
                        verified.c.kind == "identifier",
                        verified.c.payload["person_id"].astext
                        == cast(PeopleRevisionRow.entity_id, Text),
                        verified.c.payload["verification"].astext.in_(
                            ["owner_confirmed", "channel_observed"]
                        ),
                    )
                ),
            )
        if not query.include_superseded:
            statement = statement.where(PeopleRevisionRow.payload["superseded_by"].astext.is_(None))
        if query.pinned is not None:
            statement = statement.where(
                PeopleRevisionRow.payload["pinned"].astext == str(query.pinned).lower()
            )
        for ref, role in ((query.person_id, "person"), (query.source_id, "source")):
            if ref:
                assignments = (
                    select(PeopleLinkRow.entity_id, PeopleLinkRow.revision)
                    .where(
                        PeopleLinkRow.tenant_id == owner.tenant_id,
                        PeopleLinkRow.principal_id == owner.principal_id,
                        PeopleLinkRow.target_id == ref,
                        PeopleLinkRow.role == role,
                    )
                    .offset(0)
                )
                statement = statement.where(
                    tuple_(PeopleRevisionRow.entity_id, PeopleRevisionRow.revision).in_(assignments)
                )
        if query.belief_id is not None:
            statement = statement.where(
                PeopleRevisionRow.payload["belief_id"].astext == str(query.belief_id)
            )
        if query.belief_ids is not None:
            statement = statement.where(
                PeopleRevisionRow.payload["belief_id"].astext.in_(
                    [str(key) for key in query.belief_ids]
                )
            )
        if query.session_id:
            statement = statement.where(PeopleRevisionRow.source_session_id == query.session_id)
        if query.account_id is not None:
            statement = statement.where(
                PeopleRevisionRow.payload["account_id"].astext == query.account_id
            )
        if query.thread_id is not None:
            statement = statement.where(
                PeopleRevisionRow.payload["thread_id"].astext == query.thread_id
            )
        if query.message_ids:
            statement = statement.where(
                PeopleRevisionRow.payload["message_id"].astext.in_(query.message_ids)
            )
        if query.mentioned_in:
            statement = statement.where(
                PeopleRevisionRow.search_text != "",
                func.strpos(
                    query.mentioned_in.casefold(), func.lower(PeopleRevisionRow.search_text)
                )
                > 0,
            )
        if query.text:
            name_match = PeopleRevisionRow.search_text.icontains(query.text, autoescape=True)
            if query.search_aliases:
                aliases = self._query(owner, query.sensitivity_ceiling, query.known_at).subquery(
                    "visible_aliases"
                )
                alias_query = select(1).where(
                    aliases.c.kind == "identifier",
                    aliases.c.payload["person_id"].astext
                    == cast(PeopleRevisionRow.entity_id, Text),
                    aliases.c.search_text.icontains(query.text, autoescape=True),
                )
                if query.as_of is not None:
                    alias_query = alias_query.where(
                        cast(aliases.c.payload["valid_from"].astext, DateTime(timezone=True))
                        <= query.as_of,
                        or_(
                            aliases.c.payload["valid_to"].astext.is_(None),
                            cast(aliases.c.payload["valid_to"].astext, DateTime(timezone=True))
                            > query.as_of,
                        ),
                    )
                name_match = or_(name_match, exists(alias_query))
            statement = statement.where(name_match)
        payload = PeopleRevisionRow.payload
        # JSON timestamp is used only for null detection; the indexed event_at
        # column supplies chronological comparisons and ordering.
        known = payload["occurred_at"].astext.is_not(None)
        if query.channel:
            statement = statement.where(payload["channel"].astext == query.channel)
        if query.interaction_kind:
            statement = statement.where(
                payload["interaction_kind"].astext == query.interaction_kind
            )
        if query.unknown_time != "include":
            statement = statement.where(known if query.unknown_time == "exclude" else ~known)
        if query.after and query.sort != "recent":
            if query.sort == "id":
                statement = statement.where(PeopleRevisionRow.entity_id > query.after)
            elif query.after_event_at is None:
                statement = statement.where(~known, PeopleRevisionRow.entity_id > query.after)
            else:
                statement = statement.where(
                    or_(
                        ~known,
                        PeopleRevisionRow.event_at < query.after_event_at,
                        and_(
                            PeopleRevisionRow.event_at == query.after_event_at,
                            PeopleRevisionRow.entity_id > query.after,
                        ),
                    )
                )
        if query.since:
            statement = statement.where(known, PeopleRevisionRow.event_at >= query.since)
        if query.until:
            statement = statement.where(known, PeopleRevisionRow.event_at < query.until)
        if query.as_of:
            statement = statement.where(
                or_(
                    PeopleRevisionRow.kind != "interaction",
                    and_(known, PeopleRevisionRow.event_at <= query.as_of),
                ),
                or_(
                    payload["valid_from"].astext.is_(None),
                    cast(payload["valid_from"].astext, DateTime(timezone=True)) <= query.as_of,
                ),
                or_(
                    payload["valid_to"].astext.is_(None),
                    cast(payload["valid_to"].astext, DateTime(timezone=True)) > query.as_of,
                ),
            )
        ordering = (
            [
                known.desc(),
                case((known, PeopleRevisionRow.event_at), else_=None).desc(),
                PeopleRevisionRow.entity_id,
            ]
            if query.sort == "history"
            else [PeopleRevisionRow.entity_id]
        )
        if query.sort == "recent":
            interactions = self._query(owner, query.sensitivity_ceiling, query.known_at).where(
                PeopleRevisionRow.kind == "interaction",
                PeopleRevisionRow.payload["occurred_at"].astext.is_not(None),
                PeopleRevisionRow.payload["superseded_by"].astext.is_(None),
            )
            if query.as_of is not None:
                interactions = interactions.where(PeopleRevisionRow.event_at <= query.as_of)
            visible = interactions.subquery("visible_interactions")
            recent = (
                select(PeopleLinkRow.target_id, func.max(visible.c.event_at).label("recent_at"))
                .join(
                    visible,
                    (PeopleLinkRow.entity_id == visible.c.entity_id)
                    & (PeopleLinkRow.revision == visible.c.revision)
                    & (PeopleLinkRow.tenant_id == visible.c.tenant_id)
                    & (PeopleLinkRow.principal_id == visible.c.principal_id),
                )
                .where(PeopleLinkRow.role == "person")
                .group_by(PeopleLinkRow.target_id)
                .cte("recent_people")
            )
            statement = statement.outerjoin(
                recent, recent.c.target_id == PeopleRevisionRow.entity_id
            )
            at = func.coalesce(recent.c.recent_at, datetime.min.replace(tzinfo=UTC))
            if query.after:
                previous = func.coalesce(
                    select(recent.c.recent_at)
                    .where(recent.c.target_id == query.after)
                    .scalar_subquery(),
                    datetime.min.replace(tzinfo=UTC),
                )
                statement = statement.where(
                    or_(
                        at < previous,
                        and_(at == previous, PeopleRevisionRow.entity_id > query.after),
                    )
                )
            ordering = [at.desc(), PeopleRevisionRow.entity_id]
        if query.distinct_assignments:
            # One row per assignment: per-message identifier copies of one
            # address collapse to their lowest id, as the in-memory store does.
            keys = [
                PeopleRevisionRow.kind,
                case(
                    (is_identifier, PeopleRevisionRow.payload["person_id"].astext),
                    else_=cast(PeopleRevisionRow.entity_id, Text),
                ),
                PeopleRevisionRow.payload["identifier_kind"].astext,
                PeopleRevisionRow.payload["namespace"].astext,
                PeopleRevisionRow.payload["context"].astext,
                PeopleRevisionRow.payload["verification"].astext,
                func.lower(PeopleRevisionRow.search_text),
            ]
            distinct = (
                statement.distinct(*keys)
                .order_by(*keys, PeopleRevisionRow.entity_id)
                .subquery("distinct_assignments")
            )
            row_alias = aliased(PeopleRevisionRow, distinct)
            rows = (
                await self._session.execute(
                    select(row_alias).order_by(row_alias.entity_id).limit(query.limit + 1)
                )
            ).scalars()
            return [PEOPLE_RECORD.validate_python(row.payload) for row in rows]
        rows = (
            await self._session.execute(statement.order_by(*ordering).limit(query.limit + 1))
        ).scalars()
        return [PEOPLE_RECORD.validate_python(row.payload) for row in rows]

    async def put(self, record: PeopleRecord, *, expected_revision: int) -> PeopleRecord:
        owner = _principal(record)
        async with self.lock(owner):
            current = await self.get(owner, record.id, ceiling=Sensitivity.RESTRICTED)
            record = _validate(record, current, expected_revision)
            for ref in record.support_ids:
                if not isinstance(
                    await self.get(owner, ref, ceiling=Sensitivity.RESTRICTED), PeopleSource
                ):
                    raise ConflictError("people source is missing or excluded")
            for ref in referenced_people(record):
                if not isinstance(
                    await self.get(owner, ref, ceiling=Sensitivity.RESTRICTED), Person
                ):
                    raise ConflictError("people reference is missing")
            for ref in referenced_organizations(record):
                if not isinstance(
                    await self.get(owner, ref, ceiling=Sensitivity.RESTRICTED),
                    OrganizationReference,
                ):
                    raise ConflictError("organization reference is missing")
            for ref in referenced_assignments(record):
                if await self.get(owner, ref, ceiling=Sensitivity.RESTRICTED) is None:
                    raise ConflictError("people assignment reference is missing")
            values = {
                "tenant_id": record.tenant_id,
                "principal_id": record.principal_id,
                "id": record.id,
                "kind": record.kind,
                "revision": record.revision,
                "sensitivity": SENSITIVITY_ORDER[record.sensitivity],
                "erased": False,
                "excluded": isinstance(record, PeopleSource) and record.excluded,
            }
            if expected_revision == 0:
                changed = (
                    await self._session.execute(
                        pg_insert(PeopleHeadRow)
                        .values(**values)
                        .on_conflict_do_nothing()
                        .returning(PeopleHeadRow.id)
                    )
                ).scalar_one_or_none()
            else:
                changed = (
                    await self._session.execute(
                        update(PeopleHeadRow)
                        .where(
                            PeopleHeadRow.tenant_id == owner.tenant_id,
                            PeopleHeadRow.principal_id == owner.principal_id,
                            PeopleHeadRow.id == record.id,
                            PeopleHeadRow.revision == expected_revision,
                            ~PeopleHeadRow.erased,
                        )
                        .values(**values)
                        .returning(PeopleHeadRow.id)
                    )
                ).scalar_one_or_none()
            if changed is None:
                raise ConflictError("people revision changed or was erased")
            await self._session.execute(
                pg_insert(PeopleRevisionRow).values(
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    entity_id=record.id,
                    revision=record.revision,
                    kind=record.kind,
                    recorded_at=record.updated_at,
                    event_at=event_time(record),
                    sensitivity=SENSITIVITY_ORDER[record.sensitivity],
                    search_text=_name(record),
                    source_session_id=record.session_id
                    if isinstance(record, PeopleSource)
                    else None,
                    payload=record.model_dump(mode="json"),
                )
            )
            await self._session.flush()
            for refs, role in (
                (record.support_ids, "source"),
                (referenced_people(record), "person"),
                (referenced_organizations(record), "organization"),
                (referenced_assignments(record), "assignment"),
            ):
                for ref in refs:
                    await self._session.execute(
                        pg_insert(PeopleLinkRow).values(
                            tenant_id=owner.tenant_id,
                            principal_id=owner.principal_id,
                            entity_id=record.id,
                            revision=record.revision,
                            target_id=ref,
                            role=role,
                        )
                    )
            await self._session.flush()
            return record

    async def fence_for_erasure(self, principal: Principal, record_ids: Sequence[UUID]) -> int:
        return await self._erase(principal, record_ids, fence_only=True)

    async def purge_erased(self, principal: Principal, *, limit: int = 256) -> bool:
        if not 1 <= limit <= 256:
            raise ValueError("People erasure batches contain at most 256 revisions")
        pending = (
            select(PeopleRevisionRow.entity_id, PeopleRevisionRow.revision)
            .join(
                PeopleHeadRow,
                and_(
                    PeopleHeadRow.tenant_id == PeopleRevisionRow.tenant_id,
                    PeopleHeadRow.principal_id == PeopleRevisionRow.principal_id,
                    PeopleHeadRow.id == PeopleRevisionRow.entity_id,
                ),
            )
            .where(
                PeopleHeadRow.tenant_id == principal.tenant_id,
                PeopleHeadRow.principal_id == principal.principal_id,
                PeopleHeadRow.erased,
            )
        )
        async with self.lock(principal):
            await self._prepare_read()
            page = (
                await self._session.execute(
                    pending.order_by(PeopleRevisionRow.entity_id, PeopleRevisionRow.revision).limit(
                        limit
                    )
                )
            ).all()
            if not page:
                return False
            # Materialize only bounded opaque keys. A self-referencing DELETE
            # subquery can repeatedly scan its target while enforcing cascades.
            await self._session.execute(
                delete(PeopleRevisionRow)
                .where(
                    PeopleRevisionRow.tenant_id == principal.tenant_id,
                    PeopleRevisionRow.principal_id == principal.principal_id,
                    tuple_(PeopleRevisionRow.entity_id, PeopleRevisionRow.revision).in_(
                        [(row.entity_id, row.revision) for row in page]
                    ),
                )
                .execution_options(synchronize_session=False)
            )
            return (await self._session.execute(pending.limit(1))).first() is not None

    async def erase(
        self,
        principal: Principal,
        record_ids: Sequence[UUID],
        *,
        preserve_independent: bool = False,
    ) -> int:
        return await self._erase(principal, record_ids, preserve_independent=preserve_independent)

    async def _erase(
        self,
        principal: Principal,
        record_ids: Sequence[UUID],
        *,
        preserve_independent: bool = False,
        fence_only: bool = False,
    ) -> int:
        async with self.lock(principal):
            if preserve_independent:
                await self._rebase_source_copies(principal, record_ids)
            doomed = (
                select(PeopleHeadRow.id)
                .where(
                    PeopleHeadRow.tenant_id == principal.tenant_id,
                    PeopleHeadRow.principal_id == principal.principal_id,
                    PeopleHeadRow.id
                    == any_(bindparam(None, list(record_ids), type_=ARRAY(PGUUID(as_uuid=True)))),
                )
                .cte("people_erasure", recursive=True)
            )
            dependent = (
                select(PeopleLinkRow.entity_id)
                .join(
                    doomed,
                    PeopleLinkRow.target_id == doomed.c.id,
                )
                .where(
                    PeopleLinkRow.tenant_id == principal.tenant_id,
                    PeopleLinkRow.principal_id == principal.principal_id,
                )
            )
            if preserve_independent:
                dependent = dependent.join(
                    PeopleHeadRow,
                    and_(
                        PeopleHeadRow.tenant_id == PeopleLinkRow.tenant_id,
                        PeopleHeadRow.principal_id == PeopleLinkRow.principal_id,
                        PeopleHeadRow.id == PeopleLinkRow.entity_id,
                        PeopleHeadRow.revision == PeopleLinkRow.revision,
                    ),
                )
            doomed = doomed.union(dependent)
            ids = list(
                (
                    await self._session.execute(
                        update(PeopleHeadRow)
                        .where(
                            PeopleHeadRow.tenant_id == principal.tenant_id,
                            PeopleHeadRow.principal_id == principal.principal_id,
                            PeopleHeadRow.id.in_(select(doomed.c.id)),
                            ~PeopleHeadRow.erased,
                        )
                        .values(erased=True)
                        .returning(PeopleHeadRow.id)
                    )
                ).scalars()
            )
            if fence_only:
                return len(ids)
            if preserve_independent:
                stale = (
                    select(PeopleLinkRow.entity_id, PeopleLinkRow.revision)
                    .where(
                        PeopleLinkRow.tenant_id == principal.tenant_id,
                        PeopleLinkRow.principal_id == principal.principal_id,
                        PeopleLinkRow.target_id
                        == any_(bindparam(None, ids, type_=ARRAY(PGUUID(as_uuid=True)))),
                    )
                    .subquery()
                )
                await self._session.execute(
                    delete(PeopleRevisionRow).where(
                        PeopleRevisionRow.tenant_id == principal.tenant_id,
                        PeopleRevisionRow.principal_id == principal.principal_id,
                        exists(
                            select(1).where(
                                stale.c.entity_id == PeopleRevisionRow.entity_id,
                                stale.c.revision == PeopleRevisionRow.revision,
                            )
                        ),
                    )
                )
            await self._session.execute(
                delete(PeopleRevisionRow).where(
                    PeopleRevisionRow.tenant_id == principal.tenant_id,
                    PeopleRevisionRow.principal_id == principal.principal_id,
                    PeopleRevisionRow.entity_id
                    == any_(bindparam(None, ids, type_=ARRAY(PGUUID(as_uuid=True)))),
                )
            )
            return len(ids)

    async def _rebase_source_copies(self, principal: Principal, record_ids: Sequence[UUID]) -> None:
        roots = (
            await self._session.scalars(
                self._query(principal, Sensitivity.RESTRICTED, None).where(
                    PeopleRevisionRow.entity_id.in_(record_ids), PeopleRevisionRow.kind == "source"
                )
            )
        ).all()
        groups = {
            row.entity_id: row.payload.get("copy_group")
            for row in roots
            if row.payload.get("copy_group")
        }
        if not groups:
            return
        copies = (
            await self._session.scalars(
                self._query(principal, Sensitivity.RESTRICTED, None).where(
                    PeopleRevisionRow.kind == "source",
                    ~PeopleRevisionRow.entity_id.in_(record_ids),
                    PeopleRevisionRow.payload["copy_group"].astext.in_(set(groups.values())),
                )
            )
        ).all()
        alternatives = {
            row.payload["copy_group"]: row.entity_id
            for row in copies
            if not row.payload.get("support_ids")
        }
        replacements = {
            key: alternatives[group] for key, group in groups.items() if group in alternatives
        }
        for source_id in replacements:
            query = PeopleQuery(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                source_id=source_id,
                kinds=["person", "identifier", "interaction"],
                limit=100,
            )
            while True:
                rows = await self.query(query)
                for row in rows[:100]:
                    replacement = _copy_rebased(row, replacements, self._clock.now())
                    if replacement is not None:
                        await self.put(replacement, expected_revision=row.revision)
                if len(rows) <= 100:
                    break
                query = query.model_copy(update={"after": rows[99].id})

    async def erase_email_source(
        self, principal: Principal, account_id: str, thread_id: str, message_ids: frozenset[str]
    ) -> int:
        statement = select(PeopleRevisionRow.entity_id).where(
            PeopleRevisionRow.tenant_id == principal.tenant_id,
            PeopleRevisionRow.principal_id == principal.principal_id,
            PeopleRevisionRow.kind == "source",
            PeopleRevisionRow.payload["source_kind"].astext == "email",
            PeopleRevisionRow.payload["account_id"].astext == account_id,
            PeopleRevisionRow.payload["thread_id"].astext == thread_id,
        )
        if message_ids:
            statement = statement.where(
                PeopleRevisionRow.payload["message_id"].astext.in_(message_ids)
            )
        ids = list((await self._session.execute(statement.distinct())).scalars())
        return await self._erase(principal, ids, preserve_independent=True)

    async def erase_session(self, principal: Principal, session_id: UUID) -> int:
        ids = list(
            (
                await self._session.execute(
                    select(PeopleRevisionRow.entity_id)
                    .where(
                        PeopleRevisionRow.tenant_id == principal.tenant_id,
                        PeopleRevisionRow.principal_id == principal.principal_id,
                        PeopleRevisionRow.source_session_id == session_id,
                    )
                    .distinct()
                )
            ).scalars()
        )
        return await self._erase(principal, ids, preserve_independent=True)

    async def erase_principal(self, principal: Principal) -> int:
        async with self.lock(principal):
            result = await self._session.execute(
                delete(PeopleHeadRow)
                .where(
                    PeopleHeadRow.tenant_id == principal.tenant_id,
                    PeopleHeadRow.principal_id == principal.principal_id,
                )
                .returning(PeopleHeadRow.id)
            )
            return len(list(result.scalars()))
