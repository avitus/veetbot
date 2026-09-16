"""Folder, membership, and proposal store contract."""

from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.persistence.memory import InMemoryFolderStore
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.folders import (
    FolderProposal,
    FolderProposalDerivation,
    FolderProposalKind,
    FolderProposalState,
    FolderWithdrawalReason,
    ThreadFolder,
)
from tests.contract.support import NOW, PRINCIPAL_ID, TENANT, principal

FOLDER_A = UUID("00000000-0000-0000-0000-000000000701")
FOLDER_B = UUID("00000000-0000-0000-0000-000000000702")
SESSION_1 = UUID("00000000-0000-0000-0000-000000000801")
SESSION_2 = UUID("00000000-0000-0000-0000-000000000802")
SESSION_3 = UUID("00000000-0000-0000-0000-000000000803")
PROPOSAL_1 = UUID("00000000-0000-0000-0000-000000000901")
PROPOSAL_2 = UUID("00000000-0000-0000-0000-000000000902")
PROPOSAL_3 = UUID("00000000-0000-0000-0000-000000000903")


def folder(
    *,
    folder_id: UUID = FOLDER_A,
    name: str = "Travel",
    tenant_id: str = TENANT,
    principal_id: str = PRINCIPAL_ID,
) -> ThreadFolder:
    return ThreadFolder(
        id=folder_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        name=name,
        created_at=NOW,
        updated_at=NOW,
    )


def proposal(
    *,
    proposal_id: UUID = PROPOSAL_1,
    kind: FolderProposalKind = FolderProposalKind.NEW_FOLDER,
    members: tuple[UUID, ...] = (SESSION_1, SESSION_2),
    target: UUID | None = None,
    principal_id: str = PRINCIPAL_ID,
) -> FolderProposal:
    return FolderProposal(
        id=proposal_id,
        tenant_id=TENANT,
        principal_id=principal_id,
        kind=kind,
        proposed_name="Travel" if kind is FolderProposalKind.NEW_FOLDER else None,
        target_folder_id=target,
        member_session_ids=members,
        derivation=FolderProposalDerivation.LEXICAL,
        created_at=NOW,
    )


def _store() -> InMemoryFolderStore:
    return InMemoryFolderStore()


def _foreign() -> Principal:
    return principal().model_copy(update={"principal_id": "other"})


async def test_folders_are_created_listed_by_name_and_counted() -> None:
    store = _store()
    await store.create_folder(folder(folder_id=FOLDER_B, name="work"))
    await store.create_folder(folder(folder_id=FOLDER_A, name="Travel"))
    names = [row.name for row in await store.list_folders(principal())]
    assert names == ["Travel", "work"]
    assert await store.count_folders(principal()) == 2
    assert await store.count_folders(_foreign()) == 0
    assert (await store.get_folder(FOLDER_A, principal())).name == "Travel"


async def test_folder_names_are_unique_per_principal_case_insensitively() -> None:
    store = _store()
    await store.create_folder(folder(name="Travel"))
    with pytest.raises(ConflictError) as error:
        await store.create_folder(folder(folder_id=FOLDER_B, name="travel"))
    assert error.value.reason == "folder_name_taken"
    other = await store.create_folder(
        folder(folder_id=FOLDER_B, name="travel", principal_id="other")
    )
    assert other.name == "travel"


async def test_rename_allows_a_case_change_and_refuses_another_folders_key() -> None:
    store = _store()
    await store.create_folder(folder(name="Travel"))
    await store.create_folder(folder(folder_id=FOLDER_B, name="Work"))
    later = NOW + timedelta(minutes=1)
    renamed = await store.rename_folder(FOLDER_A, principal(), name="TRAVEL", updated_at=later)
    assert renamed.name == "TRAVEL"
    assert renamed.updated_at == later
    with pytest.raises(ConflictError) as error:
        await store.rename_folder(FOLDER_A, principal(), name="work", updated_at=later)
    assert error.value.reason == "folder_name_taken"
    with pytest.raises(NotFoundError):
        await store.rename_folder(FOLDER_A, _foreign(), name="Other", updated_at=later)


async def test_membership_is_single_parent_and_reports_the_previous_folder() -> None:
    store = _store()
    await store.create_folder(folder(folder_id=FOLDER_A, name="Travel"))
    await store.create_folder(folder(folder_id=FOLDER_B, name="Work"))
    assert (
        await store.set_membership(SESSION_1, principal(), folder_id=FOLDER_A, added_at=NOW) is None
    )
    assert (
        await store.set_membership(SESSION_1, principal(), folder_id=FOLDER_B, added_at=NOW)
        == FOLDER_A
    )
    assert await store.folder_of([SESSION_1, SESSION_2], principal()) == {SESSION_1: FOLDER_B}
    assert await store.members_of(FOLDER_B, principal()) == [SESSION_1]
    assert await store.members_of(FOLDER_A, principal()) == []
    assert (
        await store.set_membership(SESSION_1, principal(), folder_id=None, added_at=NOW) == FOLDER_B
    )
    assert await store.set_membership(SESSION_1, principal(), folder_id=None, added_at=NOW) is None
    assert await store.folder_of([SESSION_1], principal()) == {}


