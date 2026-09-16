"""Chat thread folder application service (Milestone 29).

Every method takes the principal as an argument and requires the exact
session scope: a folder is a view over conversations the principal already
owns, so no new authority is conferred. Names are refused before persistence,
nothing files a conversation without the owner's act, and every write records
a content-free process event.
"""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.events import ProcessEvent
from agent_core.domain.folders import (
    FOLDER_MAX_PER_PRINCIPAL,
    FolderProposal,
    FolderProposalKind,
    FolderProposalState,
    FolderWithdrawalReason,
    ThreadFolder,
    folder_name_key,
    is_chat_session,
    normalize_folder_name,
)
from agent_core.domain.views import FolderProposalView, FolderView, Page, SessionView
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory


async def record_folder_event(
    uow: RepositoryUnitOfWork,
    *,
    event_type: str,
    principal: Principal,
    payload: dict[str, object],
    key: str,
    clock: Clock,
    ids: IdFactory,
    actor_type: str = "principal",
) -> None:
    """Append one process event; payloads carry identifiers and counts only."""

    await uow.process_events.append(
        ProcessEvent(
            id=ids.new_id(),
            event_type=event_type,
            actor_type=actor_type,
            actor_id=principal.principal_id if actor_type == "principal" else None,
            payload={
                "tenant_id": principal.tenant_id,
                "principal_id": principal.principal_id,
                **payload,
            },
            derivation_key=f"{event_type}:{principal.tenant_id}:{principal.principal_id}:{key}",
            created_at=clock.now(),
        )
    )


async def withdraw_open_proposals(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    *,
    reason: FolderWithdrawalReason,
    clock: Clock,
    ids: IdFactory,
    naming: Iterable[UUID] = (),
    targeting: UUID | None = None,
    named: str | None = None,
    except_id: UUID | None = None,
    actor_type: str = "principal",
) -> list[FolderProposal]:
    """Withdraw every open proposal the cause touches, in the caller's transaction.

    `naming` matches proposals over any of those sessions, `targeting` matches
    add-to-folder proposals for that folder, and `named` matches new-folder
    proposals whose proposed name has that key.
    """

    sessions = set(naming)
    withdrawn: list[FolderProposal] = []
    for proposal in await uow.folders.list_proposals(principal, state=FolderProposalState.PROPOSED):
        if proposal.id == except_id:
            continue
        hit = bool(sessions.intersection(proposal.member_session_ids))
        hit = hit or (targeting is not None and proposal.target_folder_id == targeting)
        hit = hit or (
            named is not None
            and proposal.proposed_name is not None
            and folder_name_key(proposal.proposed_name) == named
        )
        if not hit:
            continue
        try:
            resolved = await uow.folders.resolve_proposal(
                proposal.id,
                principal,
                state=FolderProposalState.WITHDRAWN,
                resolved_at=clock.now(),
                withdrawal_reason=reason,
            )
        except ConflictError:
            continue  # Resolved concurrently; the other transaction recorded it.
        await record_folder_event(
            uow,
            event_type="folder.proposal.withdrawn",
            principal=principal,
            payload={
                "proposal_id": str(proposal.id),
                "kind": proposal.kind.value,
                "reason": reason.value,
                "members": len(proposal.member_session_ids),
            },
            key=str(proposal.id),
            clock=clock,
            ids=ids,
            actor_type=actor_type,
        )
        withdrawn.append(resolved)
    return withdrawn


async def unfile_deleted_session(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    session_id: UUID,
    *,
    clock: Clock,
    ids: IdFactory,
) -> None:
    """Leave no membership and no open proposal behind an ADR-0050 deletion."""

    await uow.folders.set_membership(session_id, principal, folder_id=None, added_at=clock.now())
    await withdraw_open_proposals(
        uow,
        principal,
        reason=FolderWithdrawalReason.MEMBER_GONE,
        clock=clock,
        ids=ids,
        naming=[session_id],
    )


