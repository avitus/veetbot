"""Local source-grounded People linking; atomic beliefs remain authoritative."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict
from uuid import UUID, uuid5

from agent_core.application.people_self import owner_references as owner_references
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
    is_non_person_reference,
    is_self_reference,
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
    # ADR-0121: a mention can be left without a person for two reasons. An
    # ambiguous one might be any of several people, so links from the same
    # claim are held back. A withheld one names someone outside People (a
    # stranger, a pronoun, the owner), which says nothing about the others.
    ambiguous: frozenset[str] = frozenset()
    withheld: frozenset[str] = frozenset()

    def resolves(self, key: str) -> bool:
        """Whether a claim endpoint names the owner or a resolved person or organization."""
        return key == "owner" or self.people.get(key) is not None


# First-person evidence that the owner took part in a reported meeting.
_FIRST_PERSON = re.compile(r"\b(?:i|me|we|us)\b", re.IGNORECASE)


def owner_tied_keys(claim: PeopleClaim) -> frozenset[str]:
    """Mention keys a claim ties to the owner: relationship or commitment endpoints.

    A referent mention ("she") carries its anchor along, so the named person is
    the one admitted.
    """
    tied: set[str] = set()
    for pair in (
        (claim.relationship.subject_key, claim.relationship.object_key)
        if claim.relationship
        else (),
        (claim.commitment.debtor_key, claim.commitment.beneficiary_key) if claim.commitment else (),
    ):
        if pair and "owner" in pair:
            tied.update(key for key in pair if key != "owner")
    by_key = {mention.key: mention for mention in claim.mentions}
    for key in list(tied):
        mention = by_key.get(key)
        if mention is not None and mention.referent_key:
            tied.add(mention.referent_key)
    return frozenset(key for key in tied if key in by_key)


def _label(kind: str, namespace: str, value: str) -> tuple[str, str] | None:
    try:
        return kind, normalize_identifier(kind, namespace, value)
    except ValueError:
        return None


def owner_tied_labels(
    claims: list[PeopleClaim], interactions: list[InteractionEvidence]
) -> frozenset[tuple[str, str]]:
    """Labels the owner tied to themself anywhere in one formation batch (ADR-0121).

    A person is admitted once, by any claim in the batch that ties them to the
    owner, so "Maya's partner is Jules" links Maya whether or not it is
    processed before "Maya is my sister".
    """
    labels: set[tuple[str, str]] = set()
    for claim in claims:
        tied = owner_tied_keys(claim)
        for mention in claim.mentions:
            if mention.key in tied and (
                label := _label(
                    mention.identifier_kind, mention.namespace, mention.identifier_value
                )
            ):
                labels.add(label)
    for interaction in interactions:
        if _FIRST_PERSON.search(interaction.text) is None:
            continue
        for mention in interaction.mentions:
            if mention.key in interaction.participant_keys and (
                label := _label(
                    mention.identifier_kind, mention.namespace, mention.identifier_value
                )
            ):
                labels.add(label)
    return frozenset(labels)


def creatable_keys(
    claim: PeopleClaim, tied_labels: frozenset[tuple[str, str]] = frozenset()
) -> frozenset[str]:
    """The mention keys of one claim that may create a person."""
    keys = set(owner_tied_keys(claim))
    for mention in claim.mentions:
        label = _label(mention.identifier_kind, mention.namespace, mention.identifier_value)
        if label is not None and label in tied_labels:
            keys.add(mention.key)
    return frozenset(keys)


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


def _source_cased(text: str, label: str) -> str | None:
    """Return `label` as the source span spells it, or None when the span lacks it.

    Case folding can change a string's length ("İ" folds to two code points,
    "ß" to "ss"), so folded positions are mapped back to source characters
    before the source is sliced.
    """
    folded_parts = [character.casefold() for character in text]
    folded_label = label.casefold()
    index = "".join(folded_parts).find(folded_label)
    if index < 0:
        return None
    offsets = [
        source_index for source_index, part in enumerate(folded_parts) for _ in range(len(part))
    ]
    end = index + len(folded_label)
    return text[offsets[index] : offsets[end - 1] + 1]


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
    creatable: frozenset[str] = frozenset(),
    self_references: frozenset[str] = frozenset(),
) -> PreparedPeople:
    """Ground a claim's mentions in People without inventing anyone (ADR-0121).

    Only keys in ``creatable`` may create a person; every other mention links to
    an existing person when resolution matches and otherwise stays a mention
    without one. Pronouns and the owner's own addresses and handles never create
    or match a person.
    """
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
    ambiguous: set[str] = set()
    withheld: set[str] = set()
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
            if existing.person_id is None:
                withheld.add(mention.key)
            continue
        if mention.referent_key is not None:
            anchors = [
                item
                for item in proposal.mentions
                if item.source_event_id == mention.source_event_id
                and item.end <= mention.start
                and item.referent_key is None
            ]
            unique_anchor = len(anchors) == 1 and anchors[0].key == mention.referent_key
            person_id = people.get(mention.referent_key) if unique_anchor else None
            if person_id is None:
                (withheld if unique_anchor and mention.referent_key in withheld else ambiguous).add(
                    mention.key
                )
        else:
            # Labels are matched to the span without regard to case, but only the
            # span's own characters are ever persisted.
            identifier_value = _source_cased(mention.text, mention.identifier_value)
            if identifier_value is None:
                raise ToolValidationError("People identity label is not supported by its source")
            display_name = _source_cased(mention.text, mention.display_name)
            if display_name is None:
                if mention.identifier_kind != "role":
                    raise ToolValidationError(
                        "People identity label is not supported by its source"
                    )
                # An unnamed relative or role ("My brother") is displayed by its own
                # source span; a paraphrase such as "User's brother" never names it.
                display_name = mention.text
            if (
                is_non_person_reference(mention.text)
                or is_non_person_reference(identifier_value)
                or is_self_reference(mention.identifier_kind, identifier_value, self_references)
            ):
                # A pronoun or the owner's own address is never a person of its own.
                people[mention.key] = None
                withheld.add(mention.key)
                records[mid] = PersonMention(
                    id=mid,
                    source_id=sid,
                    person_id=None,
                    start=mention.start,
                    end=mention.end,
                    role=mention.role,
                    support_ids=[sid],
                    **common,
                )
                continue
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
            normalized = normalize_identifier(
                mention.identifier_kind, mention.namespace, identifier_value
            )
            resolved = await resolve_identity(
                store,
                principal,
                kind=mention.identifier_kind,
                namespace=mention.namespace,
                value=identifier_value,
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
            if resolved.status == "ambiguous" and person_id is None:
                ambiguous.add(mention.key)
            if (
                resolved.status == "unresolved"
                and person_id is None
                and mention.key not in creatable
            ):
                # Someone the owner is not tied to stays a mention (ADR-0121).
                withheld.add(mention.key)
            elif resolved.status == "unresolved" and person_id is None:
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
                    value=identifier_value,
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
            ambiguous.add(organization.key)
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

    def resolves(key: str) -> bool:
        return key == "owner" or people.get(key) is not None

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
    relation_ready = proposal.relationship is not None and all(
        resolves(key)
        for key in (proposal.relationship.subject_key, proposal.relationship.object_key)
    )
    commitment_ready = proposal.commitment is not None and all(
        resolves(key)
        for key in (proposal.commitment.debtor_key, proposal.commitment.beneficiary_key)
    )
    if proposal.relationship and relation_ready:
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
        if commitment_ready:
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
        tuple(records.values()),
        people,
        tuple(sorted(support)),
        candidate,
        subject,
        ambiguous=frozenset(ambiguous),
        withheld=frozenset(withheld),
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
        and prepared.resolves(relationship.subject_key)
        and prepared.resolves(relationship.object_key)
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
    # Only ambiguity puts a claim's links in doubt; a withheld stranger does not.
    unresolved = bool(prepared.ambiguous)
    for mention in proposal.mentions:
        person_id = prepared.people[mention.key]
        if person_id is None:
            continue
        link_id = uuid5(belief.id, f"{person_id}:{mention.role}")
        # The directory repair may have erased this derived row; a belief formed
        # again must neither revive nor fail on it.
        if await store.is_erased(principal, link_id):
            continue
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
    if (
        relationship is not None
        and prepared.resolves(relationship.subject_key)
        and prepared.resolves(relationship.object_key)
        and not {relationship.subject_key, relationship.object_key} & prepared.ambiguous
    ):
        rid = uuid5(belief.id, "relationship")
        if await store.is_erased(principal, rid):
            pass
        elif await store.get(principal, rid, ceiling=Sensitivity.RESTRICTED) is None:
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
    if (
        commitment is not None
        and prepared.resolves(commitment.debtor_key)
        and prepared.resolves(commitment.beneficiary_key)
        and not {commitment.debtor_key, commitment.beneficiary_key} & prepared.ambiguous
    ):
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
        if await store.is_erased(principal, cid):
            pass
        elif await store.get(principal, cid, ceiling=Sensitivity.RESTRICTED) is None:
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
    *,
    self_references: frozenset[str] = frozenset(),
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
    # A reported meeting admits its participants only when the owner took part
    # (ADR-0121): "I met Maya" adds Maya, "Maya met Jules" adds no one.
    creatable = (
        frozenset(proposal.participant_keys)
        if _FIRST_PERSON.search(proposal.text) is not None
        else frozenset()
    )
    prepared = await prepare_people(
        store,
        principal,
        candidate,
        sources,
        now,
        creatable=creatable,
        self_references=self_references,
    )
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
    if await store.is_erased(principal, iid):
        return
    if await store.get(principal, iid, ceiling=Sensitivity.RESTRICTED) is None:
        await store.put(interaction, expected_revision=0)