async def test_membership_requires_an_owned_folder() -> None:
    store = _store()
    await store.create_folder(folder(folder_id=FOLDER_A, name="Travel"))
    with pytest.raises(NotFoundError):
        await store.set_membership(SESSION_1, principal(), folder_id=FOLDER_B, added_at=NOW)
    with pytest.raises(NotFoundError):
        await store.set_membership(SESSION_1, _foreign(), folder_id=FOLDER_A, added_at=NOW)
    assert await store.folder_of([SESSION_1], principal()) == {}


async def test_thread_counts_and_delete_cascade_memberships() -> None:
    store = _store()
    await store.create_folder(folder(folder_id=FOLDER_A, name="Travel"))
    await store.create_folder(folder(folder_id=FOLDER_B, name="Work"))
    await store.set_membership(SESSION_1, principal(), folder_id=FOLDER_A, added_at=NOW)
    await store.set_membership(SESSION_2, principal(), folder_id=FOLDER_A, added_at=NOW)
    assert await store.thread_counts(principal()) == {FOLDER_A: 2, FOLDER_B: 0}
    await store.delete_folder(FOLDER_A, principal())
    assert await store.folder_of([SESSION_1, SESSION_2], principal()) == {}
    assert [row.id for row in await store.list_folders(principal())] == [FOLDER_B]
    with pytest.raises(NotFoundError):
        await store.get_folder(FOLDER_A, principal())
    with pytest.raises(NotFoundError):
        await store.delete_folder(FOLDER_B, _foreign())


async def test_propose_is_idempotent_while_open() -> None:
    store = _store()
    first = await store.propose(proposal())
    replay = await store.propose(proposal(proposal_id=PROPOSAL_2))
    assert replay.id == first.id
    assert [row.id for row in await store.list_proposals(principal())] == [first.id]


async def test_declined_and_accepted_keys_are_durable_and_withdrawn_keys_are_free() -> None:
    store = _store()
    await store.propose(proposal())
    await store.resolve_proposal(
        PROPOSAL_1,
        principal(),
        state=FolderProposalState.DECLINED,
        resolved_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ConflictError):
        await store.propose(proposal(proposal_id=PROPOSAL_2))

    accepted = await store.propose(proposal(proposal_id=PROPOSAL_2, members=(SESSION_2, SESSION_3)))
    await store.resolve_proposal(
        accepted.id,
        principal(),
        state=FolderProposalState.ACCEPTED,
        resolved_at=NOW + timedelta(minutes=2),
        resulting_folder_id=FOLDER_A,
    )
    with pytest.raises(ConflictError):
        await store.propose(proposal(proposal_id=PROPOSAL_3, members=(SESSION_2, SESSION_3)))

    withdrawn = await store.propose(
        proposal(proposal_id=PROPOSAL_3, members=(SESSION_1, SESSION_3))
    )
    await store.resolve_proposal(
        withdrawn.id,
        principal(),
        state=FolderProposalState.WITHDRAWN,
        resolved_at=NOW + timedelta(minutes=3),
        withdrawal_reason=FolderWithdrawalReason.MEMBER_GONE,
    )
    again = await store.propose(
        proposal(
            proposal_id=UUID("00000000-0000-0000-0000-000000000904"), members=(SESSION_1, SESSION_3)
        )
    )
    assert again.state is FolderProposalState.PROPOSED


async def test_resolution_is_exactly_once_and_records_its_outcome() -> None:
    store = _store()
    await store.propose(proposal())
    resolved = await store.resolve_proposal(
        PROPOSAL_1,
        principal(),
        state=FolderProposalState.ACCEPTED,
        resolved_at=NOW + timedelta(minutes=1),
        resulting_folder_id=FOLDER_A,
    )
    assert resolved.state is FolderProposalState.ACCEPTED
    assert resolved.resulting_folder_id == FOLDER_A
    with pytest.raises(ConflictError):
        await store.resolve_proposal(
            PROPOSAL_1,
            principal(),
            state=FolderProposalState.DECLINED,
            resolved_at=NOW + timedelta(minutes=2),
        )


async def test_proposals_are_principal_scoped_and_filter_by_state() -> None:
    store = _store()
    await store.propose(proposal())
    await store.propose(proposal(proposal_id=PROPOSAL_2, members=(SESSION_2, SESSION_3)))
    await store.resolve_proposal(
        PROPOSAL_2,
        principal(),
        state=FolderProposalState.DECLINED,
        resolved_at=NOW + timedelta(minutes=1),
    )
    foreign = _foreign()
    with pytest.raises(NotFoundError):
        await store.get_proposal(PROPOSAL_1, foreign)
    with pytest.raises(NotFoundError):
        await store.resolve_proposal(
            PROPOSAL_1,
            foreign,
            state=FolderProposalState.DECLINED,
            resolved_at=NOW + timedelta(minutes=1),
        )
    assert await store.list_proposals(foreign) == []
    open_rows = await store.list_proposals(principal(), state=FolderProposalState.PROPOSED)
    declined_rows = await store.list_proposals(principal(), state=FolderProposalState.DECLINED)
    assert [row.id for row in open_rows] == [PROPOSAL_1]
    assert [row.id for row in declined_rows] == [PROPOSAL_2]
    assert (await store.get_proposal(PROPOSAL_1, principal())).state is FolderProposalState.PROPOSED