class PublicFolderService:
    """Folders, memberships, and proposal review under the session scopes."""

    def __init__(self, *, uow_factory: UnitOfWorkFactory, clock: Clock, ids: IdFactory) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids

    async def create(self, principal: Principal, *, name: str) -> FolderView:
        require_scope(principal, "session.write")
        normalized = normalize_folder_name(name)
        async with self._uow_factory() as uow:
            folder = await self._create_folder(uow, principal, normalized)
        return FolderView.from_folder(folder, 0)

    async def list(self, principal: Principal) -> Page[FolderView]:
        require_scope(principal, "session.read")
        async with self._uow_factory() as uow:
            folders = await uow.folders.list_folders(principal)
            counts = await uow.folders.thread_counts(principal)
        return Page[FolderView](
            items=[FolderView.from_folder(folder, counts.get(folder.id, 0)) for folder in folders],
            next_cursor=None,
        )

    async def get(self, principal: Principal, folder_id: UUID) -> FolderView:
        require_scope(principal, "session.read")
        async with self._uow_factory() as uow:
            folder = await uow.folders.get_folder(folder_id, principal)
            members = await uow.folders.members_of(folder_id, principal)
        return FolderView.from_folder(folder, len(members))

    async def rename(self, principal: Principal, folder_id: UUID, *, name: str) -> FolderView:
        require_scope(principal, "session.write")
        normalized = normalize_folder_name(name)
        async with self._uow_factory() as uow:
            current = await uow.folders.get_folder(folder_id, principal)
            if current.name == normalized:
                members = await uow.folders.members_of(folder_id, principal)
                return FolderView.from_folder(current, len(members))
            renamed = await uow.folders.rename_folder(
                folder_id, principal, name=normalized, updated_at=self._clock.now()
            )
            await withdraw_open_proposals(
                uow,
                principal,
                reason=FolderWithdrawalReason.NAME_TAKEN,
                clock=self._clock,
                ids=self._ids,
                named=folder_name_key(normalized),
            )
            await self._record(
                uow,
                "folder.renamed",
                principal,
                {"folder_id": str(folder_id)},
                key=f"{folder_id}:{self._ids.new_id()}",
            )
            members = await uow.folders.members_of(folder_id, principal)
        return FolderView.from_folder(renamed, len(members))

    async def delete(self, principal: Principal, folder_id: UUID) -> None:
        require_scope(principal, "session.write")
        async with self._uow_factory() as uow:
            await uow.folders.get_folder(folder_id, principal)
            members = await uow.folders.members_of(folder_id, principal)
            await withdraw_open_proposals(
                uow,
                principal,
                reason=FolderWithdrawalReason.TARGET_GONE,
                clock=self._clock,
                ids=self._ids,
                targeting=folder_id,
            )
            await uow.folders.delete_folder(folder_id, principal)
            await self._record(
                uow,
                "folder.deleted",
                principal,
                {"folder_id": str(folder_id), "unfiled": len(members)},
                key=str(folder_id),
            )

    async def move_session(
        self, principal: Principal, session_id: UUID, *, folder_id: UUID | None
    ) -> SessionView:
        require_scope(principal, "session.write")
        now = self._clock.now()
        async with self._uow_factory() as uow:
            session = await uow.sessions.get(session_id, principal)
            if not is_chat_session(session.metadata):
                raise ConflictError(
                    "only chat conversations can be filed in a folder",
                    reason="session_not_chat",
                )
            if folder_id is not None:
                await uow.folders.get_folder(folder_id, principal)
            previous = await uow.folders.set_membership(
                session_id, principal, folder_id=folder_id, added_at=now
            )
            if previous != folder_id:
                await withdraw_open_proposals(
                    uow,
                    principal,
                    reason=FolderWithdrawalReason.MEMBER_GONE,
                    clock=self._clock,
                    ids=self._ids,
                    naming=[session_id],
                )
                await self._record(
                    uow,
                    "session.folder.changed",
                    principal,
                    {
                        "session_id": str(session_id),
                        "from_folder_id": None if previous is None else str(previous),
                        "to_folder_id": None if folder_id is None else str(folder_id),
                    },
                    key=f"{session_id}:{self._ids.new_id()}",
                )
            latest = await uow.runs.latest_for_session(session_id, principal)
        return SessionView.from_session(session, latest, folder_id=folder_id)

    async def proposals(
        self, principal: Principal, *, state: FolderProposalState | None
    ) -> Page[FolderProposalView]:
        require_scope(principal, "session.read")
        async with self._uow_factory() as uow:
            rows = await uow.folders.list_proposals(principal, state=state)
        return Page[FolderProposalView](
            items=[FolderProposalView.from_proposal(row) for row in rows],
            next_cursor=None,
        )

    async def accept(
        self, principal: Principal, proposal_id: UUID, *, name: str | None = None
    ) -> FolderProposalView:
        require_scope(principal, "session.write")
        now = self._clock.now()
        stale = False
        async with self._uow_factory() as uow:
            proposal = await uow.folders.get_proposal(proposal_id, principal)
            if proposal.state is FolderProposalState.ACCEPTED:
                return FolderProposalView.from_proposal(proposal)
            if proposal.state is not FolderProposalState.PROPOSED:
                raise ConflictError(
                    f"folder proposal {proposal_id} is already {proposal.state}",
                    reason="proposal_resolved",
                )
            eligible = await self._eligible_members(uow, principal, proposal)
            target: ThreadFolder | None = None
            if proposal.kind is FolderProposalKind.ADD_TO_FOLDER:
                assert proposal.target_folder_id is not None
                try:
                    target = await uow.folders.get_folder(proposal.target_folder_id, principal)
                except NotFoundError:
                    target = None
            if not eligible or (
                proposal.kind is FolderProposalKind.ADD_TO_FOLDER and target is None
            ):
                reason = (
                    FolderWithdrawalReason.MEMBER_GONE
                    if not eligible
                    else FolderWithdrawalReason.TARGET_GONE
                )
                await self._withdraw(uow, principal, proposal, reason)
                stale = True
            else:
                if target is None:
                    assert proposal.proposed_name is not None
                    folder_name = normalize_folder_name(
                        proposal.proposed_name if name is None else name
                    )
                    target = await self._create_folder(
                        uow, principal, folder_name, except_id=proposal.id
                    )
                for session_id in eligible:
                    await uow.folders.set_membership(
                        session_id, principal, folder_id=target.id, added_at=now
                    )
                await withdraw_open_proposals(
                    uow,
                    principal,
                    reason=FolderWithdrawalReason.MEMBER_GONE,
                    clock=self._clock,
                    ids=self._ids,
                    naming=eligible,
                    except_id=proposal.id,
                )
                # The proposal resolves last, so a rolled-back acceptance leaves
                # folder, memberships, and proposal untouched together.
                resolved = await uow.folders.resolve_proposal(
                    proposal.id,
                    principal,
                    state=FolderProposalState.ACCEPTED,
                    resolved_at=now,
                    resulting_folder_id=target.id,
                )
                await self._record(
                    uow,
                    "folder.proposal.accepted",
                    principal,
                    {
                        "proposal_id": str(proposal.id),
                        "kind": proposal.kind.value,
                        "folder_id": str(target.id),
                        "filed": len(eligible),
                        "skipped": len(proposal.member_session_ids) - len(eligible),
                    },
                    key=str(proposal.id),
                )
        if stale:
            raise ConflictError(
                "the proposal no longer applies and has been withdrawn",
                reason="proposal_stale",
            )
        return FolderProposalView.from_proposal(resolved)

    async def decline(self, principal: Principal, proposal_id: UUID) -> FolderProposalView:
        require_scope(principal, "session.write")
        async with self._uow_factory() as uow:
            proposal = await uow.folders.get_proposal(proposal_id, principal)
            if proposal.state is FolderProposalState.DECLINED:
                return FolderProposalView.from_proposal(proposal)
            if proposal.state is not FolderProposalState.PROPOSED:
                raise ConflictError(
                    f"folder proposal {proposal_id} is already {proposal.state}",
                    reason="proposal_resolved",
                )
            resolved = await uow.folders.resolve_proposal(
                proposal.id,
                principal,
                state=FolderProposalState.DECLINED,
                resolved_at=self._clock.now(),
            )
            await self._record(
                uow,
                "folder.proposal.declined",
                principal,
                {"proposal_id": str(proposal.id), "kind": proposal.kind.value},
                key=str(proposal.id),
            )
        return FolderProposalView.from_proposal(resolved)

    async def _create_folder(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        name: str,
        *,
        except_id: UUID | None = None,
    ) -> ThreadFolder:
        if await uow.folders.count_folders(principal) >= FOLDER_MAX_PER_PRINCIPAL:
            raise ConflictError(
                f"a principal holds at most {FOLDER_MAX_PER_PRINCIPAL} folders",
                reason="folder_limit",
            )
        now = self._clock.now()
        folder = await uow.folders.create_folder(
            ThreadFolder(
                id=self._ids.new_id(),
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                name=name,
                created_at=now,
                updated_at=now,
            )
        )
        await withdraw_open_proposals(
            uow,
            principal,
            reason=FolderWithdrawalReason.NAME_TAKEN,
            clock=self._clock,
            ids=self._ids,
            named=folder_name_key(name),
            except_id=except_id,
        )
        await self._record(
            uow, "folder.created", principal, {"folder_id": str(folder.id)}, key=str(folder.id)
        )
        return folder

    async def _eligible_members(
        self, uow: RepositoryUnitOfWork, principal: Principal, proposal: FolderProposal
    ) -> builtins.list[UUID]:
        filed = await uow.folders.folder_of(list(proposal.member_session_ids), principal)
        eligible: builtins.list[UUID] = []
        for session_id in proposal.member_session_ids:
            if session_id in filed:
                continue
            try:
                session = await uow.sessions.get(session_id, principal)
            except NotFoundError:
                continue
            if is_chat_session(session.metadata):
                eligible.append(session_id)
        return eligible

    async def _withdraw(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        proposal: FolderProposal,
        reason: FolderWithdrawalReason,
    ) -> None:
        await uow.folders.resolve_proposal(
            proposal.id,
            principal,
            state=FolderProposalState.WITHDRAWN,
            resolved_at=self._clock.now(),
            withdrawal_reason=reason,
        )
        await self._record(
            uow,
            "folder.proposal.withdrawn",
            principal,
            {
                "proposal_id": str(proposal.id),
                "kind": proposal.kind.value,
                "reason": reason.value,
                "members": len(proposal.member_session_ids),
            },
            key=str(proposal.id),
        )

    async def _record(
        self,
        uow: RepositoryUnitOfWork,
        event_type: str,
        principal: Principal,
        payload: dict[str, object],
        *,
        key: str,
    ) -> None:
        await record_folder_event(
            uow,
            event_type=event_type,
            principal=principal,
            payload=payload,
            key=key,
            clock=self._clock,
            ids=self._ids,
        )
