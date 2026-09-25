"""Automatic People merges and merge suggestions (ADR-0125).

A pass over the directory looks for identities that are the same person.

- **Decisive evidence merges without asking.** An address, number or handle
  the owner gave one person is also held, as an observed endpoint, by a
  provisional person created from correspondence. The owner has said whose
  it is, so the correspondent is that person.
- **Anything less asks the owner.** Agreeing names, or an address shared
  only by observed correspondents, become a merge suggestion the owner
  confirms or dismisses. Two people can share a name, so a name alone never
  merges anyone (gate P01).
- **The owner's no is final.** A dismissed suggestion, an undone merge, or a
  split keeps that pair apart for good.

Merges run through the revision-checked identity service, so an automatic
merge can be undone like any other.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_core.application.authorization import require_scope
from agent_core.application.people_identity import PeopleIdentityService
from agent_core.application.people_self import owner_references
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError, ToolValidationError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import SENSITIVITY_ORDER, Sensitivity
from agent_core.domain.people import (
    PeopleInteraction,
    PeopleMergeSuggestion,
    PeopleOperation,
    PeopleQuery,
    PeopleRecord,
    PeopleSource,
    Person,
    PersonIdentifier,
    is_group_or_service_name,
    is_non_person_reference,
    is_role_mailbox,
    normalize_identifier,
)
from agent_core.domain.people_duplicates import (
    NameReason,
    Standing,
    family_surnames,
    merge_direction,
    name_match,
    name_tokens,
    shares_family_name,
)
from agent_core.domain.people_views import (
    PeopleDedupeReport,
    PeopleMergeEntry,
    ResolveMergeSuggestion,
)
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

SUGGESTION_VERSION = "people-merge-suggestion@1"
_ENDPOINT_KINDS = frozenset({"email", "phone", "handle"})
# History beyond this many interactions does not change which identity survives.
_HISTORY_CAP = 500

Pair = frozenset[UUID]


@dataclass
class _Directory:
    """One consistent read of everything the pass decides on."""

    people: dict[UUID, Person]
    names: dict[UUID, list[tuple[str, ...]]]
    holders: dict[tuple[str, str], dict[UUID, set[str]]]
    suggestions: dict[Pair, PeopleMergeSuggestion]
    distinct: set[Pair]
    references: frozenset[str]
    surnames: frozenset[str]
    standings: dict[UUID, Standing] = field(default_factory=dict)


@dataclass(frozen=True)
class _Proposal:
    source_id: UUID
    target_id: UUID
    reason: str
    family_name: bool


class PeopleDeduplicator:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, clock: Clock, identity: PeopleIdentityService
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._identity = identity

    async def run(self, principal: Principal, *, apply: bool) -> PeopleDedupeReport:
        """One pass: merge decisive duplicates, then ask about the rest."""
        require_scope(principal, "people.write")
        async with self._uow_factory() as uow:
            directory = await self._read(uow, principal)
            merges = await self._decisive(uow, principal, directory)
            proposals = await self._suggestible(uow, principal, directory, merges)
        if not apply:
            return PeopleDedupeReport(
                applied=False,
                merges=[_entry(directory, row) for row in merges],
                suggestions=[_entry(directory, row) for row in proposals],
                withdrawn=0,
            )
        applied: list[PeopleMergeEntry] = []
        for merge in merges:
            if await self._merge_automatically(principal, merge):
                applied.append(_entry(directory, merge))
        # Suggestions are decided on the directory the merges left behind.
        async with self._uow_factory() as uow, uow.people.lock(principal):
            directory = await self._read(uow, principal)
            proposals = await self._suggestible(uow, principal, directory, [])
            withdrawn = await self._record(uow, principal, directory, proposals)
        return PeopleDedupeReport(
            applied=True,
            merges=applied,
            suggestions=[_entry(directory, row) for row in proposals],
            withdrawn=withdrawn,
        )

    async def resolve(
        self,
        principal: Principal,
        suggestion_id: UUID,
        request: ResolveMergeSuggestion,
        *,
        key: str,
        ceiling: Sensitivity,
    ) -> PeopleMergeSuggestion:
        """Apply the owner's answer: merge the pair, or keep them apart for good."""
        require_scope(principal, "people.write")
        if not key or len(key) > 200:
            raise ToolValidationError("People idempotency key is invalid")
        digest = hashlib.sha256(
            json.dumps(
                [str(suggestion_id), request.model_dump(mode="json")], sort_keys=True
            ).encode()
        ).hexdigest()
        receipt_key = (
            "people-merge-suggestion:"
            + hashlib.sha256(
                json.dumps([principal.tenant_id, principal.principal_id, key]).encode()
            ).hexdigest()
        )
        async with self._uow_factory() as uow, uow.people.lock(principal):
            await uow.sessions.get(request.session_id, principal)
            previous = await uow.events.get_by_derivation(receipt_key, principal)
            if previous is not None:
                if previous.payload.get("request_hash") != digest:
                    raise ConflictError("People idempotency key was reused")
                replayed = await uow.people.get(
                    principal,
                    suggestion_id,
                    ceiling=ceiling,
                    at_revision=int(previous.payload["revision"]),
                )
                if not isinstance(replayed, PeopleMergeSuggestion):
                    raise NotFoundError("merge suggestion not found")
                return replayed
            current = await uow.people.get(principal, suggestion_id, ceiling=ceiling)
            if not isinstance(current, PeopleMergeSuggestion):
                raise NotFoundError("merge suggestion not found")
            if current.revision != request.expected_revision or current.state != "open":
                raise ConflictError("merge suggestion changed")
            operation_id = None
            if request.decision == "merge":
                people = [
                    await uow.people.get(principal, person_id, ceiling=ceiling)
                    for person_id in (current.source_id, current.target_id)
                ]
                if not all(isinstance(p, Person) and p.state != "merged" for p in people):
                    raise ConflictError("merge suggestion changed")
                preview = await self._identity.preview_merge(
                    principal,
                    current.source_id,
                    current.target_id,
                    expected_revisions={p.id: p.revision for p in people if isinstance(p, Person)},
                    ceiling=ceiling,
                    existing_uow=uow,
                )
                operation_id = (
                    await self._identity.apply(
                        principal, preview.id, ceiling=ceiling, existing_uow=uow
                    )
                ).id
            decided = current.model_copy(
                update={
                    "state": "merged" if request.decision == "merge" else "separated",
                    "operation_id": operation_id,
                    "revision": current.revision + 1,
                    "updated_at": max(
                        self._clock.now(), current.updated_at + timedelta(microseconds=1)
                    ),
                }
            )
            await uow.people.put(decided, expected_revision=current.revision)
            await uow.events.append(
                NewEvent(
                    session_id=request.session_id,
                    run_id=None,
                    event_type="people.merge_suggestion_resolved",
                    actor_type="user",
                    actor_id=principal.principal_id,
                    derivation_key=receipt_key,
                    payload={
                        "request_hash": digest,
                        "suggestion_id": str(suggestion_id),
                        "revision": decided.revision,
                        "decision": request.decision,
                    },
                )
            )
            return decided

    async def _read(self, uow: RepositoryUnitOfWork, principal: Principal) -> _Directory:
        people = {
            row.id: row
            for row in await _all(uow, principal, ["person"], states=["active", "provisional"])
            if isinstance(row, Person)
        }
        names: dict[UUID, list[tuple[str, ...]]] = defaultdict(list)
        for person in people.values():
            if not (
                is_non_person_reference(person.display_name)
                or is_group_or_service_name(person.display_name)
            ):
                names[person.id].append(name_tokens(person.display_name))
        holders: dict[tuple[str, str], dict[UUID, set[str]]] = defaultdict(lambda: defaultdict(set))
        identifiers = await _all(
            uow,
            principal,
            ["identifier"],
            assigned="attached",
            valid_at=True,
            now=self._clock.now(),
        )
        for row in identifiers:
            if not isinstance(row, PersonIdentifier) or row.person_id not in people:
                continue
            if row.identifier_kind == "name":
                if not is_group_or_service_name(row.value):
                    names[row.person_id].append(name_tokens(row.value))
            elif row.identifier_kind in _ENDPOINT_KINDS:
                try:
                    value = normalize_identifier(row.identifier_kind, row.namespace, row.value)
                except ValueError:
                    continue
                evidence = (
                    "owner"
                    if row.verification == "owner_confirmed"
                    or (row.verification == "contextual" and row.context == "owner")
                    else "observed"
                )
                holders[(row.identifier_kind, value.casefold())][row.person_id].add(evidence)
        suggestions: dict[Pair, PeopleMergeSuggestion] = {}
        for row in await _all(uow, principal, ["merge_suggestion"]):
            if isinstance(row, PeopleMergeSuggestion):
                suggestions[frozenset((row.source_id, row.target_id))] = row
        distinct = {pair for pair, row in suggestions.items() if row.state == "separated"}
        # An undone merge or a split is the owner saying these are two people.
        for row in await _all(uow, principal, ["operation"]):
            if (
                isinstance(row, PeopleOperation)
                and row.state == "completed"
                and row.operation in {"undo", "split"}
                and len(set(row.person_ids)) == 2
            ):
                distinct.add(frozenset(row.person_ids))
        references = await owner_references(uow.email, principal)
        return _Directory(
            people=people,
            names={key: [n for n in value if n] for key, value in names.items()},
            holders=holders,
            suggestions=suggestions,
            distinct=distinct,
            references=references,
            surnames=family_surnames(references),
        )

    async def _decisive(
        self, uow: RepositoryUnitOfWork, principal: Principal, directory: _Directory
    ) -> list[_Proposal]:
        """Correspondents holding an endpoint the owner gave someone else."""
        merges: dict[UUID, _Proposal] = {}
        for (kind, value), held in sorted(directory.holders.items()):
            if len(held) < 2 or f"{kind}:{value}" in directory.references:
                continue
            if kind == "email" and is_role_mailbox(value):
                continue
            assigned = [pid for pid, evidence in held.items() if "owner" in evidence]
            observed = sorted(pid for pid in held if pid not in assigned)
            if len(assigned) != 1 or not observed:
                # Several owner-given holders share the endpoint on purpose.
                continue
            target = assigned[0]
            for source in observed:
                person = directory.people[source]
                if (
                    person.state != "provisional"
                    or person.pinned
                    or frozenset((source, target)) in directory.distinct
                    or source in merges
                ):
                    continue
                merges[source] = _Proposal(source, target, "same_address", False)
        return list(merges.values())

    async def _suggestible(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        directory: _Directory,
        merges: list[_Proposal],
    ) -> list[_Proposal]:
        """Pairs worth asking the owner about, strongest direction first."""
        merging = {row.source_id for row in merges}
        live = sorted(
            pid for pid in directory.people if pid not in merging and directory.names.get(pid)
        )
        found: dict[Pair, tuple[str, bool]] = {}
        weak: dict[UUID, set[UUID]] = defaultdict(set)
        for index, first in enumerate(live):
            for second in live[index + 1 :]:
                pair = frozenset((first, second))
                if pair in directory.distinct:
                    continue
                agreement: NameReason | None = name_match(
                    directory.names[first], directory.names[second]
                )
                if agreement is None:
                    continue
                family = any(
                    shares_family_name(tokens, directory.surnames)
                    for person in (first, second)
                    for tokens in directory.names[person]
                )
                found[pair] = (agreement, family)
                if agreement != "same_name":
                    weak[first].add(second)
                    weak[second].add(first)
        # An address only correspondents share asks too; an owner-given one merged above.
        for (kind, value), held in directory.holders.items():
            observed = sorted(pid for pid, evidence in held.items() if "owner" not in evidence)
            if len(observed) < 2 or len(observed) != len(held):
                continue
            if f"{kind}:{value}" in directory.references or (
                kind == "email" and is_role_mailbox(value)
            ):
                continue
            for index, first in enumerate(observed):
                for second in observed[index + 1 :]:
                    pair = frozenset((first, second))
                    if pair not in directory.distinct and not {first, second} & merging:
                        found[pair] = ("same_address", False)
        proposals: list[_Proposal] = []
        for pair, (reason, family) in sorted(found.items(), key=lambda item: sorted(item[0])):
            first, second = sorted(pair)
            if reason in {"first_name", "nickname"} and not _unambiguous(
                first, second, weak, found
            ):
                continue
            source, target = merge_direction(
                await self._standing(uow, principal, directory, first),
                await self._standing(uow, principal, directory, second),
            )
            proposals.append(_Proposal(source, target, reason, family))
        return proposals

    async def _standing(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        directory: _Directory,
        person_id: UUID,
    ) -> Standing:
        if person_id not in directory.standings:
            person = directory.people[person_id]
            owner_stated = False
            for source_id in person.support_ids:
                source = await uow.people.get(principal, source_id, ceiling=Sensitivity.RESTRICTED)
                owner_stated = owner_stated or (
                    isinstance(source, PeopleSource) and source.source_kind == "owner"
                )
            history = sum(
                1
                for row in await _all(
                    uow, principal, ["interaction"], person_id=person_id, cap=_HISTORY_CAP
                )
                if isinstance(row, PeopleInteraction)
            )
            directory.standings[person_id] = Standing(
                person_id=person.id,
                state=person.state,
                pinned=person.pinned,
                owner_stated=owner_stated,
                history=history,
                created_at=person.created_at,
            )
        return directory.standings[person_id]

    async def _merge_automatically(self, principal: Principal, merge: _Proposal) -> bool:
        """One decisive merge in its own transaction; a change since planning skips it."""
        try:
            async with self._uow_factory() as uow, uow.people.lock(principal):
                people = [
                    await uow.people.get(principal, person_id, ceiling=Sensitivity.RESTRICTED)
                    for person_id in (merge.source_id, merge.target_id)
                ]
                if not all(isinstance(p, Person) and p.state != "merged" for p in people):
                    return False
                preview = await self._identity.preview_merge(
                    principal,
                    merge.source_id,
                    merge.target_id,
                    expected_revisions={p.id: p.revision for p in people if isinstance(p, Person)},
                    ceiling=Sensitivity.RESTRICTED,
                    existing_uow=uow,
                    automatic=True,
                )
                await self._identity.apply(
                    principal, preview.id, ceiling=Sensitivity.RESTRICTED, existing_uow=uow
                )
                return True
        except (ConflictError, NotFoundError, ToolValidationError):
            return False

    async def _record(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        directory: _Directory,
        proposals: list[_Proposal],
    ) -> int:
        """Open or refresh a suggestion per proposal; withdraw the ones that stopped matching."""
        now = self._clock.now()
        wanted = {frozenset((row.source_id, row.target_id)): row for row in proposals}
        for pair, proposal in wanted.items():
            current = directory.suggestions.get(pair)
            fields = {
                "source_id": proposal.source_id,
                "target_id": proposal.target_id,
                "reason": proposal.reason,
                "family_name": proposal.family_name,
                "state": "open",
            }
            sensitivity = max(
                (directory.people[key].sensitivity for key in pair),
                key=SENSITIVITY_ORDER.__getitem__,
            )
            if current is None:
                await uow.people.put(
                    PeopleMergeSuggestion(
                        id=_suggestion_id(principal, pair),
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        created_at=now,
                        updated_at=now,
                        sensitivity=sensitivity,
                        **fields,  # type: ignore[arg-type]
                    ),
                    expected_revision=0,
                )
            elif current.state in {"open", "withdrawn"} and any(
                getattr(current, name) != value for name, value in fields.items()
            ):
                await uow.people.put(
                    current.model_copy(
                        update={
                            **fields,
                            "sensitivity": sensitivity,
                            "revision": current.revision + 1,
                            "updated_at": max(now, current.updated_at + timedelta(microseconds=1)),
                        }
                    ),
                    expected_revision=current.revision,
                )
        withdrawn = 0
        for pair, current in directory.suggestions.items():
            if current.state == "open" and pair not in wanted:
                await uow.people.put(
                    current.model_copy(
                        update={
                            "state": "withdrawn",
                            "revision": current.revision + 1,
                            "updated_at": max(now, current.updated_at + timedelta(microseconds=1)),
                        }
                    ),
                    expected_revision=current.revision,
                )
                withdrawn += 1
        return withdrawn


