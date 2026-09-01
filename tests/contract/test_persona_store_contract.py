"""Persona document and nomination store contract."""

from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.persistence.memory import InMemoryPersonaStore
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.memory import BeliefType, MemoryAuthority, Sensitivity
from agent_core.domain.persona import (
    PersonaDocument,
    PersonaEntry,
    PersonaEntrySource,
    PersonaNomination,
    PersonaNominationState,
)
from tests.contract.support import NOW, PRINCIPAL_ID, TENANT, principal

BELIEF_ID = UUID("00000000-0000-0000-0000-000000000501")
NOMINATION_ID = UUID("00000000-0000-0000-0000-000000000601")


def document(
    *,
    version: int = 1,
    texts: tuple[str, ...] = ("User values direct answers.",),
    tenant_id: str = TENANT,
    principal_id: str = PRINCIPAL_ID,
) -> PersonaDocument:
    return PersonaDocument(
        tenant_id=tenant_id,
        principal_id=principal_id,
        version=version,
        entries=tuple(
            PersonaEntry(text=text, source=PersonaEntrySource.USER_EDIT) for text in texts
        ),
        source=PersonaEntrySource.USER_EDIT,
        created_at=NOW + timedelta(seconds=version),
    )


def nomination(
    *,
    nomination_id: UUID = NOMINATION_ID,
    belief_id: UUID = BELIEF_ID,
    tenant_id: str = TENANT,
    principal_id: str = PRINCIPAL_ID,
) -> PersonaNomination:
    return PersonaNomination(
        id=nomination_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        belief_id=belief_id,
        statement="User prefers concise answers.",
        belief_type=BeliefType.PREFERENCE,
        authority=MemoryAuthority.AFFIRMED,
        confidence=0.9,
        corroboration_count=3,
        sensitivity=Sensitivity.INTERNAL,
        nominated_at=NOW,
    )


def _store() -> InMemoryPersonaStore:
    return InMemoryPersonaStore()


async def test_unwritten_persona_reads_as_absent_and_head_wins() -> None:
    store = _store()
    assert await store.active(principal()) is None
    await store.append_version(document(version=1), expected_version=0)
    await store.append_version(document(version=2, texts=("A", "B")), expected_version=1)
    head = await store.active(principal())
    assert head is not None
    assert head.version == 2
    assert await store.active(principal().model_copy(update={"principal_id": "other"})) is None


async def test_append_version_requires_the_current_head() -> None:
    store = _store()
    with pytest.raises(ConflictError):
        await store.append_version(document(version=1), expected_version=3)
    await store.append_version(document(version=1), expected_version=0)
    with pytest.raises(ConflictError):
        await store.append_version(document(version=2), expected_version=0)
    with pytest.raises(ConflictError):
        await store.append_version(document(version=3), expected_version=1)
    appended = await store.append_version(document(version=2), expected_version=1)
    assert appended.version == 2


async def test_history_is_newest_first_and_bounded() -> None:
    store = _store()
    for version in (1, 2, 3):
        await store.append_version(document(version=version), expected_version=version - 1)
    versions = [entry.version for entry in await store.history(principal(), limit=2)]
    assert versions == [3, 2]
    assert await store.history(principal().model_copy(update={"principal_id": "other"})) == []


async def test_nominate_is_idempotent_while_open() -> None:
    store = _store()
    first = await store.nominate(nomination())
    replay = await store.nominate(
        nomination(nomination_id=UUID("00000000-0000-0000-0000-000000000602"))
    )
    assert replay.id == first.id
    assert [entry.id for entry in await store.list_nominations(principal())] == [first.id]


async def test_decline_is_durable_and_affirmed_blocks_renomination() -> None:
    store = _store()
    await store.nominate(nomination())
    await store.resolve_nomination(
        NOMINATION_ID,
        principal(),
        state=PersonaNominationState.DECLINED,
        resolved_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ConflictError):
        await store.nominate(nomination(nomination_id=UUID("00000000-0000-0000-0000-000000000603")))

    other_belief = UUID("00000000-0000-0000-0000-000000000502")
    second = await store.nominate(
        nomination(
            nomination_id=UUID("00000000-0000-0000-0000-000000000604"),
            belief_id=other_belief,
        )
    )
    await store.resolve_nomination(
        second.id,
        principal(),
        state=PersonaNominationState.AFFIRMED,
        resolved_at=NOW + timedelta(minutes=2),
        affirmed_version=1,
    )
    with pytest.raises(ConflictError):
        await store.nominate(
            nomination(
                nomination_id=UUID("00000000-0000-0000-0000-000000000605"),
                belief_id=other_belief,
            )
        )


async def test_withdrawal_frees_the_belief_for_renomination() -> None:
    store = _store()
    await store.nominate(nomination())
    await store.resolve_nomination(
        NOMINATION_ID,
        principal(),
        state=PersonaNominationState.WITHDRAWN,
        resolved_at=NOW + timedelta(minutes=1),
    )
    renominated = await store.nominate(
        nomination(nomination_id=UUID("00000000-0000-0000-0000-000000000606"))
    )
    assert renominated.state is PersonaNominationState.NOMINATED


async def test_resolution_is_exactly_once() -> None:
    store = _store()
    await store.nominate(nomination())
    await store.resolve_nomination(
        NOMINATION_ID,
        principal(),
        state=PersonaNominationState.DECLINED,
        resolved_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ConflictError):
        await store.resolve_nomination(
            NOMINATION_ID,
            principal(),
            state=PersonaNominationState.AFFIRMED,
            resolved_at=NOW + timedelta(minutes=2),
            affirmed_version=1,
        )


async def test_nominations_are_principal_scoped() -> None:
    store = _store()
    await store.nominate(nomination())
    foreign = principal().model_copy(update={"principal_id": "other"})
    with pytest.raises(NotFoundError):
        await store.get_nomination(NOMINATION_ID, foreign)
    with pytest.raises(NotFoundError):
        await store.resolve_nomination(
            NOMINATION_ID,
            foreign,
            state=PersonaNominationState.DECLINED,
            resolved_at=NOW + timedelta(minutes=1),
        )
    assert await store.list_nominations(foreign) == []
    assert [entry.id for entry in await store.list_nominations(principal())] == [NOMINATION_ID]


async def test_list_nominations_filters_by_state() -> None:
    store = _store()
    await store.nominate(nomination())
    await store.resolve_nomination(
        NOMINATION_ID,
        principal(),
        state=PersonaNominationState.DECLINED,
        resolved_at=NOW + timedelta(minutes=1),
    )
    open_rows = await store.list_nominations(principal(), state=PersonaNominationState.NOMINATED)
    declined_rows = await store.list_nominations(principal(), state=PersonaNominationState.DECLINED)
    assert open_rows == []
    assert [entry.id for entry in declined_rows] == [NOMINATION_ID]
