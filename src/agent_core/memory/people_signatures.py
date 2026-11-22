"""A known sender's own signature as display-only contact details (ADR-0336).

No model is called. The rows are the sender's claim about themselves: they live
in a reserved namespace that identity resolution, the duplicate pass and Chat
context all ignore, so a shared office number can never identify or merge anyone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.email_semantics import EmailSemanticSource
from agent_core.domain.email_signature import SIGNATURE_CONTEXT, SIGNATURE_NAMESPACE, sender_contact
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import (
    PeopleQuery,
    PeopleSource,
    PersonIdentifier,
    normalize_identifier,
)
from agent_core.memory.people_formation import _Common
from agent_core.ports.persistence import RepositoryUnitOfWork

# The most pages of one detail's copies, or of one person's distinct aliases,
# read to place a message among them. Past it the message adds nothing: the
# detail is already recorded that many times.
_COPY_PAGES = 20


def _copy_id(source_id: UUID, kind: str, key: str) -> UUID:
    return uuid5(source_id, f"signature:{kind}:{key}")


async def _ever_stated(
    uow: RepositoryUnitOfWork, principal: Principal, person_id: UUID
) -> dict[str, set[str]] | None:
    """Every title and number this person's signatures have stated, or None past the bound.

    Ended details are included: a message read late is placed among them too.
    """
    keys: dict[str, set[str]] = {"role": set(), "phone": set()}
    after: UUID | None = None
    for _ in range(_COPY_PAGES):
        page = await uow.people.query(
            PeopleQuery(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                person_id=person_id,
                kinds=["identifier"],
                assigned="attached",
                distinct_assignments=True,
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                after=after,
                limit=100,
            )
        )
        for row in page[:100]:
            if (
                isinstance(row, PersonIdentifier)
                and row.person_id == person_id
                and row.namespace == SIGNATURE_NAMESPACE
                and row.identifier_kind in keys
            ):
                keys[row.identifier_kind].add(
                    normalize_identifier(row.identifier_kind, SIGNATURE_NAMESPACE, row.value)
                )
        if len(page) <= 100:
            return keys
        after = page[99].id
    return None


async def _copies(
    uow: RepositoryUnitOfWork, principal: Principal, person_id: UUID, kind: str, key: str
) -> list[PersonIdentifier] | None:
    """Every copy of one stated detail of this person, or None past the bound."""
    copies: list[PersonIdentifier] = []
    after: UUID | None = None
    for _ in range(_COPY_PAGES):
        page = await uow.people.query(
            PeopleQuery(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                person_id=person_id,
                kinds=["identifier"],
                identifier_value=key,
                assigned="attached",
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                after=after,
                limit=100,
            )
        )
        copies += [
            row
            for row in page[:100]
            if isinstance(row, PersonIdentifier)
            and row.person_id == person_id
            and row.identifier_kind == kind
            and row.namespace == SIGNATURE_NAMESPACE
        ]
        if len(page) <= 100:
            return copies
        after = page[99].id
    return None


async def _owner_ended(
    uow: RepositoryUnitOfWork, principal: Principal, copy: PersonIdentifier, kind: str, key: str
) -> bool:
    """Whether the owner ended this copy: their correction then carries its support."""
    if copy.valid_to is None or not copy.support_ids:
        return False
    if copy.id == _copy_id(copy.support_ids[0], kind, key):
        # Still on the mail that wrote it, so a later signature ended it.
        return False
    source = await uow.people.get(principal, copy.support_ids[0], ceiling=Sensitivity.RESTRICTED)
    return isinstance(source, PeopleSource) and source.source_kind == "owner"


async def project_signature(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    source: EmailSemanticSource,
    header: dict[str, object],
    person_id: UUID,
    source_id: UUID,
    now: datetime,
    common: _Common,
) -> None:
    """Record what the sender's own signature states, for the person who holds the address.

    The caller has verified the header and resolved the From address to
    `person_id`. Gmail must have authenticated the sender, and the thread page
    must hold the whole body, or nothing is read.
    """
    body = header.get("body")
    if (
        header.get("sender_check") != "pass"
        or header.get("body_complete") is not True
        or header.get("body_offset") not in (None, 0)
        or not isinstance(body, str)
    ):
        return
    contact = sender_contact(body, source.sender)
    if contact is None:
        return
    stated: dict[str, dict[str, str]] = {"role": {}, "phone": {}}
    if (
        contact.title is not None
        and not contains_secret_material(contact.title)
        and not contains_injection_pattern(contact.title)
    ):
        stated["role"][normalize_identifier("role", SIGNATURE_NAMESPACE, contact.title)] = (
            contact.title
        )
    for phone in contact.phones:
        try:
            stated["phone"][normalize_identifier("phone", SIGNATURE_NAMESPACE, phone)] = phone
        except ValueError:
            continue
    known = await _ever_stated(uow, principal, person_id)
    if known is None:
        # Too many aliases to know what the signatures have said; change nothing.
        return
    sent_at = source.sent_at
    for kind, values in stated.items():
        if not values:
            # A signature without this detail says nothing about it.
            continue
        copies: dict[str, list[PersonIdentifier]] = {}
        for key in sorted(known[kind] | set(values)):
            found = await _copies(uow, principal, person_id, kind, key)
            if found is None:
                break
            copies[key] = found
        else:
            # What each signature stated, by its sent time. Each copy holds from
            # its own message to the earliest later signature that omits it, so
            # the intervals are the same whatever order the mail is read in.
            signatures: dict[datetime, set[str]] = {}
            for key, rows in copies.items():
                for row in rows:
                    signatures.setdefault(row.valid_from, set()).add(key)
            for key in sorted(copies.keys() - set(values)):
                # This message omits the detail, so a copy still holding at its
                # sent time ends there, even one a later signature had ended.
                holding = [
                    copy
                    for copy in copies[key]
                    if copy.valid_from < sent_at
                    and (copy.valid_to is None or copy.valid_to > sent_at)
                ]
                if any(copy.valid_to is not None for copy in holding) and any(
                    [await _owner_ended(uow, principal, c, kind, key) for c in copies[key]]
                ):
                    # The owner said when it ended; that stays as they set it.
                    continue
                for copy in holding:
                    await uow.people.put(
                        copy.model_copy(
                            update={
                                "valid_to": sent_at,
                                "revision": copy.revision + 1,
                                "updated_at": max(now, copy.updated_at + timedelta(microseconds=1)),
                            }
                        ),
                        expected_revision=copy.revision,
                    )
            for key, written in values.items():
                if any([await _owner_ended(uow, principal, c, kind, key) for c in copies[key]]):
                    # The owner's correction wins over a signature that repeats it.
                    continue
                copy_id = _copy_id(source_id, kind, key)
                if (
                    await uow.people.is_erased(principal, copy_id)
                    or await uow.people.get(principal, copy_id, ceiling=Sensitivity.RESTRICTED)
                    is not None
                ):
                    continue
                await uow.people.put(
                    PersonIdentifier(
                        id=copy_id,
                        person_id=person_id,
                        identifier_kind="role" if kind == "role" else "phone",
                        namespace=SIGNATURE_NAMESPACE,
                        value=written,
                        context=SIGNATURE_CONTEXT,
                        verification="contextual",
                        valid_from=sent_at,
                        valid_to=min(
                            (
                                at
                                for at, said in signatures.items()
                                if at > sent_at and key not in said
                            ),
                            default=None,
                        ),
                        support_ids=[source_id],
                        **common,
                    ),
                    expected_revision=0,
                )
