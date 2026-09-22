"""The maintenance pass that proposes folders (Milestone 29).

Runs for one principal on its own timer: withdraws open proposals the world
has moved past, stops when the open set is full, reads the unfiled chat
conversations and the folders that exist, asks the grouper, re-applies every
eligibility rule to what comes back, and proposes at most the free slots.
Every event it records carries identifiers and counts only.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import replace
from uuid import UUID

from agent_core.application.folder_service import record_folder_event
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, FolderNameError, NotFoundError
from agent_core.domain.events import EventEnvelope
from agent_core.domain.folders import (
    FolderProposal,
    FolderProposalKind,
    FolderProposalState,
    FolderWithdrawalReason,
    ThreadFolder,
    folder_name_key,
    is_chat_session,
    normalize_folder_name,
    proposal_content_key,
)
from agent_core.domain.messages import TextPart
from agent_core.domain.sessions import Session, SessionCursor
from agent_core.folders.clustering import (
    FolderSummary,
    GroupCandidate,
    GroupingInput,
    GroupingOutcome,
    JudgmentAudit,
    ThreadGrouper,
    ThreadSummary,
)
from agent_core.folders.profiles import FolderProposalProfile
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

logger = logging.getLogger(__name__)

FOLDER_PROPOSAL_MAX_CANDIDATES = 200
FOLDER_PROPOSAL_SNIPPET_CHARS = 400
FOLDER_PROPOSAL_NEAR_DUPLICATE = 0.8
_PAGE_SIZE = 100
_SNIPPET_EVENT_WINDOW = 8
_SAMPLE_TITLES = 5


def _event_text(event: EventEnvelope) -> str:
    content = event.payload.get("content")
    texts: list[str] = []
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        for raw in content:
            if not isinstance(raw, dict):
                continue
            try:
                part = TextPart.model_validate(raw)
            except ValueError:
                continue
            texts.append(part.text)
    return "\n".join(texts).strip()


def _member_jaccard(left: set[UUID], right: set[UUID]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


class FolderProposalPass:
    """One principal's proposal sweep; `run_once` returns the proposals created."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        principal: Principal,
        profile: FolderProposalProfile,
        grouper: ThreadGrouper,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._principal = principal
        self._profile = profile
        self._grouper = grouper

    async def run_once(self) -> int:
        if not self._profile.enabled:
            return 0
        principal = self._principal
        profile = self._profile
        attempt_id = self._ids.new_id()
        withdrawn = 0
        async with self._uow_factory() as uow:
            folders = await uow.folders.list_folders(principal)
            folder_ids = {folder.id for folder in folders}
            name_keys = {folder_name_key(folder.name) for folder in folders}
            remaining: list[FolderProposal] = []
            for proposal in await uow.folders.list_proposals(
                principal, state=FolderProposalState.PROPOSED
            ):
                reason = await self._stale_reason(uow, proposal, folder_ids, name_keys)
                if reason is None:
                    remaining.append(proposal)
                    continue
                await self._withdraw(uow, proposal, reason)
                withdrawn += 1
            free = profile.max_open - len(remaining)
            if free <= 0:
                await self._record_pass(uow, attempt_id, withdrawn=withdrawn)
                return 0
            sessions = await self._candidate_sessions(uow)
            filed = await uow.folders.folder_of([session.id for session in sessions], principal)
            reserved = {member for proposal in remaining for member in proposal.member_session_ids}
            unfiled = [
                session
                for session in sessions
                if session.id not in filed and session.id not in reserved
            ]
            if not unfiled or (len(unfiled) < profile.threshold and not folders):
                await self._record_pass(uow, attempt_id, withdrawn=withdrawn)
                return 0
            titles_by_folder: dict[UUID, list[str]] = defaultdict(list)
            for session in sessions:
                if session.id in filed and session.title:
                    titles_by_folder[filed[session.id]].append(session.title)
            summaries_of_threads: list[ThreadSummary] = []
            for session in unfiled:
                summaries_of_threads.append(
                    ThreadSummary(
                        session_id=session.id,
                        title=session.title or "",
                        snippet=await self._snippet(uow, session.id),
                    )
                )
            threads = tuple(summaries_of_threads)
            summaries = tuple(
                FolderSummary(
                    id=folder.id,
                    name=folder.name,
                    member_titles=tuple(titles_by_folder[folder.id][:_SAMPLE_TITLES]),
                )
                for folder in folders
            )
            declined = await uow.folders.list_proposals(
                principal, state=FolderProposalState.DECLINED
            )
        grouping = GroupingInput(
            threads=threads,
            folders=summaries,
            threshold=profile.threshold,
            max_members=profile.max_members,
            similarity_threshold=profile.similarity_threshold,
        )
        outcome = await self._grouper.group(grouping, principal=principal)
        selected = self._select(
            outcome.candidates,
            candidate_ids={thread.session_id for thread in threads},
            folder_ids=folder_ids,
            name_keys=name_keys,
            declined=declined,
            free=free,
        )
        created = 0
        async with self._uow_factory() as uow:
            for candidate in selected:
                proposal = FolderProposal(
                    id=self._ids.new_id(),
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    kind=candidate.kind,
                    proposed_name=candidate.name,
                    target_folder_id=candidate.target_folder_id,
                    member_session_ids=candidate.member_session_ids,
                    rationale=candidate.rationale,
                    derivation=candidate.derivation,
                    created_at=self._clock.now(),
                )
                try:
                    stored = await uow.folders.propose(proposal)
                except ConflictError:
                    continue  # A durable verdict or a concurrent pass: never again.
                if stored.id != proposal.id:
                    continue
                created += 1
                await record_folder_event(
                    uow,
                    event_type="folder.proposal.created",
                    principal=principal,
                    payload={
                        "proposal_id": str(proposal.id),
                        "kind": proposal.kind.value,
                        "derivation": proposal.derivation.value,
                        "members": len(proposal.member_session_ids),
                    },
                    key=str(proposal.id),
                    clock=self._clock,
                    ids=self._ids,
                    actor_type="system",
                )
            await self._record_pass(
                uow,
                attempt_id,
                withdrawn=withdrawn,
                candidates=len(outcome.candidates),
                created=created,
                outcome=outcome,
            )
        return created

    async def _candidate_sessions(self, uow: RepositoryUnitOfWork) -> list[Session]:
        collected: list[Session] = []
        cursor: SessionCursor | None = None
        while len(collected) < FOLDER_PROPOSAL_MAX_CANDIDATES:
            page = await uow.sessions.list(
                self._principal, limit=_PAGE_SIZE, cursor=cursor, exclude_operational=True
            )
            for session in page:
                if session.title and is_chat_session(session.metadata):
                    collected.append(session)
            if len(page) < _PAGE_SIZE:
                break
            cursor = SessionCursor(updated_at=page[-1].updated_at, id=page[-1].id)
        return collected[:FOLDER_PROPOSAL_MAX_CANDIDATES]

    async def _snippet(self, uow: RepositoryUnitOfWork, session_id: UUID) -> str:
        events = await uow.events.list_after(
            session_id, 0, self._principal, limit=_SNIPPET_EVENT_WINDOW
        )
        for event in events:
            if (
                event.event_type == "user.message.created"
                and event.actor_id == self._principal.principal_id
            ):
                return _event_text(event)[:FOLDER_PROPOSAL_SNIPPET_CHARS]
        return ""

    async def _stale_reason(
        self,
        uow: RepositoryUnitOfWork,
        proposal: FolderProposal,
        folder_ids: set[UUID],
        name_keys: set[str],
    ) -> FolderWithdrawalReason | None:
        if (
            proposal.kind is FolderProposalKind.ADD_TO_FOLDER
            and proposal.target_folder_id not in folder_ids
        ):
            return FolderWithdrawalReason.TARGET_GONE
        if (
            proposal.kind is FolderProposalKind.NEW_FOLDER
            and proposal.proposed_name is not None
            and folder_name_key(proposal.proposed_name) in name_keys
        ):
            return FolderWithdrawalReason.NAME_TAKEN
        filed = await uow.folders.folder_of(list(proposal.member_session_ids), self._principal)
        if filed:
            return FolderWithdrawalReason.MEMBER_GONE
        for member in proposal.member_session_ids:
            try:
                session = await uow.sessions.get(member, self._principal)
            except NotFoundError:
                return FolderWithdrawalReason.MEMBER_GONE
            if not is_chat_session(session.metadata):
                return FolderWithdrawalReason.MEMBER_GONE
        return None

    def _select(
        self,
        candidates: tuple[GroupCandidate, ...],
        *,
        candidate_ids: set[UUID],
        folder_ids: set[UUID],
        name_keys: set[str],
        declined: list[FolderProposal],
        free: int,
    ) -> list[GroupCandidate]:
        # The pass trusts nothing the grouper returns: every rule is re-applied.
        declined_keys = {proposal.content_key for proposal in declined}
        used: set[UUID] = set()
        chosen: list[GroupCandidate] = []
        for candidate in candidates:
            members = tuple(
                sorted(
                    {
                        member
                        for member in candidate.member_session_ids
                        if member in candidate_ids and member not in used
                    },
                    key=lambda item: item.int,
                )
            )[: self._profile.max_members]
            if not members:
                continue
            name: str | None
            if candidate.kind is FolderProposalKind.NEW_FOLDER:
                if len(members) < self._profile.threshold or candidate.name is None:
                    continue
                try:
                    name = normalize_folder_name(candidate.name)
                except FolderNameError:
                    continue
                if folder_name_key(name) in name_keys:
                    continue
                target = None
            else:
                if candidate.target_folder_id not in folder_ids:
                    continue
                name, target = None, candidate.target_folder_id
            key = proposal_content_key(candidate.kind, target, members)
            if key in declined_keys:
                continue
            member_set = set(members)
            if any(
                verdict.kind is candidate.kind
                and verdict.target_folder_id == target
                and _member_jaccard(member_set, set(verdict.member_session_ids))
                >= FOLDER_PROPOSAL_NEAR_DUPLICATE
                for verdict in declined
            ):
                continue
            used.update(members)
            chosen.append(
                replace(candidate, name=name, target_folder_id=target, member_session_ids=members)
            )
            if len(chosen) >= free:
                break
        return chosen

    async def _withdraw(
        self,
        uow: RepositoryUnitOfWork,
        proposal: FolderProposal,
        reason: FolderWithdrawalReason,
    ) -> None:
        try:
            await uow.folders.resolve_proposal(
                proposal.id,
                self._principal,
                state=FolderProposalState.WITHDRAWN,
                resolved_at=self._clock.now(),
                withdrawal_reason=reason,
            )
        except ConflictError:
            return
        await record_folder_event(
            uow,
            event_type="folder.proposal.withdrawn",
            principal=self._principal,
            payload={
                "proposal_id": str(proposal.id),
                "kind": proposal.kind.value,
                "reason": reason.value,
                "members": len(proposal.member_session_ids),
            },
            key=str(proposal.id),
            clock=self._clock,
            ids=self._ids,
            actor_type="system",
        )

    async def _record_pass(
        self,
        uow: RepositoryUnitOfWork,
        attempt_id: UUID,
        *,
        withdrawn: int,
        candidates: int = 0,
        created: int = 0,
        outcome: GroupingOutcome | None = None,
    ) -> None:
        usage = outcome.usage if outcome is not None else None
        judgment = (outcome.judgment if outcome is not None else None) or JudgmentAudit()
        payload: dict[str, object] = {
            "attempt_id": str(attempt_id),
            "candidates": candidates,
            "created": created,
            "withdrawn": withdrawn,
            "provider": outcome.provider if outcome is not None else "none",
            "model": outcome.model if outcome is not None else "none",
            "fallback_used": outcome.fallback_used if outcome is not None else False,
            "error_class": outcome.error_class if outcome is not None else None,
            "input_tokens": usage.input_tokens if usage is not None else 0,
            "output_tokens": usage.output_tokens if usage is not None else 0,
            "cost": str(usage.cost) if usage is not None else "0",
            # Always present and content-free; they read none, zero, or false
            # when the judgment matcher did not run.
            "judgment_provider": judgment.provider,
            "judgment_model": judgment.model,
            "judgment_requests": judgment.requests,
            "judgment_matched": judgment.matched,
            "judgment_input_tokens": judgment.input_tokens,
            "judgment_cost": str(judgment.cost),
            "judgment_fallback_used": judgment.fallback_used,
            "judgment_error_class": judgment.error_class,
        }
        await record_folder_event(
            uow,
            event_type="folder.proposal.pass",
            principal=self._principal,
            payload=payload,
            key=str(attempt_id),
            clock=self._clock,
            ids=self._ids,
            actor_type="system",
        )
        # The same content-free facts on the service log, so a pass can be read
        # without a database query. `created` is reserved on a log record.
        logger.info(
            "folder_proposal_pass",
            extra={
                ("proposals_created" if key == "created" else key): value
                for key, value in payload.items()
            },
        )


__all__ = [
    "FOLDER_PROPOSAL_MAX_CANDIDATES",
    "FOLDER_PROPOSAL_NEAR_DUPLICATE",
    "FOLDER_PROPOSAL_SNIPPET_CHARS",
    "FolderProposalPass",
    "ThreadFolder",
]
