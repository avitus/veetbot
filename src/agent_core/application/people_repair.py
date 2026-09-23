"""The one-time People directory repair (ADR-0121).

People holds the people the owner knows and the people the owner writes to.
Before ADR-0121, formation admitted anyone named in mail, and correspondence
never ran because every refresh marked the account as syncing. The repair:

1. projects the headers of retained mail from the last 90 days again, so
   everyone the owner wrote to is recorded with their history;
2. gives each active person an owner-confirmed name alias when they have none;
3. removes provisional people that nothing the owner did ties to them.

Removing a person deletes the facts mail formed about them, unlinks facts the
owner stated, suppresses no source, and records a content-free audit event.
A preview writes nothing. Its list comes before the backfill, which can only
keep more people.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID, uuid5

from agent_core.application.authorization import require_scope
from agent_core.application.people_self import owner_references
from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailRecord
from agent_core.domain.errors import (
    ConflictError,
    NotFoundError,
    ToolTrustRejectedError,
    ToolValidationError,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import MemoryAuthority, Sensitivity
from agent_core.domain.people import (
    PeopleCommitment,
    PeopleImportJob,
    PeopleInteraction,
    PeopleQuery,
    PeopleRecord,
    PeopleSource,
    Person,
    PersonIdentifier,
    PersonMemoryLink,
    PersonMention,
    RelationshipAssertion,
    is_non_person_reference,
    is_self_reference,
    normalize_identifier,
)
from agent_core.domain.people_views import PeopleRepairCandidate, PeopleRepairReport
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

REPAIR_WINDOW = timedelta(days=90)
REPAIR_VERSION = "people-directory-repair@1"
_RUNNING_IMPORTS = frozenset({"queued", "running"})
_OWNER_AUTHORITY = frozenset({MemoryAuthority.USER, MemoryAuthority.AFFIRMED})
_PREVIEW_NOTE = (
    "Preview only; nothing changed. With --confirm the repair first records the retained "
    "mail you sent, which keeps everyone you wrote to, then removes the people listed "
    "here that still qualify."
)
_CONFIRMED_NOTE = (
    "Removed people keep no directory entry. Facts mail formed only about them were "
    "deleted; facts you stated remain, unlinked. No message or source was deleted."
)

Reason = Literal["unconfirmed", "pronoun", "self"]


class BeliefDeletion(Protocol):
    """The governed memory delete (ADR-0117), run inside the repair's transaction."""

    async def delete(
        self,
        belief_id: UUID,
        *,
        trace_id: UUID | None = None,
        existing_uow: RepositoryUnitOfWork | None = None,
    ) -> None: ...


@dataclass
class _Scan:
    """Per-transaction lookups shared across the people one pass inspects."""

    references: frozenset[str]
    sources: dict[UUID, PeopleSource | None] = field(default_factory=dict)
    assertions: dict[UUID, bool] = field(default_factory=dict)
    explicit: dict[UUID, bool] = field(default_factory=dict)


