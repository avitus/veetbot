"""PostgreSQL folder store parity coverage for Milestone 29."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_core.bootstrap import build
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
from agent_core.domain.sessions import Session, SessionStatus
from tests.integration.m2_support import PRINCIPAL, database_settings

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
AGENT_ID = UUID("00000000-0000-0000-0000-000000000010")


def _alembic(*arguments: str) -> None:
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Alembic command failed: {exc.stderr}") from exc


def _folder(principal: Principal, name: str) -> ThreadFolder:
    return ThreadFolder(
        id=uuid4(),
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        name=name,
        created_at=NOW,
        updated_at=NOW,
    )


def _session(principal: Principal) -> Session:
    return Session(
        id=uuid4(),
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        status=SessionStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )


def _proposal(principal: Principal, members: tuple[UUID, ...]) -> FolderProposal:
    return FolderProposal(
        id=uuid4(),
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        kind=FolderProposalKind.NEW_FOLDER,
        proposed_name="Travel",
        member_session_ids=tuple(sorted(members, key=lambda value: value.int)),
        derivation=FolderProposalDerivation.LEXICAL,
        created_at=NOW,
    )


async def test_folder_store_round_trips_folders_memberships_and_proposals(
    tmp_path: Path,
) -> None:
    _alembic("upgrade", "head")
    settings = replace(database_settings(), artifact_root=tmp_path / "artifacts")
    principal = PRINCIPAL.model_copy(update={"principal_id": f"folders-{uuid4().hex[:12]}"})
    foreign = PRINCIPAL.model_copy(update={"principal_id": f"other-{uuid4().hex[:12]}"})
    travel = _folder(principal, "Travel")
    work = _folder(principal, "Work")
    first, second = _session(principal), _session(principal)

    async with build(settings=settings, storage="postgres") as composition:
        async with composition.uow_factory() as uow:
            await uow.sessions.create(first)
            await uow.sessions.create(second)
            await uow.folders.create_folder(travel)
            await uow.folders.create_folder(work)

        with pytest.raises(ConflictError) as error:
            async with composition.uow_factory() as uow:
                await uow.folders.create_folder(_folder(principal, "travel"))
        assert error.value.reason == "folder_name_taken"

        async with composition.uow_factory() as uow:
            names = [row.name for row in await uow.folders.list_folders(principal)]
            assert names == ["Travel", "Work"]
            assert await uow.folders.list_folders(foreign) == []
            assert await uow.folders.count_folders(principal) == 2
            assert (
                await uow.folders.set_membership(
                    first.id, principal, folder_id=travel.id, added_at=NOW
                )
                is None
            )
            assert (
                await uow.folders.set_membership(
                    second.id, principal, folder_id=travel.id, added_at=NOW
                )
                is None
            )

        async with composition.uow_factory() as uow:
            assert await uow.folders.folder_of([first.id, second.id], principal) == {
                first.id: travel.id,
                second.id: travel.id,
            }
            assert await uow.folders.folder_of([first.id], foreign) == {}
            assert await uow.folders.thread_counts(principal) == {travel.id: 2, work.id: 0}
            assert (
                await uow.folders.set_membership(
                    second.id, principal, folder_id=work.id, added_at=NOW
                )
                == travel.id
            )
            assert await uow.folders.members_of(work.id, principal) == [second.id]
            with pytest.raises(NotFoundError):
                await uow.folders.set_membership(
                    first.id, foreign, folder_id=travel.id, added_at=NOW
                )

        async with composition.uow_factory() as uow:
            renamed = await uow.folders.rename_folder(
                travel.id, principal, name="TRAVEL", updated_at=NOW + timedelta(minutes=1)
            )
            assert renamed.name == "TRAVEL"
            with pytest.raises(ConflictError):
                await uow.folders.rename_folder(
                    travel.id, principal, name="work", updated_at=NOW + timedelta(minutes=2)
                )

        # ADR-0050 session deletion leaves no membership behind: the row goes
        # with the session through the foreign-key cascade.
        async with composition.uow_factory() as uow:
            assert await uow.session_deletions.delete(first.id, principal, NOW)
        async with composition.uow_factory() as uow:
            assert await uow.folders.folder_of([first.id, second.id], principal) == {
                second.id: work.id
            }

        # Deleting a folder unfiles its members and never touches the session.
        async with composition.uow_factory() as uow:
            await uow.folders.delete_folder(work.id, principal)
        async with composition.uow_factory() as uow:
            assert await uow.folders.folder_of([second.id], principal) == {}
            assert (await uow.sessions.get(second.id, principal)).id == second.id
            with pytest.raises(NotFoundError):
                await uow.folders.get_folder(work.id, principal)

        proposal = _proposal(principal, (second.id, uuid4()))
        async with composition.uow_factory() as uow:
            stored = await uow.folders.propose(proposal)
            assert stored.state is FolderProposalState.PROPOSED
        async with composition.uow_factory() as uow:
            replayed = await uow.folders.propose(proposal.model_copy(update={"id": uuid4()}))
            assert replayed.id == proposal.id
            with pytest.raises(NotFoundError):
                await uow.folders.get_proposal(proposal.id, foreign)
        async with composition.uow_factory() as uow:
            declined = await uow.folders.resolve_proposal(
                proposal.id,
                principal,
                state=FolderProposalState.DECLINED,
                resolved_at=NOW + timedelta(minutes=3),
            )
            assert declined.state is FolderProposalState.DECLINED
        with pytest.raises(ConflictError):
            async with composition.uow_factory() as uow:
                await uow.folders.propose(proposal.model_copy(update={"id": uuid4()}))
        with pytest.raises(ConflictError):
            async with composition.uow_factory() as uow:
                await uow.folders.resolve_proposal(
                    proposal.id,
                    principal,
                    state=FolderProposalState.WITHDRAWN,
                    resolved_at=NOW + timedelta(minutes=4),
                    withdrawal_reason=FolderWithdrawalReason.MEMBER_GONE,
                )
        async with composition.uow_factory() as uow:
            open_rows = await uow.folders.list_proposals(
                principal, state=FolderProposalState.PROPOSED
            )
            declined_rows = await uow.folders.list_proposals(
                principal, state=FolderProposalState.DECLINED
            )
            assert open_rows == []
            assert [row.id for row in declined_rows] == [proposal.id]
