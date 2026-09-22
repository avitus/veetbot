"""Local source-grounded People linking; atomic beliefs remain authoritative."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict
from uuid import UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.email_semantics import EmailSemanticSource
from agent_core.domain.errors import ConflictError, ToolValidationError
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.memory import (
    SENSITIVITY_ORDER,
    BeliefType,
    MemoryCandidate,
    MemoryDerivation,
    MemoryRecord,
    Polarity,
    Portability,
    Sensitivity,
)
from agent_core.domain.people import (
    InteractionParticipant,
    OrganizationReference,
    PeopleCommitment,
    PeopleEndpoint,
    PeopleInteraction,
    PeopleQuery,
    PeopleRecord,
    PeopleSource,
    Person,
    PersonIdentifier,
    PersonMemoryLink,
    PersonMention,
    RelationshipAssertion,
    normalize_identifier,
)
from agent_core.domain.people_extraction import InteractionEvidence, PeopleClaim
from agent_core.domain.people_sources import email_source_id as email_source_id
from agent_core.domain.people_sources import source_id as source_id
from agent_core.memory.communication_sources import FormationSource, FormationSourceKind
from agent_core.memory.people import resolve_identity
from agent_core.ports.people import PeopleStore

PEOPLE_FORMATION_VERSION = "formation@11"
PEOPLE_LINKER_VERSION = "people-linker@1"


class _Common(TypedDict):
    tenant_id: str
    principal_id: str
    created_at: datetime
    updated_at: datetime
    sensitivity: Sensitivity


class _Supported(_Common):
    support_ids: list[UUID]


class _Validation(_Supported):
    id: UUID
    belief_id: UUID


@dataclass(frozen=True)
class PreparedPeople:
    records: tuple[PeopleRecord, ...]
    people: dict[str, UUID | None]
    source_ids: tuple[UUID, ...]
    candidate: MemoryCandidate
    subject: str


def _admitted_source(source: FormationSource, email: EmailSemanticSource | None) -> bool:
    if email is None:
        return source.kind is FormationSourceKind.OWNER_ASSERTION
    return (
        source.kind is FormationSourceKind.ATTRIBUTED_COMMUNICATION
        and source.channel == "email"
        and source.event.session_id == email.session_id
        and source.event.sequence == email.source_event_sequence
        and source.text == email.body
    )


def validate_commitment_state(state: str, text: str) -> None:
    """Terminal states need affirmative delivery/cancellation evidence, never a draft."""
    if state not in {"completed", "cancelled"}:
        return
    normalized = text.casefold().replace("\u2019", "'")
    if re.search(r"\b(draft(?:ed)?|would|might|planning|unsent)\b", normalized):
        raise ToolValidationError("a draft or prospective statement cannot close a commitment")
    if re.search(
        r"\b(not|never|haven't|hasn't|didn't|wasn't|isn't)\b.{0,40}"
        r"\b(sent|delivered|completed|finished|paid|received|cancelled|canceled)\b",
        normalized,
    ):
        raise ToolValidationError("negated completion cannot close a commitment")
    affirmative = (
        r"\b(sent|delivered|completed|finished|paid|received|fulfilled)\b"
        if state == "completed"
        else r"\b(cancelled|canceled)\b|called off|no longer need"
    )
    if re.search(affirmative, normalized) is None:
        raise ToolValidationError("terminal commitment state lacks affirmative source evidence")


async def prepare_people(
    store: PeopleStore,
    principal: Principal,
    candidate: MemoryCandidate,
    sources: dict[int, FormationSource],
    now: datetime,
    *,
    email: EmailSemanticSource | None = None,
) -> PreparedPeople:
    proposal = candidate.people
    if proposal is None:
        raise ToolValidationError("People proposal is missing")
    if contains_secret_material(proposal.model_dump_json()) or contains_injection_pattern(
        proposal.model_dump_json()
    ):
        raise ToolValidationError("People proposal contains refused content")
    records: dict[UUID, PeopleRecord] = {}
    people: dict[str, UUID | None] = {}
    support: set[UUID] = set()
    for mention in proposal.mentions:
        source = sources.get(mention.source_event_id)
        if (
            source is None
            or not _admitted_source(source, email)
            or mention.source_event_id not in candidate.source_event_ids
            or source.text[mention.start : mention.end] != mention.text
            or contains_secret_material(mention.text)
            or contains_injection_pattern(mention.text)
        ):
            raise ToolValidationError("People proposal lacks admitted exact source evidence")
        sid = (
            email_source_id(principal, email)
            if email is not None
            else source_id(principal, source.event.session_id, source.event.sequence)
        )
        mid = uuid5(sid, f"mention:{mention.start}:{mention.end}")
        if await store.source_suppressed(principal, sid) or await store.is_erased(principal, mid):
            raise ConflictError("People source was erased")
        common: _Common = {
            "tenant_id": principal.tenant_id,
            "principal_id": principal.principal_id,
            "created_at": now,
            "updated_at": now,
            "sensitivity": max(
                (candidate.sensitivity_guess, Sensitivity.SENSITIVE),
                key=SENSITIVITY_ORDER.__getitem__,
            ),
        }
        support.add(sid)
        if await store.get(principal, sid, ceiling=Sensitivity.RESTRICTED) is None:
            records[sid] = PeopleSource(
                id=sid,
                session_id=source.event.session_id,
                event_sequence=source.event.sequence,
                source_kind="email" if email is not None else "owner",
                evidence_at=email.sent_at if email is not None else source.event.created_at,
                account_id=email.account_id if email else None,
                thread_id=email.provider_thread_id if email else None,
                message_id=email.message_id if email else None,
                source_revision=hashlib.sha256(source.event.model_dump_json().encode()).hexdigest(),
                **common,
            )
        existing = await store.get(principal, mid, ceiling=Sensitivity.RESTRICTED)
        if isinstance(existing, PersonMention):
            # Persisted owner repair takes precedence over a fresh extraction.
            people[mention.key] = existing.person_id
            continue
        if mention.referent_key is not None:
            anchors = [
                item
                for item in proposal.mentions
                if item.source_event_id == mention.source_event_id
                and item.end <= mention.start
                and item.referent_key is None
            ]
            person_id = (
                people.get(mention.referent_key)
                if len(anchors) == 1 and anchors[0].key == mention.referent_key
                else None
            )
        else:
            normalized = normalize_identifier(
                mention.identifier_kind, mention.namespace, mention.identifier_value
            )
            if mention.identifier_value.casefold() not in mention.text.casefold():
                raise ToolValidationError("People identity label is not supported by its source")
            display_name = mention.display_name
            if display_name.casefold() not in mention.text.casefold():
                if mention.identifier_kind != "role":
                    raise ToolValidationError(
                        "People identity label is not supported by its source"
                    )
                # An unnamed relative or role ("My brother") is displayed by its own
                # source span; a paraphrase such as "User's brother" never names it.
                display_name = mention.text
            # A contextual label the source does not state ("User's mother" for
            # "My mom") is dropped rather than refusing the whole mention.
            stated_context = (
                mention.context
                if mention.context and mention.context.casefold() in source.text.casefold()
                else ""
            )
            context = stated_context or (
                "email:" + hashlib.sha256(email.sender.encode()).hexdigest() if email else "owner"
            )
            resolved = await resolve_identity(
                store,
                principal,
                kind=mention.identifier_kind,
                namespace=mention.namespace,
                value=mention.identifier_value,
                context=context,
                at=email.sent_at if email is not None else source.event.created_at,
                ceiling=Sensitivity.RESTRICTED,
            )
            person_id = resolved.person_ids[0] if resolved.status == "matched" else None
            local = [
                r
                for r in records.values()
                if isinstance(r, PersonIdentifier)
                and r.identifier_kind == mention.identifier_kind
                and r.namespace == mention.namespace
                and r.context == context
                and normalize_identifier(r.identifier_kind, r.namespace, r.value) == normalized
            ]
            if len(local) == 1 and mention.identifier_kind != "role":
                person_id = local[0].person_id
            if resolved.status == "unresolved" and person_id is None:
                person_id = uuid5(sid, f"person:{mention.start}:{mention.end}")
                if await store.is_erased(principal, person_id):
                    raise ConflictError("People identity was erased")
                records[person_id] = Person(
                    id=person_id,
                    display_name=display_name,
                    state="provisional",
                    support_ids=[sid],
                    **common,
                )
            if person_id is not None:
                alias_id = uuid5(sid, f"identifier:{mention.start}:{mention.end}")
                if await store.is_erased(principal, alias_id):
                    raise ConflictError("People identity mapping was erased")
                records[alias_id] = PersonIdentifier(
                    id=alias_id,
                    person_id=person_id,
                    identifier_kind=mention.identifier_kind,
                    namespace=mention.namespace,
                    value=mention.identifier_value,
                    context=context,
                    verification="contextual",
                    valid_from=email.sent_at if email is not None else source.event.created_at,
                    support_ids=[sid],
                    **common,
                )
        people[mention.key] = person_id
        records[mid] = PersonMention(
            id=mid,
            source_id=sid,
            person_id=person_id,
            start=mention.start,
            end=mention.end,
            role=mention.role,
            support_ids=[sid],
            **common,
        )
    organization_keys = {organization.key for organization in proposal.organizations}
    for organization in proposal.organizations:
        source = sources.get(organization.source_event_id)
        if (
            source is None
            or not _admitted_source(source, email)
            or organization.source_event_id not in candidate.source_event_ids
            or source.text[organization.start : organization.end] != organization.text
        ):
            raise ToolValidationError("organization lacks admitted exact source evidence")
        sid = (
            email_source_id(principal, email)
            if email is not None
            else source_id(principal, source.event.session_id, source.event.sequence)
        )
        if await store.source_suppressed(principal, sid):
            raise ConflictError("People source was erased")
        support.add(sid)
        if await store.get(principal, sid, ceiling=Sensitivity.RESTRICTED) is None:
            records[sid] = PeopleSource(
                id=sid,
                session_id=source.event.session_id,
                event_sequence=source.event.sequence,
                source_kind="email" if email is not None else "owner",
                evidence_at=email.sent_at if email is not None else source.event.created_at,
                account_id=email.account_id if email else None,
                thread_id=email.provider_thread_id if email else None,
                message_id=email.message_id if email else None,
                source_revision=hashlib.sha256(source.event.model_dump_json().encode()).hexdigest(),
                **common,
            )
        candidates = await store.query(
            PeopleQuery(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                kinds=["organization"],
                text=organization.display_name,
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                limit=100,
            )
        )
        exact = [
            row
            for row in candidates
            if isinstance(row, OrganizationReference)
            and row.display_name.casefold() == organization.display_name.casefold()
        ]
        if len(candidates) > 100 or len(exact) > 1:
            people[organization.key] = None
        elif exact:
            people[organization.key] = exact[0].id
        else:
            identifier = uuid5(sid, f"organization:{organization.start}:{organization.end}")
            if await store.is_erased(principal, identifier):
                raise ConflictError("organization was erased")
            records[identifier] = OrganizationReference(
                id=identifier, display_name=organization.display_name, support_ids=[sid], **common
            )
            people[organization.key] = identifier
    subject_ids = sorted(
        {str(people[m.key]) for m in proposal.mentions if m.role == "subject" and people.get(m.key)}
    )
    identity_key = ",".join(subject_ids) or "unresolved:" + ",".join(
        str(key) for key in sorted(support)
    )
    # Stable identity augments the conflict key; a shared display name does not.
    subject = f"person:{identity_key}:{candidate.subject}"[:512]
    if all(value is not None for value in people.values()):

        def endpoint(key: str) -> PeopleEndpoint:
            return (
                PeopleEndpoint(kind="owner")
                if key == "owner"
                else PeopleEndpoint(
                    kind="organization" if key in organization_keys else "person", id=people[key]
                )
            )

        common_validation: _Validation = {
            "id": UUID(int=0),
            "tenant_id": principal.tenant_id,
            "principal_id": principal.principal_id,
            "created_at": now,
            "updated_at": now,
            "support_ids": list(support),
            "belief_id": UUID(int=0),
            "sensitivity": Sensitivity.SENSITIVE,
        }
        if proposal.relationship:
            relation = proposal.relationship
            RelationshipAssertion(
                subject=endpoint(relation.subject_key),
                object=endpoint(relation.object_key),
                predicate=relation.predicate,
                qualifier=relation.qualifier,
                valid_from=relation.valid_from,
                valid_to=relation.valid_to,
                precision=relation.precision,
                source_timezone=relation.source_timezone,
                **common_validation,
            )
        if proposal.commitment:
            commitment = proposal.commitment
            if commitment.source_event_id not in candidate.source_event_ids:
                raise ToolValidationError("commitment state requires its own admitted evidence")
            evidence = sources[commitment.source_event_id]
            validate_commitment_state(commitment.state, evidence.text)
            PeopleCommitment(
                debtor=endpoint(commitment.debtor_key),
                beneficiary=endpoint(commitment.beneficiary_key),
                description=candidate.statement,
                state=commitment.state,
                due_at=commitment.due_at,
                due_precision=commitment.due_precision,
                source_timezone=commitment.source_timezone,
                state_source_id=email_source_id(principal, email)
                if email is not None
                else source_id(principal, evidence.event.session_id, evidence.event.sequence),
                **common_validation,
            )
    return PreparedPeople(
        tuple(records.values()), people, tuple(sorted(support)), candidate, subject
    )


def direct_owner_kinship(prepared: PreparedPeople, belief: MemoryRecord) -> bool:
    proposal = prepared.candidate.people
    relationship = proposal.relationship if proposal else None
    return (
        belief.consolidation_policy_version == PEOPLE_FORMATION_VERSION
        and belief.derivation is MemoryDerivation.DIRECT
        and belief.polarity is Polarity.ASSERT
        and relationship is not None
        and relationship.predicate in {"parent", "child", "sibling", "relative"}
        and "owner" in {relationship.subject_key, relationship.object_key}
        and relationship.valid_to is None
        and all(value is not None for value in prepared.people.values())
    )


async def persist_people(
    store: PeopleStore,
    principal: Principal,
    prepared: PreparedPeople,
    belief: MemoryRecord,
    now: datetime,
) -> None:
    await persist_identity_evidence(store, principal, prepared)
    proposal = prepared.candidate.people
    assert proposal is not None
    common: _Supported = {
        "tenant_id": principal.tenant_id,
        "principal_id": principal.principal_id,
        "created_at": now,
        "updated_at": now,
        "sensitivity": belief.sensitivity,
        "support_ids": list(prepared.source_ids),
    }
    unresolved = any(value is None for value in prepared.people.values())
    for mention in proposal.mentions:
        person_id = prepared.people[mention.key]
        if person_id is None:
            continue
        link_id = uuid5(belief.id, f"{person_id}:{mention.role}")
        if await store.get(principal, link_id, ceiling=Sensitivity.RESTRICTED) is None:
            await store.put(
                PersonMemoryLink(
                    id=link_id,
                    person_id=person_id,
                    belief_id=belief.id,
                    role=mention.role,
                    unresolved=unresolved,
                    **common,
                ),
                expected_revision=0,
            )

    def endpoint(key: str) -> PeopleEndpoint:
        return (
            PeopleEndpoint(kind="owner")
            if key == "owner"
            else PeopleEndpoint(
                kind="organization"
                if key in {org.key for org in proposal.organizations}
                else "person",
                id=prepared.people[key],
            )
        )

    relationship = proposal.relationship
    if relationship is not None and not unresolved:
        rid = uuid5(belief.id, "relationship")
        if await store.get(principal, rid, ceiling=Sensitivity.RESTRICTED) is None:
            await store.put(
                RelationshipAssertion(
                    id=rid,
                    subject=endpoint(relationship.subject_key),
                    object=endpoint(relationship.object_key),
                    predicate=relationship.predicate,
                    qualifier=relationship.qualifier,
                    valid_from=relationship.valid_from,
                    valid_to=relationship.valid_to,
                    precision=relationship.precision,
                    source_timezone=relationship.source_timezone,
                    belief_id=belief.id,
                    **common,
                ),
                expected_revision=0,
            )
    commitment = proposal.commitment
    if commitment is not None and not unresolved:
        sid = next(
            (
                row.id
                for row in prepared.records
                if isinstance(row, PeopleSource)
                and row.event_sequence == commitment.source_event_id
            ),
            None,
        )
        if sid is None:
            sid = (
                prepared.source_ids[0]
                if len(prepared.source_ids) == 1
                else source_id(principal, belief.source_session_id, commitment.source_event_id)
            )
        cid = uuid5(belief.id, "commitment")
        if await store.get(principal, cid, ceiling=Sensitivity.RESTRICTED) is None:
            await store.put(
                PeopleCommitment(
                    id=cid,
                    debtor=endpoint(commitment.debtor_key),
                    beneficiary=endpoint(commitment.beneficiary_key),
                    description=belief.statement,
                    state=commitment.state,
                    belief_id=belief.id,
                    due_at=commitment.due_at,
                    due_precision=commitment.due_precision,
                    source_timezone=commitment.source_timezone,
                    state_source_id=sid,
                    **common,
                ),
                expected_revision=0,
            )


async def persist_identity_evidence(
    store: PeopleStore, principal: Principal, prepared: PreparedPeople
) -> None:
    # Sources precede identities, which precede their aliases and mentions.
    order = {"source": 0, "person": 1, "organization": 1, "identifier": 2, "mention": 3}
    for record in sorted(prepared.records, key=lambda row: order.get(row.kind, 4)):
        current = await store.get(principal, record.id, ceiling=Sensitivity.RESTRICTED)
        if current is None:
            await store.put(record, expected_revision=0)


async def persist_interaction(
    store: PeopleStore,
    principal: Principal,
    proposal: InteractionEvidence,
    sources: dict[int, FormationSource],
    now: datetime,
    scope: str,
) -> None:
    source = sources.get(proposal.source_event_id)
    if (
        source is None
        or source.kind is not FormationSourceKind.OWNER_ASSERTION
        or proposal.text not in source.text
    ):
        raise ToolValidationError("interaction requires admitted owner evidence")
    if contains_secret_material(proposal.model_dump_json()) or contains_injection_pattern(
        proposal.model_dump_json()
    ):
        raise ToolValidationError("interaction contains refused content")
    candidate = MemoryCandidate(
        belief_type=BeliefType.FACT,
        subject="interaction",
        statement=proposal.summary,
        source_event_ids=[proposal.source_event_id],
        model_confidence=0.65,
        proposed_scope=scope,
        proposed_portability=Portability.LOCAL,
        sensitivity_guess=Sensitivity.SENSITIVE,
        people=PeopleClaim(
            organizations=[], mentions=proposal.mentions, relationship=None, commitment=None
        ),
    )
    prepared = await prepare_people(store, principal, candidate, sources, now)
    participant_ids = {
        person for key in proposal.participant_keys if (person := prepared.people[key]) is not None
    }
    participants = [
        InteractionParticipant(person_id=person_id, role="participant")
        for person_id in sorted(participant_ids)
    ]
    if not participants:
        raise ToolValidationError("interaction participants remain unresolved")
    sid = source_id(principal, source.event.session_id, source.event.sequence)
    iid = uuid5(sid, "interaction:" + hashlib.sha256(proposal.text.encode()).hexdigest())
    interaction = PeopleInteraction(
        id=iid,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        created_at=now,
        updated_at=now,
        support_ids=list(prepared.source_ids),
        sensitivity=Sensitivity.SENSITIVE,
        channel="chat",
        interaction_kind=proposal.interaction_kind,
        attribution="owner_reported",
        direction="reported",
        summary=proposal.summary,
        occurred_at=proposal.occurred_at,
        precision=proposal.precision,
        source_timezone=proposal.source_timezone,
        participants=participants,
    )
    await persist_identity_evidence(store, principal, prepared)
    if await store.get(principal, iid, ceiling=Sensitivity.RESTRICTED) is None:
        await store.put(interaction, expected_revision=0)
