"""PostgreSQL chat thread folder, membership, and proposal store (Milestone 29)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.functions import func

from agent_core.adapters.persistence.sqlalchemy_models import (
    SessionFolderMembershipRow,
    ThreadFolderProposalRow,
    ThreadFolderRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.folders import (
    FolderProposal,
    FolderProposalDerivation,
    FolderProposalKind,
    FolderProposalState,
    FolderWithdrawalReason,
    ThreadFolder,
    folder_name_key,
    normalize_folder_name,
)


def _rowcount(result: Any) -> int:
    return int(result.rowcount or 0)


def _folder_from_row(row: ThreadFolderRow) -> ThreadFolder:
    return ThreadFolder(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        name=row.name,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _proposal_from_row(row: ThreadFolderProposalRow) -> FolderProposal:
    return FolderProposal(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        kind=FolderProposalKind(row.kind),
        proposed_name=row.proposed_name,
        target_folder_id=row.target_folder_id,
        member_session_ids=tuple(UUID(value) for value in row.member_session_ids),
        rationale=row.rationale,
        derivation=FolderProposalDerivation(row.derivation),
        content_key=row.content_key,
        state=FolderProposalState(row.state),
        withdrawal_reason=(
            FolderWithdrawalReason(row.withdrawal_reason)
            if row.withdrawal_reason is not None
            else None
        ),
        resulting_folder_id=row.resulting_folder_id,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
    )


class PostgresFolderStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_folder(self, folder: ThreadFolder) -> ThreadFolder:
        try:
            await self._session.execute(
                pg_insert(ThreadFolderRow).values(
                    id=folder.id,
                    tenant_id=folder.tenant_id,
                    principal_id=folder.principal_id,
                    name=folder.name,
                    name_key=folder_name_key(folder.name),
                    created_at=folder.created_at,
                    updated_at=folder.updated_at,
                )
            )
        except IntegrityError as error:
            raise ConflictError(
                f"folder name {folder.name!r} is taken", reason="folder_name_taken"
            ) from error
        return folder

    async def get_folder(self, folder_id: UUID, principal: Principal) -> ThreadFolder:
        return _folder_from_row(await self._owned_folder_row(folder_id, principal))

    async def list_folders(self, principal: Principal) -> list[ThreadFolder]:
        rows = (
            (
                await self._session.execute(
                    select(ThreadFolderRow)
                    .where(
                        ThreadFolderRow.tenant_id == principal.tenant_id,
                        ThreadFolderRow.principal_id == principal.principal_id,
                    )
                    .order_by(ThreadFolderRow.name_key.asc(), ThreadFolderRow.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [_folder_from_row(row) for row in rows]

    async def count_folders(self, principal: Principal) -> int:
        count = (
            await self._session.execute(
                select(func.count(ThreadFolderRow.id)).where(
                    ThreadFolderRow.tenant_id == principal.tenant_id,
                    ThreadFolderRow.principal_id == principal.principal_id,
                )
            )
        ).scalar_one()
        return int(count)

    async def thread_counts(self, principal: Principal) -> dict[UUID, int]:
        counts = {folder.id: 0 for folder in await self.list_folders(principal)}
        rows = await self._session.execute(
            select(SessionFolderMembershipRow.folder_id, func.count())
            .where(
                SessionFolderMembershipRow.tenant_id == principal.tenant_id,
                SessionFolderMembershipRow.principal_id == principal.principal_id,
            )
            .group_by(SessionFolderMembershipRow.folder_id)
        )
        for folder_id, count in rows.all():
            if folder_id in counts:
                counts[folder_id] = int(count)
        return counts

    async def rename_folder(
        self,
        folder_id: UUID,
        principal: Principal,
        *,
        name: str,
        updated_at: datetime,
    ) -> ThreadFolder:
        await self._owned_folder_row(folder_id, principal)
        normalized = normalize_folder_name(name)
        key = folder_name_key(normalized)
        clash = (
            await self._session.execute(
                select(ThreadFolderRow.id).where(
                    ThreadFolderRow.tenant_id == principal.tenant_id,
                    ThreadFolderRow.principal_id == principal.principal_id,
                    ThreadFolderRow.name_key == key,
                    ThreadFolderRow.id != folder_id,
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise ConflictError(f"folder name {normalized!r} is taken", reason="folder_name_taken")
        try:
            await self._session.execute(
                update(ThreadFolderRow)
                .where(
                    ThreadFolderRow.id == folder_id,
                    ThreadFolderRow.tenant_id == principal.tenant_id,
                    ThreadFolderRow.principal_id == principal.principal_id,
                )
                .values(name=normalized, name_key=key, updated_at=updated_at)
            )
        except IntegrityError as error:
            raise ConflictError(
                f"folder name {normalized!r} is taken", reason="folder_name_taken"
            ) from error
        return _folder_from_row(await self._owned_folder_row(folder_id, principal))

    async def delete_folder(self, folder_id: UUID, principal: Principal) -> None:
        await self._owned_folder_row(folder_id, principal)
        # Memberships go with the folder through the ON DELETE CASCADE.
        await self._session.execute(
            delete(ThreadFolderRow).where(
                ThreadFolderRow.id == folder_id,
                ThreadFolderRow.tenant_id == principal.tenant_id,
                ThreadFolderRow.principal_id == principal.principal_id,
            )
        )

    async def folder_of(
        self, session_ids: Sequence[UUID], principal: Principal
    ) -> dict[UUID, UUID]:
        if not session_ids:
            return {}
        rows = await self._session.execute(
            select(
                SessionFolderMembershipRow.session_id,
                SessionFolderMembershipRow.folder_id,
            ).where(
                SessionFolderMembershipRow.session_id.in_(list(session_ids)),
                SessionFolderMembershipRow.tenant_id == principal.tenant_id,
                SessionFolderMembershipRow.principal_id == principal.principal_id,
            )
        )
        return {row.session_id: row.folder_id for row in rows.all()}

    async def members_of(self, folder_id: UUID, principal: Principal) -> list[UUID]:
        await self._owned_folder_row(folder_id, principal)
        rows = await self._session.execute(
            select(SessionFolderMembershipRow.session_id)
            .where(
                SessionFolderMembershipRow.folder_id == folder_id,
                SessionFolderMembershipRow.tenant_id == principal.tenant_id,
                SessionFolderMembershipRow.principal_id == principal.principal_id,
            )
            .order_by(
                SessionFolderMembershipRow.added_at.asc(),
                SessionFolderMembershipRow.session_id.asc(),
            )
        )
        return list(rows.scalars().all())

    async def set_membership(
        self,
        session_id: UUID,
        principal: Principal,
        *,
        folder_id: UUID | None,
        added_at: datetime,
    ) -> UUID | None:
        if folder_id is not None:
            await self._owned_folder_row(folder_id, principal)
        previous = (
            await self._session.execute(
                select(SessionFolderMembershipRow.folder_id).where(
                    SessionFolderMembershipRow.session_id == session_id,
                    SessionFolderMembershipRow.tenant_id == principal.tenant_id,
                    SessionFolderMembershipRow.principal_id == principal.principal_id,
                )
            )
        ).scalar_one_or_none()
        if folder_id is None:
            if previous is not None:
                await self._session.execute(
                    delete(SessionFolderMembershipRow).where(
                        SessionFolderMembershipRow.session_id == session_id,
                        SessionFolderMembershipRow.tenant_id == principal.tenant_id,
                        SessionFolderMembershipRow.principal_id == principal.principal_id,
                    )
                )
            return previous
        if previous == folder_id:
            return previous
        statement = pg_insert(SessionFolderMembershipRow).values(
            session_id=session_id,
            folder_id=folder_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            added_at=added_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[SessionFolderMembershipRow.session_id],
            set_={
                "folder_id": folder_id,
                "tenant_id": principal.tenant_id,
                "principal_id": principal.principal_id,
                "added_at": added_at,
            },
        )
        try:
            await self._session.execute(statement)
        except IntegrityError as error:
            raise NotFoundError(f"session {session_id} not found") from error
        return previous

    async def propose(self, proposal: FolderProposal) -> FolderProposal:
        existing_rows = (
            (
                await self._session.execute(
                    select(ThreadFolderProposalRow).where(
                        ThreadFolderProposalRow.tenant_id == proposal.tenant_id,
                        ThreadFolderProposalRow.principal_id == proposal.principal_id,
                        ThreadFolderProposalRow.content_key == proposal.content_key,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in existing_rows:
            state = FolderProposalState(row.state)
            if state is FolderProposalState.PROPOSED:
                return _proposal_from_row(row)
            if state in (FolderProposalState.DECLINED, FolderProposalState.ACCEPTED):
                raise ConflictError(
                    f"grouping {proposal.content_key} has a durable {state} proposal",
                    reason="proposal_key_durable",
                )
        # A concurrent pass can win the open-proposal index; the savepoint keeps
        # the caller's transaction usable after that conflict.
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    pg_insert(ThreadFolderProposalRow).values(
                        id=proposal.id,
                        tenant_id=proposal.tenant_id,
                        principal_id=proposal.principal_id,
                        kind=proposal.kind.value,
                        proposed_name=proposal.proposed_name,
                        target_folder_id=proposal.target_folder_id,
                        member_session_ids=[str(member) for member in proposal.member_session_ids],
                        rationale=proposal.rationale,
                        derivation=proposal.derivation.value,
                        content_key=proposal.content_key,
                        state=proposal.state.value,
                        withdrawal_reason=(
                            proposal.withdrawal_reason.value
                            if proposal.withdrawal_reason is not None
                            else None
                        ),
                        resulting_folder_id=proposal.resulting_folder_id,
                        created_at=proposal.created_at,
                        resolved_at=proposal.resolved_at,
                    )
                )
        except IntegrityError as error:
            raise ConflictError(
                f"grouping {proposal.content_key} was proposed concurrently",
                reason="proposal_key_durable",
            ) from error
        return proposal

    async def get_proposal(self, proposal_id: UUID, principal: Principal) -> FolderProposal:
        return _proposal_from_row(await self._owned_proposal_row(proposal_id, principal))

    async def list_proposals(
        self,
        principal: Principal,
        *,
        state: FolderProposalState | None = None,
    ) -> list[FolderProposal]:
        query = select(ThreadFolderProposalRow).where(
            ThreadFolderProposalRow.tenant_id == principal.tenant_id,
            ThreadFolderProposalRow.principal_id == principal.principal_id,
        )
        if state is not None:
            query = query.where(ThreadFolderProposalRow.state == state.value)
        rows = (
            (
                await self._session.execute(
                    query.order_by(
                        ThreadFolderProposalRow.created_at.desc(),
                        ThreadFolderProposalRow.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        return [_proposal_from_row(row) for row in rows]

    async def resolve_proposal(
        self,
        proposal_id: UUID,
        principal: Principal,
        *,
        state: FolderProposalState,
        resolved_at: datetime,
        resulting_folder_id: UUID | None = None,
        withdrawal_reason: FolderWithdrawalReason | None = None,
    ) -> FolderProposal:
        # The guarded update is the atomicity: only a still-open row moves,
        # so two racing resolutions cannot both write a terminal state.
        result = await self._session.execute(
            update(ThreadFolderProposalRow)
            .where(
                ThreadFolderProposalRow.id == proposal_id,
                ThreadFolderProposalRow.tenant_id == principal.tenant_id,
                ThreadFolderProposalRow.principal_id == principal.principal_id,
                ThreadFolderProposalRow.state == FolderProposalState.PROPOSED.value,
            )
            .values(
                state=state.value,
                resolved_at=resolved_at,
                resulting_folder_id=resulting_folder_id,
                withdrawal_reason=(
                    withdrawal_reason.value if withdrawal_reason is not None else None
                ),
            )
        )
        if _rowcount(result) != 1:
            row = await self._owned_proposal_row(proposal_id, principal)
            raise ConflictError(
                f"folder proposal {proposal_id} is already {row.state}",
                reason="proposal_resolved",
            )
        return _proposal_from_row(await self._owned_proposal_row(proposal_id, principal))

    async def _owned_folder_row(self, folder_id: UUID, principal: Principal) -> ThreadFolderRow:
        row = (
            await self._session.execute(
                select(ThreadFolderRow).where(
                    ThreadFolderRow.id == folder_id,
                    ThreadFolderRow.tenant_id == principal.tenant_id,
                    ThreadFolderRow.principal_id == principal.principal_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"folder {folder_id} not found")
        return row

    async def _owned_proposal_row(
        self, proposal_id: UUID, principal: Principal
    ) -> ThreadFolderProposalRow:
        row = (
            await self._session.execute(
                select(ThreadFolderProposalRow).where(
                    ThreadFolderProposalRow.id == proposal_id,
                    ThreadFolderProposalRow.tenant_id == principal.tenant_id,
                    ThreadFolderProposalRow.principal_id == principal.principal_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"folder proposal {proposal_id} not found")
        return row
