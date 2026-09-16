"""Chat thread folder, membership, and proposal store port (Milestone 29)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.folders import (
    FolderProposal,
    FolderProposalState,
    FolderWithdrawalReason,
    ThreadFolder,
)


class FolderStore(Protocol):
    """Folders, session memberships, and grouping proposals, principal-scoped.

    Folder names are unique per principal on their case-folded key; a second
    create or a rename onto another folder's key is a `ConflictError` with
    reason `folder_name_taken`. A membership is keyed by the session, so
    `set_membership` is an upsert that returns the previous folder, and `None`
    removes the row. `propose` is idempotent while a proposal with the same
    content key is open and refuses a key whose proposal was declined or
    accepted — the owner's verdict is durable; a withdrawn key may be proposed
    again. `resolve_proposal` moves only a still-open row and raises
    `ConflictError` otherwise. Cross-principal reads raise `NotFoundError`,
    indistinguishable from absence.
    """

    async def create_folder(self, folder: ThreadFolder) -> ThreadFolder: ...

    async def get_folder(self, folder_id: UUID, principal: Principal) -> ThreadFolder: ...

    async def list_folders(self, principal: Principal) -> list[ThreadFolder]: ...

    async def count_folders(self, principal: Principal) -> int: ...

    async def thread_counts(self, principal: Principal) -> dict[UUID, int]: ...

    async def rename_folder(
        self,
        folder_id: UUID,
        principal: Principal,
        *,
        name: str,
        updated_at: datetime,
    ) -> ThreadFolder: ...

    async def delete_folder(self, folder_id: UUID, principal: Principal) -> None: ...

    async def folder_of(
        self, session_ids: Sequence[UUID], principal: Principal
    ) -> dict[UUID, UUID]: ...

    async def members_of(self, folder_id: UUID, principal: Principal) -> list[UUID]: ...

    async def set_membership(
        self,
        session_id: UUID,
        principal: Principal,
        *,
        folder_id: UUID | None,
        added_at: datetime,
    ) -> UUID | None: ...

    async def propose(self, proposal: FolderProposal) -> FolderProposal: ...

    async def get_proposal(self, proposal_id: UUID, principal: Principal) -> FolderProposal: ...

    async def list_proposals(
        self,
        principal: Principal,
        *,
        state: FolderProposalState | None = None,
    ) -> list[FolderProposal]: ...

    async def resolve_proposal(
        self,
        proposal_id: UUID,
        principal: Principal,
        *,
        state: FolderProposalState,
        resolved_at: datetime,
        resulting_folder_id: UUID | None = None,
        withdrawal_reason: FolderWithdrawalReason | None = None,
    ) -> FolderProposal: ...