def _unambiguous(
    first: UUID,
    second: UUID,
    weak: dict[UUID, set[UUID]],
    found: dict[Pair, tuple[str, bool]],
) -> bool:
    """A first-name or nickname match counts only when it points one way.

    When a name matches several people, the one sharing the owner's family
    name still counts if it is the only such match on both sides.
    """
    if len(weak[first]) == 1 and len(weak[second]) == 1:
        return True

    def family_partners(person: UUID) -> set[UUID]:
        return {other for other in weak[person] if found[frozenset((person, other))][1]}

    return (
        found[frozenset((first, second))][1]
        and family_partners(first) == {second}
        and family_partners(second) == {first}
    )


def _suggestion_id(principal: Principal, pair: Pair) -> UUID:
    first, second = sorted(str(key) for key in pair)
    return uuid5(
        NAMESPACE_URL,
        f"{SUGGESTION_VERSION}:{principal.tenant_id}:{principal.principal_id}:{first}:{second}",
    )


def _entry(directory: _Directory, row: _Proposal) -> PeopleMergeEntry:
    return PeopleMergeEntry(
        source_id=row.source_id,
        target_id=row.target_id,
        source_name=directory.people[row.source_id].display_name,
        target_name=directory.people[row.target_id].display_name,
        reason=row.reason,  # type: ignore[arg-type]
        family_name=row.family_name,
    )


async def _all(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    kinds: list[str],
    *,
    states: list[str] | None = None,
    assigned: str = "any",
    valid_at: bool = False,
    now: object = None,
    person_id: UUID | None = None,
    cap: int | None = None,
) -> list[PeopleRecord]:
    """Every current row of the given kinds, paged; `cap` stops early."""
    query = PeopleQuery.model_validate(
        {
            "tenant_id": principal.tenant_id,
            "principal_id": principal.principal_id,
            "kinds": kinds,
            "states": states,
            "assigned": assigned,
            "valid_at": now if valid_at else None,
            "distinct_assignments": kinds == ["identifier"],
            "person_id": person_id,
            "sensitivity_ceiling": Sensitivity.RESTRICTED,
            "limit": 100,
        }
    )
    found: list[PeopleRecord] = []
    while True:
        page = await uow.people.query(query)
        found.extend(page[:100])
        if len(page) <= 100 or (cap is not None and len(found) >= cap):
            return found
        query = query.model_copy(update={"after": page[99].id})
