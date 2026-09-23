"""Local, header-grounded email identities and observed exchange history."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import getaddresses
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailAccount
from agent_core.domain.email_semantics import EmailSemanticSource
from agent_core.domain.errors import ConflictError
from agent_core.domain.events import EventEnvelope
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import (
    InteractionParticipant,
    PeopleInteraction,
    PeopleQuery,
    PeopleSource,
    Person,
    PersonIdentifier,
    is_non_person_reference,
    normalize_identifier,
)
from agent_core.memory.people import resolve_identity
from agent_core.memory.people_formation import _Common, email_source_id
from agent_core.ports.persistence import RepositoryUnitOfWork

# The most unattached endpoints a first reply adopts into a person's history,
# and the most any later message from them adopts.
_ADOPTION_LIMIT = 256
_ADOPTION_STEP = 32

_ROLE_MAILBOX = re.compile(
    r"^(?:no[._-]?reply|support|info|hello|sales|help|billing|team|contact|admin|notifications?|news|newsletter|office|jobs|careers)(?:[+._-].*)?$",
    re.IGNORECASE,
)


def _addresses(value: object) -> list[tuple[str, str]]:
    values = value if isinstance(value, list) else [value]
    result = []
    for name, address in getaddresses([item for item in values if isinstance(item, str)]):
        try:
            result.append((name, normalize_identifier("email", "owner", address)))
        except ValueError:
            continue
    return result


def _exchange_identity(source: EmailSemanticSource, header: dict[str, object]) -> list[str]:
    message_id = header.get("message_id_header")
    body = header.get("body")
    if (
        isinstance(message_id, str)
        and re.fullmatch(r"<[^<>\s]+@[^<>\s]+>", message_id)
        and header.get("headers_complete") is True
        and header.get("body_complete") is True
        and isinstance(body, str)
    ):
        # A Message-ID alone is reusable/spoofable. Require the same delivered
        # envelope, original date, and complete body before combining copies.
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "id": message_id,
                    "sender": _addresses(source.sender),
                    "to": sorted(_addresses(header.get("to"))),
                    "cc": sorted(_addresses(header.get("cc"))),
                    "bcc": sorted(_addresses(header.get("bcc"))),
                    "subject": header.get("subject"),
                    "sent_at": source.sent_at.isoformat(),
                    "body": body,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return ["delivered-message@1", fingerprint]
    return [source.account_id, source.provider_thread_id, source.message_id]


def _interaction_id(principal: Principal, exchange: list[str]) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        json.dumps(
            [
                "people-email-interaction@1",
                principal.tenant_id,
                principal.principal_id,
                *exchange,
            ]
        ),
    )


@dataclass(frozen=True)
class _Correspondent:
    """What one address in one message may attach to (ADR-0118)."""

    person_id: UUID | None = None
    # No assignment of the address is live at or after the message, so a new
    # person holding it from then on cannot overlap an existing holder.
    creatable: bool = False
    # Exactly one person has ever held the address and the owner never ended
    # it, so unattached mail from the address is that person's.
    adoptable: bool = False
    # A copy written for a message inside an ended assignment ends with it.
    ends: datetime | None = None


def _sole_holder(held: list[PersonIdentifier], person_id: UUID) -> bool:
    return bool(held) and all(row.person_id == person_id and row.valid_to is None for row in held)


async def _correspondent(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    address: str,
    sent_at: datetime,
    now: datetime,
) -> _Correspondent:
    """The person an address belonged to when the mail was sent.

    The history walk reads newest mail first, so a person created from a reply
    holds the address only from that reply onward. Older mail attaches to the
    same person only when exactly one person has ever held the address and the
    owner never ended that assignment; otherwise it stays unattached rather
    than inventing a duplicate.
    """
    rows = await uow.people.query(
        PeopleQuery(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            kinds=["identifier"],
            identifier_value=address,
            assigned="attached",
            distinct_assignments=True,
            sensitivity_ceiling=Sensitivity.RESTRICTED,
            limit=100,
        )
    )
    truncated = len(rows) > 100
    held = [
        row
        for row in rows[:100]
        if isinstance(row, PersonIdentifier)
        and row.person_id is not None
        and row.identifier_kind == "email"
        and normalize_identifier("email", row.namespace, row.value) == address
    ]
    match = await resolve_identity(
        uow.people,
        principal,
        kind="email",
        namespace="owner",
        value=address,
        context="owner",
        at=sent_at,
        ceiling=Sensitivity.SENSITIVE,
    )
    if match.status == "ambiguous":
        return _Correspondent()
    if match.status == "matched":
        person_id = match.person_ids[0]
        ends = min(
            (
                row.valid_to
                for row in held
                if row.person_id == person_id
                and row.valid_to is not None
                and row.valid_to > sent_at
            ),
            default=None,
        )
        return _Correspondent(
            person_id, adoptable=not truncated and _sole_holder(held, person_id), ends=ends
        )
    if truncated:
        return _Correspondent()
    later = await resolve_identity(
        uow.people,
        principal,
        kind="email",
        namespace="owner",
        value=address,
        context="owner",
        at=now,
        ceiling=Sensitivity.SENSITIVE,
    )
    if later.status == "matched" and _sole_holder(held, later.person_ids[0]):
        return _Correspondent(later.person_ids[0], adoptable=True)
    if all(row.valid_to is not None and row.valid_to <= sent_at for row in held):
        return _Correspondent(creatable=True, adoptable=not held)
    return _Correspondent()


async def _adopt_received_history(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    person_id: UUID,
    address: str,
    now: datetime,
    common: _Common,
    *,
    limit: int,
) -> None:
    """Attach a person's unattached address endpoints and record that mail as received.

    Every unattached email endpoint is a sender's (outgoing recipients who are
    not added leave nothing behind), so each adopted endpoint is a message the
    owner received from this person before adding them. Each projection adopts
    a bounded number; later mail from the person adopts the rest.
    """
    after: UUID | None = None
    budget = limit
    while budget > 0:
        rows = await uow.people.query(
            PeopleQuery(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                kinds=["identifier"],
                identifier_value=address,
                assigned="unattached",
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                after=after,
                limit=100,
            )
        )
        for row in rows[:100]:
            if budget <= 0:
                return
            if (
                not isinstance(row, PersonIdentifier)
                or row.identifier_kind != "email"
                or row.namespace != "owner"
                or row.context != "owner"
                or normalize_identifier("email", "owner", row.value) != address
            ):
                continue
            sources = [
                source
                for source_id in row.support_ids
                if isinstance(
                    source := await uow.people.get(
                        principal, source_id, ceiling=Sensitivity.RESTRICTED
                    ),
                    PeopleSource,
                )
                and source.source_kind == "email"
                and not await uow.people.source_suppressed(principal, source.id)
            ]
            if len(sources) != len(row.support_ids):
                continue
            await uow.people.put(
                row.model_copy(
                    update={
                        "person_id": person_id,
                        "revision": row.revision + 1,
                        "updated_at": max(now, row.updated_at + timedelta(microseconds=1)),
                    }
                ),
                expected_revision=row.revision,
            )
            budget -= 1
            for source in sources:
                await _record_received(uow, principal, person_id, source, now, common)
        if len(rows) <= 100:
            return
        after = rows[99].id


async def _record_received(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    person_id: UUID,
    source: PeopleSource,
    now: datetime,
    common: _Common,
) -> None:
    exchange = (
        ["delivered-message@1", source.copy_group]
        if source.copy_group
        else [str(source.account_id), str(source.thread_id), str(source.message_id)]
    )
    interaction_id = _interaction_id(principal, exchange)
    if await uow.people.is_erased(principal, interaction_id):
        return
    current = await uow.people.get(principal, interaction_id, ceiling=Sensitivity.RESTRICTED)
    sender = InteractionParticipant(person_id=person_id, role="sender")
    if current is None:
        await uow.people.put(
            PeopleInteraction(
                id=interaction_id,
                channel="email",
                interaction_kind="exchange",
                attribution="observed",
                direction="incoming",
                summary="Received email",
                occurred_at=source.evidence_at,
                precision="instant",
                participants=[sender],
                support_ids=[source.id],
                **common,
            ),
            expected_revision=0,
        )
        return
    if not isinstance(current, PeopleInteraction):
        return
    participants = current.participants
    if (sender.person_id, sender.role) not in {(p.person_id, p.role) for p in participants}:
        participants = [*participants, sender]
    support_ids = list(dict.fromkeys([*current.support_ids, source.id]))[:256]
    if participants != current.participants or support_ids != current.support_ids:
        await uow.people.put(
            current.model_copy(
                update={
                    "participants": participants,
                    "support_ids": support_ids,
                    "revision": current.revision + 1,
                    "updated_at": max(now, current.updated_at + timedelta(microseconds=1)),
                }
            ),
            expected_revision=current.revision,
        )


async def project_correspondence(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    source: EmailSemanticSource,
    header: dict[str, object],
    event: EventEnvelope,
    now: datetime,
) -> None:
    """Called only after the semantic adapter verifies the exact immutable header."""
    account_row = await uow.email.get(principal, "account", source.account_id)
    if account_row is None:
        return
    account = EmailAccount.model_validate(account_row.payload)
    # The account's own address is all direction needs; a syncing or unavailable
    # account still belongs to the owner (ADR-0118).
    if not account.email_address:
        return
    # A copy delivered to the work account can have been sent from the owner's
    # verified personal address. Neither address is a third-party correspondent.
    accounts = await uow.email.list(principal, "account", limit=101)
    if len(accounts) > 100:
        raise ConflictError("People correspondence exceeds the bounded account window")
    owned: set[str] = set()
    for row in accounts:
        verified = EmailAccount.model_validate(row.payload)
        owned.update(
            address.casefold()
            for _, address in _addresses([verified.email_address, *verified.verified_addresses])
        )
    senders = _addresses(source.sender)
    if len(senders) != 1:
        return
    outgoing = senders[0][1].casefold() in owned
    # The owner's display name on their own mail; a recipient shown with the
    # same name is another of the owner's addresses, not someone they know.
    owner_name = " ".join(senders[0][0].casefold().split()) if outgoing else ""
    labels = header.get("label_ids")
    if isinstance(labels, list) and "DRAFT" in labels:
        return
    if outgoing and (not isinstance(labels, list) or "SENT" not in labels):
        return
    contacts = (
        (_addresses(header.get("to")) + _addresses(header.get("cc"))) if outgoing else senders
    )
    contacts = [(name, address) for name, address in contacts if address.casefold() not in owned][
        :32
    ]
    if not contacts:
        return
    sid = email_source_id(principal, source)
    if await uow.people.source_suppressed(principal, sid):
        raise ConflictError("People email source was erased")
    common: _Common = {
        "tenant_id": principal.tenant_id,
        "principal_id": principal.principal_id,
        "created_at": now,
        "updated_at": now,
        "sensitivity": Sensitivity.SENSITIVE,
    }
    exchange = _exchange_identity(source, header)
    copy_group = exchange[1] if exchange[0] == "delivered-message@1" else None
    current_source = await uow.people.get(principal, sid, ceiling=Sensitivity.RESTRICTED)
    if current_source is None:
        await uow.people.put(
            PeopleSource(
                id=sid,
                session_id=source.session_id,
                event_sequence=source.source_event_sequence,
                source_kind="email",
                evidence_at=source.sent_at,
                account_id=source.account_id,
                thread_id=source.provider_thread_id,
                message_id=source.message_id,
                source_revision=hashlib.sha256(event.model_dump_json().encode()).hexdigest(),
                copy_group=copy_group,
                **common,
            ),
            expected_revision=0,
        )
    elif isinstance(current_source, PeopleSource) and copy_group is not None:
        if current_source.copy_group not in {None, copy_group}:
            raise ConflictError("People email copy identity changed")
        if current_source.copy_group is None:
            await uow.people.put(
                current_source.model_copy(
                    update={
                        "copy_group": copy_group,
                        "revision": current_source.revision + 1,
                        "updated_at": max(
                            now, current_source.updated_at + timedelta(microseconds=1)
                        ),
                    }
                ),
                expected_revision=current_source.revision,
            )
    if isinstance(current_source, PeopleSource) and copy_group is None:
        copy_group = current_source.copy_group
    if copy_group is not None:
        exchange = ["delivered-message@1", copy_group]
    participants = []
    for name, address in contacts:
        if contains_secret_material(name) or contains_injection_pattern(name):
            continue
        found = await _correspondent(uow, principal, address, source.sent_at, now)
        person_id = found.person_id
        created = False
        if (
            person_id is None
            and found.creatable
            and outgoing
            and name
            and not _ROLE_MAILBOX.fullmatch(address.split("@", 1)[0])
            and not is_non_person_reference(name)
            and " ".join(name.casefold().split()) != owner_name
        ):
            # ADR-0118: the owner writing to someone is what adds them to People.
            # A header name alone never merges identities.
            candidate_id = uuid5(sid, "correspondent:" + address)
            if not await uow.people.is_erased(principal, candidate_id):
                person_id = candidate_id
                if (
                    await uow.people.get(principal, person_id, ceiling=Sensitivity.RESTRICTED)
                    is None
                ):
                    await uow.people.put(
                        Person(
                            id=person_id,
                            display_name=name[:200],
                            state="provisional",
                            support_ids=[sid],
                            **common,
                        ),
                        expected_revision=0,
                    )
                    created = True
        if person_id is None and outgoing:
            # An unknown recipient the owner is not adding leaves nothing behind,
            # so every unattached address endpoint is a sender's.
            continue
        alias_id = uuid5(sid, "email:" + address)
        if await uow.people.is_erased(principal, alias_id):
            # The repair or an erasure removed this row; skip it, never fail the source.
            pass
        elif await uow.people.get(principal, alias_id, ceiling=Sensitivity.RESTRICTED) is None:
            await uow.people.put(
                PersonIdentifier(
                    id=alias_id,
                    person_id=person_id,
                    identifier_kind="email",
                    namespace="owner",
                    value=address,
                    context="owner",
                    verification="channel_observed",
                    valid_from=source.sent_at,
                    valid_to=found.ends if person_id is not None else None,
                    support_ids=[sid],
                    **common,
                ),
                expected_revision=0,
            )
        if person_id is None:
            continue
        if found.adoptable:
            # Mail received before the owner first wrote to this person joins their history.
            await _adopt_received_history(
                uow,
                principal,
                person_id,
                address,
                now,
                common,
                limit=_ADOPTION_LIMIT if created else _ADOPTION_STEP,
            )
        if name:
            name_id = uuid5(sid, "name:" + address)
            if await uow.people.is_erased(principal, name_id):
                pass
            elif await uow.people.get(principal, name_id, ceiling=Sensitivity.RESTRICTED) is None:
                await uow.people.put(
                    PersonIdentifier(
                        id=name_id,
                        person_id=person_id,
                        identifier_kind="name",
                        namespace="owner",
                        value=name[:200],
                        context="email:" + hashlib.sha256(source.sender.encode()).hexdigest(),
                        verification="contextual",
                        valid_from=source.sent_at,
                        support_ids=[sid],
                        **common,
                    ),
                    expected_revision=0,
                )
        participants.append(
            InteractionParticipant(person_id=person_id, role="recipient" if outgoing else "sender")
        )
    participants = list({(item.person_id, item.role): item for item in participants}.values())
    if not participants:
        return
    interaction_id = _interaction_id(principal, exchange)
    if await uow.people.is_erased(principal, interaction_id):
        return
    current = await uow.people.get(principal, interaction_id, ceiling=Sensitivity.RESTRICTED)
    legacy_id = _interaction_id(principal, _exchange_identity(source, {}))
    legacy = (
        await uow.people.get(principal, legacy_id, ceiling=Sensitivity.RESTRICTED)
        if legacy_id != interaction_id
        else None
    )
    if isinstance(legacy, PeopleInteraction):
        if legacy.superseded_by not in {None, interaction_id}:
            raise ConflictError("People email history copy identity changed")
        # Preserve an owner-repaired assignment on the original partial receipt.
        participants = legacy.participants
        if isinstance(current, PeopleInteraction) and {
            (p.person_id, p.role) for p in current.participants
        } != {(p.person_id, p.role) for p in participants}:
            raise ConflictError("People email copies have conflicting identity assignments")
    support_ids = list(
        dict.fromkeys(
            [
                *(current.support_ids if isinstance(current, PeopleInteraction) else []),
                *(legacy.support_ids if isinstance(legacy, PeopleInteraction) else []),
                sid,
            ]
        )
    )
    if len(support_ids) > 256:
        raise ConflictError("People email copy evidence exceeds the bounded history window")
    if current is not None:
        if isinstance(current, PeopleInteraction) and support_ids != current.support_ids:
            await uow.people.put(
                current.model_copy(
                    update={
                        "revision": current.revision + 1,
                        "updated_at": max(now, current.updated_at + timedelta(microseconds=1)),
                        "support_ids": support_ids,
                    }
                ),
                expected_revision=current.revision,
            )
    else:
        label = "Sent email" if outgoing else "Received email"
        await uow.people.put(
            PeopleInteraction(
                id=interaction_id,
                channel="email",
                interaction_kind="exchange",
                attribution="observed",
                direction="outgoing" if outgoing else "incoming",
                summary=label,
                occurred_at=source.sent_at,
                precision="instant",
                participants=participants,
                support_ids=support_ids,
                **common,
            ),
            expected_revision=0,
        )
    if isinstance(legacy, PeopleInteraction) and legacy.superseded_by is None:
        await uow.people.put(
            legacy.model_copy(
                update={
                    "superseded_by": interaction_id,
                    "revision": legacy.revision + 1,
                    "updated_at": max(now, legacy.updated_at + timedelta(microseconds=1)),
                }
            ),
            expected_revision=legacy.revision,
        )