class PeopleDirectoryRepair:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        *,
        memory_for: Callable[[Principal], BeliefDeletion],
        reproject: Callable[[Principal, EmailRecord], Awaitable[bool]],
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._memory_for = memory_for
        self._reproject = reproject

    async def run(
        self, principal: Principal, *, confirm: bool, session_id: UUID | None = None
    ) -> PeopleRepairReport:
        require_scope(principal, "people.write")
        if confirm and session_id is None:
            raise ToolValidationError("a confirmed repair records its audit in a session")
        async with self._uow_factory() as uow:
            await _refuse_during_import(uow, principal)
            if session_id is not None:
                await uow.sessions.get(session_id, principal)
        retained = await self._retained_mail(principal)
        if not confirm:
            async with self._uow_factory() as uow:
                unaliased = await _unaliased(uow, principal)
                candidates = await self._candidates(uow, principal)
                pruned = {candidate.person_id for candidate in candidates}
                sources = await _fact_threads(uow, principal)
                deleted: set[UUID] = set()
                unlinked: set[UUID] = set()
                scan = _Scan(references=await owner_references(uow.email, principal))
                for candidate in candidates:
                    removed_facts, kept_facts = await self._settle_beliefs(
                        uow, principal, candidate.person_id, pruned, scan, apply=False
                    )
                    deleted.update(removed_facts)
                    unlinked.update(kept_facts)
            return PeopleRepairReport(
                confirmed=False,
                retained_mail=len(retained),
                mail_projected=0,
                mail_skipped=0,
                aliases_added=[person.display_name for person in unaliased],
                candidates=candidates,
                beliefs_deleted=len(deleted),
                beliefs_unlinked=len(unlinked - deleted),
                mail_threads_reset=len(_threads(sources, deleted)),
                note=_PREVIEW_NOTE,
            )
        assert session_id is not None
        projected = 0
        for key in retained:
            projected += int(await self._reproject_one(principal, key))
        aliases = await self._add_aliases(principal, session_id)
        async with self._uow_factory() as uow:
            candidates = await self._candidates(uow, principal)
            sources = await _fact_threads(uow, principal)
        pruned = {candidate.person_id for candidate in candidates}
        removed: list[PeopleRepairCandidate] = []
        deleted = set()
        unlinked = set()
        for candidate in candidates:
            outcome = await self._prune(principal, candidate, pruned, session_id)
            if outcome is not None:
                removed.append(candidate)
                deleted.update(outcome[0])
                unlinked.update(outcome[1])
        return PeopleRepairReport(
            confirmed=True,
            retained_mail=len(retained),
            mail_projected=projected,
            mail_skipped=len(retained) - projected,
            aliases_added=aliases,
            candidates=removed,
            beliefs_deleted=len(deleted),
            beliefs_unlinked=len(unlinked - deleted),
            mail_threads_reset=len(_threads(sources, deleted)),
            note=_CONFIRMED_NOTE,
        )

    async def _retained_mail(self, principal: Principal) -> list[str]:
        """Retained, unexcluded messages inside the window, oldest first."""
        since = self._clock.now() - REPAIR_WINDOW
        found: list[tuple[datetime, str]] = []
        after: str | None = None
        async with self._uow_factory() as uow:
            while True:
                page = await uow.email.list(principal, "semantic_source", after=after, limit=1000)
                for record in page:
                    if record.payload.get("excluded"):
                        continue
                    try:
                        at = datetime.fromisoformat(str(record.payload.get("evidence_at")))
                    except ValueError:
                        continue
                    if at.tzinfo is not None and at >= since:
                        found.append((at, record.key))
                if len(page) < 1000:
                    break
                after = page[-1].key
        return [key for _, key in sorted(found)]

    async def _reproject_one(self, principal: Principal, key: str) -> bool:
        """One message per transaction; bulk, excluded, erased or unverifiable mail is skipped."""
        async with self._uow_factory() as uow:
            record = await uow.email.get(principal, "semantic_source", key)
        if record is None:
            return False
        try:
            return await self._reproject(principal, record)
        except (ConflictError, NotFoundError, ToolTrustRejectedError, ToolValidationError):
            return False

    async def _add_aliases(self, principal: Principal, session_id: UUID) -> list[str]:
        """Owner-created people are found again when the owner names them in chat."""
        added: list[str] = []
        async with self._uow_factory() as uow, uow.people.lock(principal):
            for person in await _unaliased(uow, principal):
                name = normalize_identifier("name", "owner", person.display_name)
                alias_id = uuid5(person.id, f"{REPAIR_VERSION}:name:{name}")
                if await uow.people.is_erased(principal, alias_id):
                    continue
                now = self._clock.now()
                content = f"Owner confirmed the name {person.display_name} in the directory repair"
                request_hash = hashlib.sha256(f"{REPAIR_VERSION}:{alias_id}".encode()).hexdigest()
                event = await uow.events.append(
                    NewEvent(
                        session_id=session_id,
                        run_id=None,
                        event_type="people.owner_assertion",
                        actor_type="user",
                        actor_id=principal.principal_id,
                        derivation_key=f"{REPAIR_VERSION}:{alias_id}",
                        payload={
                            "content": content,
                            "request_hash": request_hash,
                            "person_id": str(person.id),
                            "person_revision": person.revision,
                        },
                    )
                )
                source = PeopleSource(
                    id=uuid5(alias_id, "source"),
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    created_at=now,
                    updated_at=now,
                    sensitivity=person.sensitivity,
                    session_id=session_id,
                    event_sequence=event.sequence,
                    evidence_at=event.created_at,
                    source_kind="owner",
                    source_revision=request_hash,
                )
                await uow.people.put(source, expected_revision=0)
                await uow.people.put(
                    PersonIdentifier(
                        id=alias_id,
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        created_at=now,
                        updated_at=now,
                        sensitivity=person.sensitivity,
                        person_id=person.id,
                        identifier_kind="name",
                        namespace="owner",
                        value=person.display_name,
                        context="owner",
                        verification="owner_confirmed",
                        valid_from=now,
                        support_ids=[source.id],
                    ),
                    expected_revision=0,
                )
                added.append(person.display_name)
        return added

    async def _candidates(
        self, uow: RepositoryUnitOfWork, principal: Principal
    ) -> list[PeopleRepairCandidate]:
        scan = _Scan(references=await owner_references(uow.email, principal))
        query = PeopleQuery(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            kinds=["person"],
            states=["provisional"],
            pinned=False,
            sensitivity_ceiling=Sensitivity.RESTRICTED,
            limit=100,
        )
        found: list[PeopleRepairCandidate] = []
        while True:
            page = await uow.people.query(query)
            for person in page[:100]:
                if not isinstance(person, Person):
                    continue
                reason = await self._reason(uow, principal, person, scan)
                if reason is not None:
                    found.append(
                        PeopleRepairCandidate(
                            person_id=person.id, display_name=person.display_name, reason=reason
                        )
                    )
            if len(page) <= 100:
                return found
            query = query.model_copy(update={"after": page[99].id})

    async def _reason(
        self, uow: RepositoryUnitOfWork, principal: Principal, person: Person, scan: _Scan
    ) -> Reason | None:
        """Why this person leaves People, or None when the owner's evidence keeps them."""
        if person.state != "provisional" or person.pinned:
            return None
        # A pronoun or one of the owner's own addresses is never a person, whatever
        # else was recorded about it.
        if is_non_person_reference(person.display_name):
            return "pronoun"
        identifiers = [
            row
            for row in await _rows(uow, principal, ["identifier"], person.id)
            if isinstance(row, PersonIdentifier)
        ]
        if is_self_reference("email", person.display_name, scan.references) or any(
            row.identifier_kind in {"email", "handle"}
            and is_self_reference(row.identifier_kind, row.value, scan.references)
            for row in identifiers
        ):
            return "self"
        if any(row.verification == "owner_confirmed" for row in identifiers):
            return None
        if await self._owner_asserted(uow, principal, person.support_ids, scan):
            return None
        # Merges, splits, forgets and import scopes are the owner's own operations.
        if await _rows(uow, principal, ["operation", "import_job", "person"], person.id, limit=1):
            return None
        for row in await _rows(uow, principal, ["relationship", "commitment"], person.id):
            if not isinstance(row, (RelationshipAssertion, PeopleCommitment)):
                continue
            if await self._owner_asserted(uow, principal, row.support_ids, scan):
                return None
            ends = (
                (row.subject, row.object)
                if isinstance(row, RelationshipAssertion)
                else (row.debtor, row.beneficiary)
            )
            # A tie mail reported between the owner and someone is not the owner's word.
            if any(end.kind == "owner" for end in ends) and await self._owner_stated(
                uow, principal, row.support_ids, scan
            ):
                return None
        for row in await _rows(uow, principal, ["interaction"], person.id):
            if not isinstance(row, PeopleInteraction):
                continue
            roles = {p.role for p in row.participants if p.person_id == person.id} - {"mentioned"}
            if roles and (
                (row.direction == "outgoing" and row.channel in {"email", "sms"})
                or row.attribution == "owner_reported"
            ):
                return None
        for row in await _rows(uow, principal, ["memory_link"], person.id):
            if not isinstance(row, PersonMemoryLink):
                continue
            if await self._owner_asserted(
                uow, principal, row.support_ids, scan
            ) or await self._explicit(uow, principal, row.belief_id, scan):
                return None
        return "unconfirmed"

    async def _prune(
        self,
        principal: Principal,
        candidate: PeopleRepairCandidate,
        pruned: set[UUID],
        session_id: UUID,
    ) -> tuple[list[UUID], list[UUID]] | None:
        """Remove one person in one transaction; None when a recheck keeps them."""
        person_id = candidate.person_id
        async with (
            self._uow_factory() as uow,
            uow.email.lock(principal),
            uow.people.lock(principal),
        ):
            await _refuse_during_import(uow, principal)
            person = await uow.people.get(principal, person_id, ceiling=Sensitivity.RESTRICTED)
            scan = _Scan(references=await owner_references(uow.email, principal))
            if not isinstance(person, Person):
                return None
            reason = await self._reason(uow, principal, person, scan)
            if reason is None:
                return None
            deleted, unlinked = await self._settle_beliefs(
                uow, principal, person_id, pruned, scan, apply=True
            )
            now = self._clock.now()
            for row in await _rows(uow, principal, ["mention", "identifier"], person_id):
                keep_unattached = isinstance(row, PersonMention) or (
                    isinstance(row, PersonIdentifier)
                    and row.identifier_kind == "email"
                    and row.verification == "channel_observed"
                )
                # A detached mention keeps the fact's provenance; a detached address
                # endpoint lets a later reply adopt this mail as history.
                if keep_unattached and isinstance(row, (PersonMention, PersonIdentifier)):
                    await uow.people.put(
                        row.model_copy(
                            update={
                                "person_id": None,
                                "revision": row.revision + 1,
                                "updated_at": max(now, row.updated_at + timedelta(microseconds=1)),
                            }
                        ),
                        expected_revision=row.revision,
                    )
            for row in await _rows(uow, principal, ["interaction"], person_id):
                if not isinstance(row, PeopleInteraction):
                    continue
                others = [p for p in row.participants if p.person_id != person_id]
                if others and len(others) < len(row.participants):
                    await uow.people.put(
                        row.model_copy(
                            update={
                                "participants": others,
                                "revision": row.revision + 1,
                                "updated_at": max(now, row.updated_at + timedelta(microseconds=1)),
                            }
                        ),
                        expected_revision=row.revision,
                    )
            await uow.people.erase(principal, [person_id], preserve_independent=True)
            await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="people.directory_pruned",
                    actor_type="user",
                    actor_id=principal.principal_id,
                    payload={
                        "repair": REPAIR_VERSION,
                        "person_id": str(person_id),
                        "reason": reason,
                        "beliefs_deleted": len(deleted),
                        "beliefs_unlinked": len(unlinked),
                    },
                )
            )
            return deleted, unlinked

    async def _settle_beliefs(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        person_id: UUID,
        pruned: set[UUID],
        scan: _Scan,
        *,
        apply: bool,
    ) -> tuple[list[UUID], list[UUID]]:
        """Facts mail formed only about removed people are deleted; the rest are unlinked.

        With `apply` false nothing is written and the same split is reported.
        """
        supports: dict[UUID, list[UUID]] = {}
        subject_of: set[UUID] = set()
        for row in await _rows(
            uow, principal, ["memory_link", "relationship", "commitment"], person_id
        ):
            if isinstance(row, PersonMemoryLink):
                supports.setdefault(row.belief_id, []).extend(row.support_ids)
                if row.role == "subject":
                    subject_of.add(row.belief_id)
            elif isinstance(row, (RelationshipAssertion, PeopleCommitment)):
                supports.setdefault(row.belief_id, []).extend(row.support_ids)
                subject_of.add(row.belief_id)
        memory = self._memory_for(principal)
        deleted: list[UUID] = []
        unlinked: list[UUID] = []
        for belief_id, support_ids in supports.items():
            try:
                belief = await uow.memories.get(belief_id, principal)
            except NotFoundError:
                continue
            kinds = set()
            for source_id in support_ids:
                source = await self._source(uow, principal, source_id, scan)
                kinds.add(None if source is None else source.source_kind)
            mail_formed = belief.authority is MemoryAuthority.INFERRED and kinds == {"email"}
            if (
                mail_formed
                and belief_id in subject_of
                and await _subjects(uow, principal, belief_id) <= pruned
            ):
                if apply:
                    await memory.delete(belief_id, existing_uow=uow)
                deleted.append(belief_id)
            else:
                unlinked.append(belief_id)
        return deleted, unlinked

    async def _source(
        self, uow: RepositoryUnitOfWork, principal: Principal, source_id: UUID, scan: _Scan
    ) -> PeopleSource | None:
        if source_id not in scan.sources:
            row = await uow.people.get(principal, source_id, ceiling=Sensitivity.RESTRICTED)
            scan.sources[source_id] = row if isinstance(row, PeopleSource) else None
        return scan.sources[source_id]

    async def _owner_stated(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        support_ids: Sequence[UUID],
        scan: _Scan,
    ) -> bool:
        for source_id in support_ids:
            source = await self._source(uow, principal, source_id, scan)
            if source is not None and source.source_kind == "owner":
                return True
        return False

    async def _owner_asserted(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        support_ids: Sequence[UUID],
        scan: _Scan,
    ) -> bool:
        """Whether an owner edit (create, rename, confirm, correct) supports the row."""
        for source_id in support_ids:
            if source_id not in scan.assertions:
                source = await self._source(uow, principal, source_id, scan)
                asserted = False
                if source is not None and source.source_kind == "owner":
                    try:
                        events = await uow.events.list_after(
                            source.session_id, source.event_sequence - 1, principal, limit=1
                        )
                    except NotFoundError:
                        events = []
                    asserted = any(
                        event.sequence == source.event_sequence
                        and event.event_type == "people.owner_assertion"
                        and event.actor_id == principal.principal_id
                        for event in events
                    )
                scan.assertions[source_id] = asserted
            if scan.assertions[source_id]:
                return True
        return False

    async def _explicit(
        self, uow: RepositoryUnitOfWork, principal: Principal, belief_id: UUID, scan: _Scan
    ) -> bool:
        """Whether the owner explicitly asked to remember this fact about the person.

        An explicit remember records its own formation run. Only the source
        session's hundred newest runs are read, so an older one is not found.
        """
        if belief_id not in scan.explicit:
            explicit = False
            try:
                belief = await uow.memories.get(belief_id, principal)
            except NotFoundError:
                belief = None
            if belief is not None and belief.authority in _OWNER_AUTHORITY:
                runs = await uow.memories.list_consolidations(
                    principal, session_id=belief.source_session_id, limit=100
                )
                explicit = any(
                    run.id == belief.formation_run_id and run.trigger == "explicit" for run in runs
                )
            scan.explicit[belief_id] = explicit
        return scan.explicit[belief_id]


