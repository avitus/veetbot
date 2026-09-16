"""Local, header-grounded email identities and observed exchange history."""

from __future__ import annotations

import hashlib
import json
import re
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
    PeopleSource,
    Person,
    PersonIdentifier,
    normalize_identifier,
)
from agent_core.memory.people import resolve_identity
from agent_core.memory.people_formation import _Common, email_source_id
from agent_core.ports.persistence import RepositoryUnitOfWork

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
    if account.status != "ready" or not account.email_address:
        return
    # A copy delivered to the work account can have been sent from the owner's
    # verified personal address. Neither address is a third-party correspondent.
    accounts = await uow.email.list(principal, "account", limit=101)
    if len(accounts) > 100:
        raise ConflictError("People correspondence exceeds the bounded account window")
    owned: set[str] = set()
    for row in accounts:
        verified = EmailAccount.model_validate(row.payload)
        if verified.status == "ready":
            owned.update(
                address
                for _, address in _addresses([verified.email_address, *verified.verified_addresses])
            )
    senders = _addresses(source.sender)
    if len(senders) != 1:
        return
    outgoing = senders[0][1] in owned
    labels = header.get("label_ids")
    if isinstance(labels, list) and "DRAFT" in labels:
        return
    if outgoing and (not isinstance(labels, list) or "SENT" not in labels):
        return
    contacts = (
        (_addresses(header.get("to")) + _addresses(header.get("cc"))) if outgoing else senders
    )
    contacts = [(name, address) for name, address in contacts if address not in owned][:32]
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
        match = await resolve_identity(
            uow.people,
            principal,
            kind="email",
            namespace="owner",
            value=address,
            context="owner",
            at=source.sent_at,
            ceiling=Sensitivity.SENSITIVE,
        )
        person_id = match.person_ids[0] if match.status == "matched" else None
        if (
            match.status == "unresolved"
            and name
            and not _ROLE_MAILBOX.fullmatch(address.split("@", 1)[0])
        ):
            # A new address may seed a provisional person; a header name never merges identities.
            person_id = uuid5(sid, "correspondent:" + address)
            if await uow.people.is_erased(principal, person_id):
                raise ConflictError("People correspondent was erased")
            if await uow.people.get(principal, person_id, ceiling=Sensitivity.RESTRICTED) is None:
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
        if person_id is not None:
            person = await uow.people.get(principal, person_id, ceiling=Sensitivity.RESTRICTED)
            if (
                isinstance(person, Person)
                and person.state == "provisional"
                and person.display_name == name
                and sid not in person.support_ids
                and len(person.support_ids) < 256
            ):
                await uow.people.put(
                    person.model_copy(
                        update={
                            "revision": person.revision + 1,
                            "updated_at": max(now, person.updated_at + timedelta(microseconds=1)),
                            "support_ids": [*person.support_ids, sid],
                        }
                    ),
                    expected_revision=person.revision,
                )
        alias_id = uuid5(sid, "email:" + address)
        if await uow.people.is_erased(principal, alias_id):
            raise ConflictError("People contact was erased")
        if await uow.people.get(principal, alias_id, ceiling=Sensitivity.RESTRICTED) is None:
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
                    support_ids=[sid],
                    **common,
                ),
                expected_revision=0,
            )
        if person_id is None:
            continue
        if name:
            name_id = uuid5(sid, "name:" + address)
            if await uow.people.get(principal, name_id, ceiling=Sensitivity.RESTRICTED) is None:
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
        raise ConflictError("People email history was erased")
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