async def _rows(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    kinds: list[str],
    person_id: UUID,
    *,
    limit: int | None = None,
) -> list[PeopleRecord]:
    """Current rows that reference one person, paged to the end or to `limit`."""
    query = PeopleQuery(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        kinds=kinds,
        person_id=person_id,
        sensitivity_ceiling=Sensitivity.RESTRICTED,
        limit=100,
    )
    found: list[PeopleRecord] = []
    while True:
        page = await uow.people.query(query)
        found.extend(row for row in page[:100] if row.id != person_id)
        if len(page) <= 100 or (limit is not None and len(found) >= limit):
            return found if limit is None else found[:limit]
        query = query.model_copy(update={"after": page[99].id})


async def _subjects(uow: RepositoryUnitOfWork, principal: Principal, belief_id: UUID) -> set[UUID]:
    """Every person a belief is about: its subject links and projection endpoints."""
    query = PeopleQuery(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        kinds=["memory_link", "relationship", "commitment"],
        belief_id=belief_id,
        sensitivity_ceiling=Sensitivity.RESTRICTED,
        limit=100,
    )
    people: set[UUID] = set()
    while True:
        page = await uow.people.query(query)
        for row in page[:100]:
            if isinstance(row, PersonMemoryLink) and row.role == "subject":
                people.add(row.person_id)
            elif isinstance(row, RelationshipAssertion):
                people.update(
                    end.id for end in (row.subject, row.object) if end.kind == "person" and end.id
                )
            elif isinstance(row, PeopleCommitment):
                people.update(
                    end.id
                    for end in (row.debtor, row.beneficiary)
                    if end.kind == "person" and end.id
                )
        if len(page) <= 100:
            return people
        query = query.model_copy(update={"after": page[99].id})


async def _fact_threads(
    uow: RepositoryUnitOfWork, principal: Principal
) -> dict[str, tuple[str, str]]:
    """The mail thread each retained fact came from, keyed by belief id."""
    threads: dict[str, tuple[str, str]] = {}
    after: str | None = None
    while True:
        page = await uow.email.list(principal, "semantic_source", after=after, limit=1000)
        for record in page:
            memory_ids = record.payload.get("memory_ids")
            thread = (
                str(record.payload.get("account_id")),
                str(record.payload.get("provider_thread_id")),
            )
            for belief_id in memory_ids if isinstance(memory_ids, list) else []:
                threads[str(belief_id)] = thread
        if len(page) < 1000:
            return threads
        after = page[-1].key


def _threads(sources: dict[str, tuple[str, str]], beliefs: set[UUID]) -> set[tuple[str, str]]:
    return {sources[str(belief)] for belief in beliefs if str(belief) in sources}


async def _unaliased(uow: RepositoryUnitOfWork, principal: Principal) -> list[Person]:
    """Active people without an owner-confirmed name alias."""
    query = PeopleQuery(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        kinds=["person"],
        states=["active"],
        sensitivity_ceiling=Sensitivity.RESTRICTED,
        limit=100,
    )
    found: list[Person] = []
    while True:
        page = await uow.people.query(query)
        for person in page[:100]:
            if not isinstance(person, Person):
                continue
            aliases = await _rows(uow, principal, ["identifier"], person.id)
            if not any(
                isinstance(row, PersonIdentifier)
                and row.identifier_kind == "name"
                and row.verification == "owner_confirmed"
                for row in aliases
            ):
                found.append(person)
        if len(page) <= 100:
            return found
        query = query.model_copy(update={"after": page[99].id})


async def _refuse_during_import(uow: RepositoryUnitOfWork, principal: Principal) -> None:
    query = PeopleQuery(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        kinds=["import_job"],
        sensitivity_ceiling=Sensitivity.RESTRICTED,
        limit=100,
    )
    while True:
        page = await uow.people.query(query)
        if any(isinstance(row, PeopleImportJob) and row.state in _RUNNING_IMPORTS for row in page):
            raise ConflictError(
                "A People import is running; let it finish or cancel it before repairing"
            )
        if len(page) <= 100:
            return
        query = query.model_copy(update={"after": page[99].id})
