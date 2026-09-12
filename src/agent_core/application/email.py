"""The shared email experience application boundary.

HTTP and native clients read projections here. Only typed runtime tasks perform
remote work; email bodies never become fabricated owner messages.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from email.utils import getaddresses
from typing import Any, Literal, cast
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.application.session_service import bootstrap_session
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.approvals import ApprovalStatus
from agent_core.domain.context import TaskState, WorkingState
from agent_core.domain.email import (
    EMAIL_POLICY_VERSION,
    EMAIL_SLICE_RESERVATION,
    EmailAccount,
    EmailArchiveConsent,
    EmailArchiveOperation,
    EmailAttachment,
    EmailBudgetLimits,
    EmailDraft,
    EmailDraftEdit,
    EmailDraftStatus,
    EmailFeedback,
    EmailLearningState,
    EmailMessage,
    EmailOperation,
    EmailRecord,
    EmailTask,
    EmailThread,
    EmailValue,
    addresses,
    apply_feedback,
    archive_result_matches,
    feedback_matches,
)
from agent_core.domain.errors import (
    AuthorizationError,
    BudgetExceededError,
    ConflictError,
    NotFoundError,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryStatus,
    RecallQuery,
    Sensitivity,
)
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import TERMINAL_RUN_STATUSES, Run, RunStatus
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.email import EmailStore
from agent_core.ports.persistence import CheckpointSeeder, RepositoryUnitOfWork, UnitOfWorkFactory
from agent_core.ports.skills import SkillCatalog


async def records(store: EmailStore, principal: Principal, kind: str) -> AsyncIterator[EmailRecord]:
    after: str | None = None
    while page := await store.list(principal, kind, after=after):
        for row in page:
            yield row
        after = page[-1].key


async def task_records(
    store: EmailStore, principal: Principal, *, created_since: datetime | None = None
) -> AsyncIterator[EmailRecord]:
    """Read only unsettled tasks and the optional current accounting window."""
    after: str | None = None
    while page := await store.list_tasks(principal, created_since=created_since, after=after):
        for row in page:
            yield row
        after = page[-1].key


async def read_value[Value: EmailValue](
    store: EmailStore, principal: Principal, kind: str, key: str, model: type[Value]
) -> Value | None:
    row = await store.get(principal, kind, key)
    return None if row is None else model.model_validate(row.payload)


async def save_value(
    store: EmailStore,
    principal: Principal,
    kind: str,
    key: str,
    value: EmailValue,
    now: datetime,
) -> EmailRecord:
    """Caller holds the principal lock for read-modify-write sequences."""
    previous = await store.get(principal, kind, key)
    revision = 0 if previous is None else previous.revision
    return await store.put(
        EmailRecord(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            kind=kind,
            key=key,
            revision=revision + 1,
            payload=value.model_dump(mode="json"),
            created_at=now if previous is None else previous.created_at,
            updated_at=now,
        ),
        expected_revision=revision,
    )


def thread_summary(thread: EmailThread) -> dict[str, object]:
    return thread.model_dump(
        mode="json",
        exclude={
            "messages",
            "provider_thread_id",
            "source_fingerprint",
            "last_accessed_at",
        },
    )


def draft_body_fingerprint(body: str) -> str:
    """Ignore transport-only whitespace while retaining substantive draft lineage."""
    normalized = "\n".join(
        line.rstrip() for line in body.replace("\r\n", "\n").splitlines()
    ).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


class EmailExperienceService:
    """Coordinate owner-scoped mail projections, governed tasks, and feedback."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        account_ids: tuple[str, ...],
        budget_limits: EmailBudgetLimits,
        agent: AgentSpec,
        dispatch: Callable[[UUID], Awaitable[None]],
        seed_checkpoint: CheckpointSeeder,
        catalogs: SkillCatalog | None = None,
        activate_session: Callable[[UUID], Awaitable[None]] | None = None,
        close_session: Callable[[UUID], Awaitable[None]] | None = None,
        forget_source: Callable[[Principal, str, str, frozenset[str]], Awaitable[None]]
        | None = None,
        cleanup_artifacts: Callable[[], Awaitable[None]] | None = None,
        cancel_parked_run: Callable[[RepositoryUnitOfWork, Run, str], Awaitable[Run]] | None = None,
        resolve_archive_approval: Callable[[Principal, UUID, WorkerLease | None], Awaitable[None]]
        | None = None,
        archive_self_approval_enabled: bool = True,
        current_archive_principal: Callable[[], Principal] | None = None,
    ) -> None:
        """Wire ordinary run policy, account bindings, and finite email allowances."""
        self.uow_factory = uow_factory
        self.clock = clock
        self.ids = ids
        self.account_ids = account_ids
        self.budget_limits = budget_limits
        self.agent = agent
        self.dispatch = dispatch
        self.seed_checkpoint = seed_checkpoint
        self.catalogs = catalogs
        self.activate_session = activate_session
        self.close_session = close_session
        self.forget_source = forget_source
        self.cleanup_artifacts = cleanup_artifacts
        self.cancel_parked_run = cancel_parked_run
        self.resolve_archive_approval = resolve_archive_approval
        self.archive_self_approval_enabled = archive_self_approval_enabled
        self.current_archive_principal = current_archive_principal
        self.account_servers = {
            account_id: {
                mode: f"gmail_{mode}" if index == 0 else f"gmail_{account_id}_{mode}"
                for mode in ("read", "write", "send")
            }
            for index, account_id in enumerate(account_ids)
        }

    async def accounts(self, principal: Principal) -> dict[str, object]:
        require_scope(principal, "email.read")
        items: list[dict[str, object]] = []
        async with self.uow_factory() as uow:
            for account_id in self.account_ids:
                if f"mcp.{self.account_servers[account_id]['read']}.use" not in principal.scopes:
                    continue
                account = await read_value(
                    uow.email, principal, "account", account_id, EmailAccount
                )
                if account is None:
                    account = EmailAccount(
                        id=account_id, label=account_id.replace("_", " ").title()
                    )
                items.append(
                    {
                        **account.model_dump(mode="json"),
                        "read_server_id": self.account_servers[account_id]["read"],
                        "send_server_id": self.account_servers[account_id]["send"],
                        "write_server_id": self.account_servers[account_id].get("write"),
                        "archive_supported": self._archive_supported(principal, account_id),
                    }
                )
        return {"items": items, "next_cursor": None}

    async def _feedback(self, store: EmailStore, principal: Principal) -> list[EmailFeedback]:
        return [
            EmailFeedback.model_validate(row.payload)
            async for row in records(store, principal, "feedback")
        ]

    async def thread_record(
        self, store: EmailStore, principal: Principal, thread_id: UUID
    ) -> EmailThread:
        """Read an account-authorized thread through the caller's unit of work."""
        return await self._thread(store, principal, thread_id)

    async def _thread(
        self, store: EmailStore, principal: Principal, thread_id: UUID
    ) -> EmailThread:
        thread = await read_value(store, principal, "thread", str(thread_id), EmailThread)
        if thread is None:
            raise NotFoundError("email thread not found")
        self._authorize_account(principal, thread.account_id)
        return thread

    def _authorize_account(self, principal: Principal, account_id: str) -> None:
        if account_id not in self.account_servers:
            raise NotFoundError("email account is not available")
        require_scope(principal, f"mcp.{self.account_servers[account_id]['read']}.use")

    async def threads(
        self,
        principal: Principal,
        *,
        view: Literal["priority", "other", "all"] = "priority",
        account_id: str | None = None,
        text: str | None = None,
        cursor: str | None = None,
        limit: int = 5,
    ) -> dict[str, object]:
        require_scope(principal, "email.read")
        if not 1 <= limit <= 100 or (cursor is not None and not cursor.isdecimal()):
            raise ValueError("email page is malformed")
        async with self.uow_factory() as uow, uow.email.lock(principal):
            feedback = await self._feedback(uow.email, principal)
            threads = []
            async for row in records(uow.email, principal, "thread"):
                stored = EmailThread.model_validate(row.payload)
                if (
                    stored.account_id not in self.account_servers
                    or f"mcp.{self.account_servers[stored.account_id]['read']}.use"
                    not in principal.scopes
                ):
                    continue
                reconciled = await self._reconcile_archive_in(uow, principal, stored)
                threads.append(apply_feedback(reconciled, feedback))
        eligible: list[EmailThread] = []
        for thread in threads:
            if (
                thread.account_id not in self.account_servers
                or f"mcp.{self.account_servers[thread.account_id]['read']}.use"
                not in principal.scopes
            ):
                continue
            priority = (
                thread.in_inbox
                and thread.priority >= 0.7
                and thread.dismissed_revision != thread.revision
            )
            if (view == "priority" and not priority) or (view == "other" and priority):
                continue
            if account_id is not None and account_id != thread.account_id:
                continue
            if (
                text
                and text.casefold()
                not in (
                    thread.subject + " " + " ".join(thread.senders) + " " + thread.summary
                ).casefold()
            ):
                continue
            eligible.append(thread)
        eligible.sort(key=lambda item: (-item.priority, -item.updated_at.timestamp(), str(item.id)))
        start = int(cursor or "0")
        page = eligible[start : start + limit]
        return {
            "items": [thread_summary(thread) for thread in page],
            "next_cursor": str(start + limit) if start + limit < len(eligible) else None,
        }

    async def thread(self, principal: Principal, thread_id: UUID) -> dict[str, object]:
        require_scope(principal, "email.read")
        await self.expire_cache(principal)
        async with self.uow_factory() as uow, uow.email.lock(principal):
            thread = await self._thread(uow.email, principal, thread_id)
            thread = await self._reconcile_archive_in(uow, principal, thread)
            thread = thread.model_copy(update={"last_accessed_at": self.clock.now()})
            await save_value(
                uow.email, principal, "thread", str(thread.id), thread, self.clock.now()
            )
            selected = apply_feedback(thread, await self._feedback(uow.email, principal))
            result = selected.model_dump(mode="json")
            draft = (
                None
                if thread.draft_id is None
                else await read_value(
                    uow.email, principal, "draft", str(thread.draft_id), EmailDraft
                )
            )
            result["draft"] = None if draft is None else draft.model_dump(mode="json")
            return result

    async def feedback(
        self,
        principal: Principal,
        *,
        thread_id: UUID,
        target: Literal["thread", "person", "topic"],
        judgment: Literal["important", "less_important", "needs_reply", "no_reply_needed"],
        explanation: str | None = None,
        target_value: str | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        require_scope(principal, "email.write")
        now = self.clock.now()
        encoded = json.dumps(
            [str(thread_id), target, judgment, explanation, target_value, expected_revision]
        )
        request_digest = hashlib.sha256(encoded.encode()).hexdigest()
        async with self.uow_factory() as uow, uow.email.lock(principal):
            thread = await self._thread(uow.email, principal, thread_id)
            key = (
                None
                if idempotency_key is None
                else hashlib.sha256(idempotency_key.encode()).hexdigest()
            )
            replay = None if key is None else await uow.email.get(principal, "feedback_replay", key)
            if replay is not None:
                if replay.payload.get("request_digest") != request_digest:
                    raise ConflictError("idempotency key was used for different feedback")
                return {
                    "feedback_id": replay.payload["feedback_id"],
                    "thread": thread_summary(
                        apply_feedback(thread, await self._feedback(uow.email, principal))
                    ),
                }
            if expected_revision is not None and thread.revision != expected_revision:
                raise ConflictError("email thread changed")
            values = (
                [str(thread.id)]
                if target == "thread"
                else addresses(thread.senders)
                if target == "person"
                else thread.topics
            )
            if target_value is not None:
                target_value = addresses([target_value])[0] if target == "person" else target_value
                if target_value not in values:
                    raise ValueError("feedback target is not supported by this thread")
                values = [target_value]
            if len(values) != 1:
                raise ValueError("choose the specific person or topic for this feedback")
            item = EmailFeedback(
                id=self.ids.new_id(),
                thread_id=thread.id,
                target=target,
                judgment=judgment,
                target_values=values,
                explanation=explanation,
                created_at=now,
            )
            await save_value(uow.email, principal, "feedback", str(item.id), item, now)
            await self._bump_profile(uow.email, principal)
            if key is not None:
                await uow.email.put(
                    EmailRecord(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        kind="feedback_replay",
                        key=key,
                        revision=1,
                        payload={"feedback_id": str(item.id), "request_digest": request_digest},
                        created_at=now,
                        updated_at=now,
                    ),
                    expected_revision=0,
                )
            return {
                "feedback_id": str(item.id),
                "thread": thread_summary(
                    apply_feedback(thread, await self._feedback(uow.email, principal))
                ),
            }

    async def undo_feedback(self, principal: Principal, feedback_id: UUID) -> dict[str, object]:
        require_scope(principal, "email.write")
        async with self.uow_factory() as uow, uow.email.lock(principal):
            item = await read_value(
                uow.email, principal, "feedback", str(feedback_id), EmailFeedback
            )
            if item is None:
                raise NotFoundError("email feedback not found")
            if item.undone_at is None:
                await save_value(
                    uow.email,
                    principal,
                    "feedback",
                    str(item.id),
                    item.model_copy(update={"undone_at": self.clock.now()}),
                    self.clock.now(),
                )
                await self._bump_profile(uow.email, principal)
            thread = await self._thread(uow.email, principal, item.thread_id)
            return thread_summary(
                apply_feedback(thread, await self._feedback(uow.email, principal))
            )

    async def _draft(self, store: EmailStore, principal: Principal, draft_id: UUID) -> EmailDraft:
        draft = await read_value(store, principal, "draft", str(draft_id), EmailDraft)
        if draft is None:
            raise NotFoundError("email draft not found")
        self._authorize_account(principal, draft.account_id)
        return draft

    async def draft(self, principal: Principal, draft_id: UUID) -> EmailDraft:
        require_scope(principal, "email.read")
        await self.expire_cache(principal)
        async with self.uow_factory() as uow:
            return await self._draft(uow.email, principal, draft_id)

    async def _archive_draft(
        self, store: EmailStore, principal: Principal, draft: EmailDraft, *, conflict: bool = False
    ) -> None:
        key = f"{draft.id}:{draft.revision:012d}" + (
            f":conflict:{self.ids.new_id()}" if conflict else ""
        )
        if await store.get(principal, "draft_revision", key) is None:
            await save_value(store, principal, "draft_revision", key, draft, self.clock.now())
        if draft.body:
            await self._put_data(
                store,
                principal,
                "draft_lineage",
                f"{draft.id}:{draft_body_fingerprint(draft.body)}",
                {
                    "account_id": draft.account_id,
                    "thread_id": str(draft.thread_id),
                    "body_digest": draft_body_fingerprint(draft.body),
                },
            )

    async def edit_draft(
        self,
        principal: Principal,
        draft_id: UUID,
        edit: EmailDraftEdit,
        *,
        idempotency_key: str | None = None,
    ) -> EmailDraft:
        require_scope(principal, "email.write")
        if any(c in edit.subject for c in "\r\n\x00"):
            raise ValueError("subject contains a header delimiter")
        values = {
            "to": addresses(edit.to),
            "cc": addresses(edit.cc),
            "bcc": addresses(edit.bcc),
            "subject": edit.subject,
            "body": edit.body,
        }
        conflict = False
        replay_key = (
            None
            if idempotency_key is None
            else hashlib.sha256(idempotency_key.encode()).hexdigest()
        )
        digest = hashlib.sha256((str(draft_id) + edit.model_dump_json()).encode()).hexdigest()
        async with self.uow_factory() as uow, uow.email.lock(principal):
            current = await self._draft(uow.email, principal, draft_id)
            replay = (
                None
                if replay_key is None
                else await uow.email.get(principal, "draft_edit_replay", replay_key)
            )
            if replay is not None:
                if replay.payload["request_digest"] != digest:
                    raise ConflictError("idempotency key was used for a different draft edit")
                previous = await read_value(
                    uow.email,
                    principal,
                    "draft_revision",
                    str(replay.payload["revision_key"]),
                    EmailDraft,
                )
                if previous is None:
                    raise ConflictError("the previous draft revision is no longer retained")
                return previous
            if current.status in {
                EmailDraftStatus.SENDING,
                EmailDraftStatus.SENT,
                EmailDraftStatus.UNCERTAIN,
                EmailDraftStatus.DISCARDED,
            }:
                raise ConflictError("this draft cannot be edited in its current send state")
            thread = await self._thread(uow.email, principal, current.thread_id)
            if current.stale and edit.source_revision != thread.revision:
                raise ConflictError("review the changed thread before updating this draft")
            updated = current.model_copy(
                update={
                    **values,
                    "revision": current.revision + 1,
                    "source_revision": thread.revision,
                    "stale": False,
                    "owner_edited": True,
                    "status": EmailDraftStatus.READY,
                    "approval_id": None,
                    "run_id": None,
                    "updated_at": self.clock.now(),
                }
            )
            await self._archive_draft(uow.email, principal, current)
            conflict = current.revision != edit.expected_revision
            await self._archive_draft(uow.email, principal, updated, conflict=conflict)
            if not conflict:
                await self._cancel_obsolete_send(uow, principal, current, editing=True)
                await self._learn_owner_edit(uow.email, principal, current, updated)
                await save_value(
                    uow.email, principal, "draft", str(updated.id), updated, self.clock.now()
                )
                if replay_key is not None:
                    await self._put_data(
                        uow.email,
                        principal,
                        "draft_edit_replay",
                        replay_key,
                        {
                            "request_digest": digest,
                            "revision_key": f"{updated.id}:{updated.revision:012d}",
                        },
                    )
        if conflict:
            raise ConflictError(
                "draft changed on another device; both edits were preserved",
                reason="email_draft_revision_conflict",
            )
        return updated

    async def _learn_owner_edit(
        self,
        store: EmailStore,
        principal: Principal,
        previous: EmailDraft,
        current: EmailDraft,
    ) -> None:
        if (await self._learning_state(store, principal)).paused:
            return
        original, revised = previous.body.split(), current.body.split()
        changes = [
            " ".join(revised[j1:j2])
            for op, _, _, j1, j2 in SequenceMatcher(
                None, original, revised, autojunk=False
            ).get_opcodes()
            if op in {"insert", "replace"}
        ]
        excerpt = "\n".join(part for part in changes if len(part) >= 15).strip()[:2000]
        if not excerpt or not current.to:
            return
        await self._put_data(
            store,
            principal,
            "style",
            f"draft:{current.id}:{current.revision}",
            {
                "account_id": current.account_id,
                "thread_id": str(current.thread_id),
                "draft_id": str(current.id),
                "draft_revision": current.revision,
                "sent_at": self.clock.now().isoformat(),
                "recipients": current.to,
                "excerpt": excerpt,
                "authorship": "owner_edit_delta",
                "independent": True,
            },
        )
        await self._prune_styles(store, principal)
        await self._bump_profile(store, principal)

    async def _cancel_obsolete_send(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        draft: EmailDraft,
        *,
        editing: bool = False,
    ) -> None:
        if draft.run_id is None:
            return
        try:
            run = await uow.runs.get(draft.run_id, principal)
        except NotFoundError:
            return
        task = await read_value(uow.email, principal, "task", str(run.id), EmailTask)
        if task is None or task.kind != "send" or run.status in TERMINAL_RUN_STATUSES:
            return
        if run.status in {RunStatus.QUEUED, RunStatus.WAITING_FOR_APPROVAL}:
            if self.cancel_parked_run is None:
                raise ConflictError("send cancellation is unavailable")
            await self.cancel_parked_run(uow, run, principal.principal_id)
        elif editing:
            raise ConflictError("send review is running; retry the edit when review is ready")

    async def draft_revisions(self, principal: Principal, draft_id: UUID) -> dict[str, object]:
        require_scope(principal, "email.read")
        await self.expire_cache(principal)
        async with self.uow_factory() as uow:
            await self._draft(uow.email, principal, draft_id)
            revisions = [
                row.payload
                async for row in records(uow.email, principal, "draft_revision")
                if row.key.startswith(f"{draft_id}:")
            ]
        return {"items": revisions, "next_cursor": None}

    async def _session_in(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        thread: EmailThread | None,
    ) -> Session:
        if thread is not None and thread.session_id is not None:
            session = await uow.sessions.get(thread.session_id, principal)
            if (
                session.status is SessionStatus.ACTIVE
                and session.metadata.get("email_account_servers") == self.account_servers
            ):
                return session
            # Manifest/default changes select a fresh capability binding. Old
            # sessions remain intact so historical observations keep their identity.
        session_id, catalog = await bootstrap_session(
            uow, self.ids, self.catalogs, self.close_session, self.agent, principal
        )
        now = self.clock.now()
        metadata: dict[str, Any] = (
            {"email_operational": True}
            if thread is None
            else {"email_thread_id": str(thread.id), "email_account_id": thread.account_id}
        )
        metadata["email_account_servers"] = self.account_servers
        session = Session(
            id=session_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            agent_id=self.agent.id,
            agent_version=self.agent.version,
            status=SessionStatus.ACTIVE,
            title=None if thread is None else thread.subject[:64],
            metadata=metadata,
            created_at=now,
            updated_at=now,
        )
        await uow.sessions.create(session)
        await uow.events.append(
            NewEvent(
                session_id=session.id,
                run_id=None,
                event_type="session.created",
                payload_schema_version=2,
                actor_type="application",
                payload={
                    "agent_id": str(self.agent.id),
                    "title": session.title,
                    "skill_pins": []
                    if catalog is None
                    else [pin.model_dump(mode="json") for pin in catalog.pins],
                    "dropped_skills": [] if catalog is None else list(catalog.dropped_names),
                },
            )
        )
        if thread is not None:
            state = WorkingState(
                tasks=[
                    TaskState(
                        task_id=f"email:{thread.id}",
                        description=json.dumps(
                            {
                                "selected_email_thread_id": str(thread.id),
                                "account_id": thread.account_id,
                                "context_reader": "email.context",
                                "feedback_service": "email.feedback",
                            },
                            sort_keys=True,
                        ),
                        trust_level=TrustLevel.EXTERNAL_UNTRUSTED,
                        updated_at=now,
                    )
                ]
            )
            await uow.events.append(
                NewEvent(
                    session_id=session.id,
                    run_id=None,
                    event_type="context.working_state.updated",
                    actor_type="application",
                    payload={
                        "working_state": state.model_dump(mode="json"),
                        "source": "email_thread_binding",
                    },
                )
            )
            await save_value(
                uow.email,
                principal,
                "thread",
                str(thread.id),
                thread.model_copy(update={"session_id": session.id}),
                now,
            )
        return session

    async def _check_budget(self, store: EmailStore, principal: Principal, amount: Decimal) -> None:
        """Count settled usage and unresolved reservations against both windows."""
        now = self.clock.now().astimezone(UTC)
        daily = Decimal("0")
        monthly = Decimal("0")
        async for record in task_records(store, principal, created_since=now - timedelta(days=30)):
            task = EmailTask.model_validate(record.payload)
            cost = task.reservation if task.settled_cost is None else task.settled_cost
            if task.settled_cost is None or task.created_at.astimezone(UTC).date() == now.date():
                daily += cost
            if task.settled_cost is None or task.created_at >= now - timedelta(days=30):
                monthly += cost
        if (
            daily + amount > self.budget_limits.daily_cost
            or monthly + amount > self.budget_limits.monthly_cost
        ):
            raise BudgetExceededError(
                "email_aggregate_cost",
                "Automatic email work has reached its cost ceiling; "
                "cached mail and editing remain available.",
            )

    async def submit_task(
        self,
        principal: Principal,
        *,
        kind: Literal["refresh", "draft", "send", "archive"],
        thread_id: UUID | None = None,
        draft_id: UUID | None = None,
        expected_revision: int | None = None,
        instruction: str | None = None,
        idempotency_key: str | None = None,
        archived: bool | None = None,
    ) -> EmailOperation:
        """Authorize, coalesce, and reserve an email task before durable dispatch."""
        for scope in ("email.write", "run.write", "session.write"):
            require_scope(principal, scope)
        await self.expire_cache(principal)
        now = self.clock.now()
        async with self.uow_factory() as uow, uow.email.lock(principal):
            thread = (
                None if thread_id is None else await self._thread(uow.email, principal, thread_id)
            )
            draft = None if draft_id is None else await self._draft(uow.email, principal, draft_id)
            if draft is not None:
                thread = await self._thread(uow.email, principal, draft.thread_id)
                thread_id = thread.id
            accounts = (
                [
                    account
                    for account in self.account_ids
                    if account in self.account_servers
                    and f"mcp.{self.account_servers[account]['read']}.use" in principal.scopes
                ]
                if thread is None
                else [thread.account_id]
            )
            if not accounts or any(account not in self.account_servers for account in accounts):
                raise ConflictError("No configured email account is available for this operation")
            for account in accounts:
                require_scope(principal, f"mcp.{self.account_servers[account]['read']}.use")
                if kind == "send":
                    require_scope(principal, f"mcp.{self.account_servers[account]['send']}.use")
                if kind == "archive":
                    self._require_archive(principal, account)
            intent = [kind, str(thread_id), str(draft_id), expected_revision, instruction]
            if kind == "archive":
                intent.append(archived)
            digest = hashlib.sha256(json.dumps(intent).encode()).hexdigest()
            replay_key = (
                None
                if idempotency_key is None
                else hashlib.sha256(idempotency_key.encode()).hexdigest()
            )
            replay = (
                None
                if replay_key is None
                else await uow.email.get(principal, "task_replay", replay_key)
            )
            if replay is not None:
                if replay.payload["request_digest"] != digest:
                    raise ConflictError("idempotency key was used for a different email operation")
                old = await read_value(
                    uow.email, principal, "task", str(replay.payload["run_id"]), EmailTask
                )
                if old is None:
                    raise ConflictError("email operation has been erased")
                run = await uow.runs.get(old.run_id, principal)
                return EmailOperation(
                    operation_id=old.id,
                    run_id=old.run_id,
                    status=run.status.value,
                    replayed=True,
                )
            if kind == "archive":
                if (
                    thread is None
                    or type(archived) is not bool
                    or expected_revision != thread.revision
                ):
                    raise ConflictError("the thread changed; review it before changing Inbox state")
                thread = await self._reconcile_archive_in(uow, principal, thread)
                previous_archive = thread.archive_operation
                if previous_archive is not None:
                    if previous_archive.status == "uncertain":
                        raise ConflictError(
                            "check Gmail before retrying an uncertain archive operation"
                        )
                    if previous_archive.status == "pending":
                        if previous_archive.target_archived != archived:
                            raise ConflictError("wait for the current Inbox operation to finish")
                        prior_run = await uow.runs.get(previous_archive.run_id, principal)
                        if replay_key is not None:
                            await self._archive_replay_in(
                                uow.email, principal, replay_key, digest, prior_run.id, now
                            )
                        return EmailOperation(
                            operation_id=previous_archive.operation_id,
                            run_id=prior_run.id,
                            status=prior_run.status.value,
                            replayed=True,
                        )
            async for row in task_records(uow.email, principal):
                old = EmailTask.model_validate(row.payload)
                try:
                    run = await uow.runs.get(old.run_id, principal)
                except NotFoundError:
                    # Missing accounting evidence never releases a reservation.
                    continue
                if run.status in TERMINAL_RUN_STATUSES:
                    await self._settle_task_in(uow, principal, old, run)
                    continue
                if old.kind != kind or (
                    kind != "refresh" and (old.thread_id != thread_id or old.draft_id != draft_id)
                ):
                    continue
                if kind == "send" and old.expected_revision != expected_revision:
                    raise ConflictError("another draft revision already has a send operation")
                if kind == "archive" and replay_key is not None:
                    await self._archive_replay_in(
                        uow.email, principal, replay_key, digest, old.run_id, now
                    )
                return EmailOperation(
                    operation_id=old.id,
                    run_id=old.run_id,
                    status=run.status.value,
                    replayed=True,
                )
            if draft is not None:
                assert thread is not None
                if (
                    draft.revision != expected_revision
                    or draft.stale
                    or draft.source_revision != thread.revision
                ):
                    raise ConflictError("review the latest draft and thread before sending")
                if draft.status in {
                    EmailDraftStatus.SENDING,
                    EmailDraftStatus.SENT,
                    EmailDraftStatus.UNCERTAIN,
                    EmailDraftStatus.DISCARDED,
                }:
                    raise ConflictError("this draft cannot be sent in its current state")
            reservation = (
                Decimal("0")
                if kind in {"send", "archive"}
                else min(
                    EMAIL_SLICE_RESERVATION,
                    self.agent.limits.max_cost or EMAIL_SLICE_RESERVATION,
                )
            )
            if kind != "archive":
                await self._check_budget(uow.email, principal, reservation)
            session = await self._session_in(uow, principal, None if kind == "archive" else thread)
            if await uow.runs.active_for_session(session.id, principal) is not None:
                raise ConflictError("the thread conversation has an active run")
            deadline = (
                self.agent.limits.deadline_at
                if kind == "send"
                else min(
                    now + timedelta(seconds=120),
                    self.agent.limits.deadline_at or now + timedelta(seconds=120),
                )
            )
            limits = self.agent.limits.model_copy(
                deep=True,
                update={
                    "max_cost": reservation if reservation else self.agent.limits.max_cost,
                    "deadline_at": deadline,
                },
            )
            run = Run(
                id=self.ids.new_id(),
                session_id=session.id,
                tenant_id=principal.tenant_id,
                principal_scopes=set(principal.scopes),
                agent_id=session.agent_id,
                agent_version=session.agent_version,
                status=RunStatus.QUEUED,
                limits=limits,
                priority=10,
                scheduled_for=now,
                deadline_at=deadline,
                created_at=now,
                updated_at=now,
            )
            if uow.queue is None:
                await uow.runs.create(run)
            else:
                await uow.queue.enqueue(run, priority=run.priority, scheduled_for=now)
            task = EmailTask(
                id=self.ids.new_id(),
                run_id=run.id,
                session_id=session.id,
                kind=kind,
                account_ids=accounts,
                thread_id=thread_id,
                draft_id=draft_id,
                expected_revision=expected_revision,
                instruction=instruction,
                created_at=now,
                reservation=reservation,
                archive_consent=(
                    EmailArchiveConsent(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        account_id=thread.account_id,
                        provider_thread_id=thread.provider_thread_id,
                        read_server_id=self.account_servers[thread.account_id]["read"],
                        write_server_id=self.account_servers[thread.account_id]["write"],
                        expected_revision=thread.revision,
                        archived=archived,
                        expires_at=now + timedelta(seconds=120),
                    )
                    if kind == "archive" and thread is not None and archived is not None
                    else None
                ),
            )
            await save_value(uow.email, principal, "task", str(run.id), task, now)
            if task.archive_consent is not None and thread is not None:
                await save_value(
                    uow.email,
                    principal,
                    "thread",
                    str(thread.id),
                    thread.model_copy(
                        update={
                            "archive_operation": EmailArchiveOperation(
                                operation_id=task.id,
                                run_id=run.id,
                                target_archived=task.archive_consent.archived,
                            )
                        }
                    ),
                    now,
                )
            if draft is not None:
                await save_value(
                    uow.email,
                    principal,
                    "draft",
                    str(draft.id),
                    draft.model_copy(
                        update={
                            "run_id": run.id,
                            "session_id": session.id,
                            "status": EmailDraftStatus.GENERATING
                            if kind == "draft"
                            else EmailDraftStatus.AWAITING_APPROVAL,
                        }
                    ),
                    now,
                )
            if replay_key is not None:
                await uow.email.put(
                    EmailRecord(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        kind="task_replay",
                        key=replay_key,
                        revision=1,
                        payload={"run_id": str(run.id), "request_digest": digest},
                        created_at=now,
                        updated_at=now,
                    ),
                    expected_revision=0,
                )
            event = await uow.events.append(
                NewEvent(
                    session_id=session.id,
                    run_id=run.id,
                    event_type="email.task.requested",
                    actor_type="application",
                    payload={"task_id": str(task.id), "kind": kind},
                    derivation_key=f"email-task:{task.id}",
                )
            )
            await uow.runs.set_seed_event_sequence(run.id, event.sequence)
            if task.archive_consent is not None:
                await uow.events.append(
                    NewEvent(
                        session_id=session.id,
                        run_id=run.id,
                        event_type="email.archive.requested",
                        actor_type="principal",
                        actor_id=principal.principal_id,
                        payload={
                            "task_id": str(task.id),
                            "thread_id": str(thread_id),
                            "archived": task.archive_consent.archived,
                            "consent_digest": hashlib.sha256(
                                task.archive_consent.model_dump_json().encode()
                            ).hexdigest(),
                        },
                        derivation_key=f"email-archive:{task.id}",
                    )
                )
            await uow.events.append(
                NewEvent(
                    session_id=session.id,
                    run_id=run.id,
                    event_type="run.queued",
                    actor_type="application",
                    payload={"run_id": str(run.id), "priority": run.priority},
                )
            )
            await self.seed_checkpoint(uow, run, event.sequence, None, principal)
        if self.activate_session is not None:
            await self.activate_session(session.id)
        if kind in {"refresh", "archive"}:
            # Admission has pinned the ordinary catalog. The worker opens its
            # own transports; keeping the API's copies leaks a process roster
            # for every foreground poll.
            await self._release_refresh_session(session.id)
        await self.dispatch(run.id)
        async with self.uow_factory() as uow:
            final_run = await uow.runs.get(run.id, principal)
        return EmailOperation(operation_id=task.id, run_id=run.id, status=final_run.status.value)

    async def operation(self, principal: Principal, operation_id: UUID) -> EmailOperation:
        require_scope(principal, "email.read")
        require_scope(principal, "run.read")
        async with self.uow_factory() as uow:
            async for row in records(uow.email, principal, "task"):
                task = EmailTask.model_validate(row.payload)
                if task.id == operation_id:
                    run = await uow.runs.get(task.run_id, principal)
                    return EmailOperation(
                        operation_id=task.id, run_id=task.run_id, status=run.status.value
                    )
        raise NotFoundError("email operation not found")

    def _archive_supported(self, principal: Principal, account_id: str) -> bool:
        """Advertise only a configured action this principal can explicitly approve."""
        server = self.account_servers.get(account_id, {}).get("write")
        return bool(
            self.resolve_archive_approval is not None
            and self.archive_self_approval_enabled
            and server is not None
            and {
                "email.read",
                "email.write",
                "run.write",
                "session.write",
                "approval.resolve",
                f"mcp.{server}.use",
            }.issubset(principal.scopes)
        )

    async def _archive_replay_in(
        self,
        store: EmailStore,
        principal: Principal,
        key: str,
        digest: str,
        run_id: UUID,
        now: datetime,
    ) -> None:
        """Bind every accepted coalesced gesture key before returning its existing action."""
        await store.put(
            EmailRecord(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                kind="task_replay",
                key=key,
                revision=1,
                payload={"run_id": str(run_id), "request_digest": digest},
                created_at=now,
                updated_at=now,
            ),
            expected_revision=0,
        )

    def _require_archive(self, principal: Principal, account_id: str) -> None:
        """Preflight current authority before storing an owner consent or queue row."""
        for scope in (
            "email.read",
            "email.write",
            "run.write",
            "session.write",
            "approval.resolve",
        ):
            require_scope(principal, scope)
        self._authorize_account(principal, account_id)
        server = self.account_servers.get(account_id, {}).get("write")
        if server is None or self.resolve_archive_approval is None:
            raise ConflictError("Gmail archive is unavailable for this account")
        require_scope(principal, f"mcp.{server}.use")
        if not self.archive_self_approval_enabled:
            raise AuthorizationError("approval requires a distinct resolver")

    async def archive(
        self,
        principal: Principal,
        thread_id: UUID,
        expected_revision: int,
        *,
        archived: bool,
        idempotency_key: str,
    ) -> EmailOperation:
        """Admit exactly one explicit Inbox transition; legacy dismiss remains local."""
        if type(archived) is not bool or not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("invalid archive request")
        return await self.submit_task(
            principal,
            kind="archive",
            thread_id=thread_id,
            expected_revision=expected_revision,
            archived=archived,
            idempotency_key=idempotency_key,
        )

    async def validate_archive(
        self,
        principal: Principal,
        run: Run,
        lease: WorkerLease | None,
    ) -> EmailTask:
        """Revalidate immutable consent immediately before each undispatched attempt."""
        principal = self._archive_authority(principal)
        async with self.uow_factory() as uow, uow.email.lock(principal):
            await self._fence(uow, run, lease)
            task = await read_value(uow.email, principal, "task", str(run.id), EmailTask)
            if task is None or task.kind != "archive" or task.thread_id is None:
                raise ConflictError("archive request is unavailable")
            consent = task.archive_consent
            if consent is None or (consent.tenant_id, consent.principal_id) != (
                principal.tenant_id,
                principal.principal_id,
            ):
                raise ConflictError("archive request does not belong to this owner")
            self._require_archive(principal, consent.account_id)
            thread = await self._thread(uow.email, principal, task.thread_id)
            if (
                task.session_id != run.session_id
                or task.account_ids != [consent.account_id]
                or task.expected_revision != consent.expected_revision
                or consent.expires_at <= self.clock.now()
                or thread.revision != consent.expected_revision
                or thread.account_id != consent.account_id
                or thread.provider_thread_id != consent.provider_thread_id
                or self.account_servers[consent.account_id].get("read") != consent.read_server_id
                or self.account_servers[consent.account_id].get("write") != consent.write_server_id
                or thread.archive_operation is None
                or thread.archive_operation.run_id != run.id
            ):
                raise ConflictError("archive request expired or the source changed")
            session = await uow.sessions.get(run.session_id, principal)
            if session.metadata.get("email_account_servers") != self.account_servers:
                raise ConflictError("archive account configuration changed")
            event = await uow.events.get_by_derivation(f"email-archive:{task.id}", principal)
            if (
                event is None
                or event.run_id != run.id
                or event.actor_type != "principal"
                or event.actor_id != principal.principal_id
                or event.payload
                != {
                    "task_id": str(task.id),
                    "thread_id": str(task.thread_id),
                    "archived": consent.archived,
                    "consent_digest": hashlib.sha256(
                        consent.model_dump_json().encode()
                    ).hexdigest(),
                }
            ):
                raise ConflictError("archive consent has no matching authenticated gesture")
            for invocation in await uow.invocations.list_for_run(run.id, principal):
                if not invocation.tool_name.endswith(".modify_labels"):
                    continue
                approval = await uow.approvals.get_by_action(invocation.id)
                if (
                    invocation.tool_name != consent.tool_name
                    or invocation.normalized_arguments != consent.arguments
                    or (
                        approval is not None
                        and (approval.expires_at is None or approval.expires_at <= self.clock.now())
                    )
                ):
                    raise ConflictError("the frozen archive invocation expired or changed")
            return task

    def _archive_authority(self, principal: Principal) -> Principal:
        """Intersect admission scopes with the current configured owner authority."""
        if self.current_archive_principal is None:
            raise AuthorizationError("current archive authority is unavailable")
        current = self.current_archive_principal()
        if (current.tenant_id, current.principal_id) != (
            principal.tenant_id,
            principal.principal_id,
        ):
            raise AuthorizationError("archive owner authority changed")
        return principal.model_copy(
            update={
                "scopes": principal.scopes & current.scopes,
                "roles": principal.roles & current.roles,
            }
        )

    async def approve_archive(
        self,
        principal: Principal,
        run: Run,
        lease: WorkerLease | None,
        approval_id: UUID,
    ) -> None:
        """Consume the authenticated gesture through ordinary one-time approval resolution."""
        task = await self.validate_archive(principal, run, lease)
        consent = task.archive_consent
        assert consent is not None
        async with self.uow_factory() as uow, uow.email.lock(principal):
            await self._fence(uow, run, lease)
            approval = await uow.approvals.get(approval_id, principal)
            invocations = await uow.invocations.list_for_run(run.id, principal)
            expected_call = "email-" + hashlib.sha256(f"{run.id}:archive".encode()).hexdigest()[:32]
            invocation = next((i for i in invocations if i.id == approval.tool_invocation_id), None)
            if (
                approval.run_id != run.id
                or approval.session_id != run.session_id
                or approval.tool_name != consent.tool_name
                or approval.status not in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
                or approval.expires_at is None
                or approval.expires_at <= self.clock.now()
                or invocation is None
                or invocation.call_id != expected_call
                or invocation.tool_name != consent.tool_name
                or invocation.normalized_arguments != consent.arguments
                or approval.arguments != consent.arguments
                or approval.normalized_arguments_hash != invocation.normalized_arguments_hash
            ):
                raise ConflictError("approval does not match the requested archive action")
            await uow.events.append(
                NewEvent(
                    session_id=run.session_id,
                    run_id=run.id,
                    event_type="approval.requested",
                    actor_type="runtime",
                    payload={"approval_id": str(approval.id)},
                    derivation_key=f"email-archive-approval:{approval.id}",
                ),
                lease=lease,
            )
        assert self.resolve_archive_approval is not None
        await self.resolve_archive_approval(self._archive_authority(principal), approval_id, lease)

    async def finish_archive(
        self,
        principal: Principal,
        run: Run,
        lease: WorkerLease | None,
        *,
        status: Literal["completed", "failed", "uncertain"],
    ) -> None:
        """Project a confirmed result without overwriting newer source material."""
        async with self.uow_factory() as uow, uow.email.lock(principal):
            await self._fence(uow, run, lease)
            task = await read_value(uow.email, principal, "task", str(run.id), EmailTask)
            if task is None or task.thread_id is None:
                return
            thread = await read_value(
                uow.email, principal, "thread", str(task.thread_id), EmailThread
            )
            if (
                thread is None
                or thread.archive_operation is None
                or thread.archive_operation.run_id != run.id
            ):
                return
            if status == "failed":
                await uow.approvals.cancel_for_run(run.id)
            await self._archive_result_in(uow, principal, thread, task, status)

    async def _archive_result_in(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        thread: EmailThread,
        task: EmailTask,
        status: Literal["completed", "failed", "uncertain"],
    ) -> EmailThread:
        """Retain safe operation status; only confirmed same-revision writes affect labels."""
        operation = thread.archive_operation
        assert operation is not None
        error = (
            "The outcome is uncertain. Check Gmail before another change."
            if status == "uncertain"
            else "Inbox state could not be changed. Try again."
            if status == "failed"
            else None
        )
        update: dict[str, Any] = {
            "archive_operation": operation.model_copy(update={"status": status, "error": error})
        }
        if status == "completed" and thread.revision == task.expected_revision:
            archived = operation.target_archived
            update["in_inbox"] = not archived
            update["messages"] = [
                m.model_copy(
                    update={
                        "labels": [label for label in m.labels if label != "INBOX"]
                        if archived
                        else list(dict.fromkeys([*m.labels, "INBOX"]))
                    }
                )
                for m in thread.messages
            ]
        thread = thread.model_copy(update=update)
        await save_value(uow.email, principal, "thread", str(thread.id), thread, self.clock.now())
        return thread

    async def _reconcile_archive_in(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        thread: EmailThread,
    ) -> EmailThread:
        """Recover terminal/crashed operation display from durable invocation evidence."""
        operation = thread.archive_operation
        if operation is None or operation.status != "pending":
            return thread
        try:
            run = await uow.runs.get(operation.run_id, principal)
        except NotFoundError:
            return thread
        if run.status not in TERMINAL_RUN_STATUSES:
            return thread
        task = await read_value(uow.email, principal, "task", str(run.id), EmailTask)
        if task is None:
            return thread
        writes = [
            i
            for i in await uow.invocations.list_for_run(run.id, principal)
            if i.tool_name.endswith(".modify_labels")
        ]
        status: Literal["completed", "failed", "uncertain"] = "failed"
        if task.archive_consent is not None and any(
            i.status is ToolInvocationStatus.SUCCEEDED
            and i.call_id
            == "email-" + hashlib.sha256(f"{run.id}:archive".encode()).hexdigest()[:32]
            and i.tool_name == task.archive_consent.tool_name
            and i.normalized_arguments == task.archive_consent.arguments
            and archive_result_matches(task.archive_consent, i.structured_result)
            for i in writes
        ):
            status = "completed"
        elif any(
            i.status is ToolInvocationStatus.UNCERTAIN
            or i.status is ToolInvocationStatus.SUCCEEDED
            or (i.status is ToolInvocationStatus.RUNNING and i.effect_sent_at is not None)
            for i in writes
        ):
            status = "uncertain"
        return await self._archive_result_in(uow, principal, thread, task, status)

    async def get_task(self, principal: Principal, run_id: UUID) -> EmailTask | None:
        async with self.uow_factory() as uow:
            return await read_value(uow.email, principal, "task", str(run_id), EmailTask)

    async def save_task(self, principal: Principal, task: EmailTask) -> None:
        async with self.uow_factory() as uow, uow.email.lock(principal):
            await save_value(uow.email, principal, "task", str(task.run_id), task, self.clock.now())

    async def settle(self, principal: Principal, run_id: UUID) -> None:
        """Release terminal refresh resources and settle only provable usage."""
        task = await self.get_task(principal, run_id)
        if task is not None and task.kind in {"refresh", "archive"}:
            async with self.uow_factory() as uow:
                run = await uow.runs.get(run_id, principal)
            if run.status in TERMINAL_RUN_STATUSES:
                # Release even when provider usage still needs reconciliation.
                # Durable session/events and all source receipts remain intact.
                await self._release_refresh_session(task.session_id)
        async with self.uow_factory() as uow, uow.email.lock(principal):
            task = await read_value(uow.email, principal, "task", str(run_id), EmailTask)
            if task is None or task.settled_cost is not None:
                return
            run = await uow.runs.get(run_id, principal)
            if run.status in TERMINAL_RUN_STATUSES:
                await self._settle_task_in(uow, principal, task, run)

    async def _settle_task_in(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        task: EmailTask,
        run: Run,
    ) -> None:
        """Reconcile a terminal task while the caller holds the principal lock."""
        events = await uow.events.list_after(run.session_id, 0, principal, run_id=run.id)
        started: set[UUID] = set()
        observed: set[UUID] = set()
        known: set[UUID] = set()
        malformed = False
        for event in events:
            if event.event_type not in {
                "model.request.started",
                "model.response.completed",
                "model.response.failed",
            }:
                continue
            raw_attempt_id = event.payload.get("attempt_id")
            if not isinstance(raw_attempt_id, str):
                malformed = True
                continue
            try:
                attempt_id = UUID(raw_attempt_id)
            except ValueError:
                malformed = True
                continue
            observed.add(attempt_id)
            if event.event_type == "model.request.started":
                started.add(attempt_id)
            elif event.event_type == "model.response.completed" or (
                event.payload.get("error_class") == "ModelPermanentError"
                and event.payload.get("http_status") == 400
                and event.payload.get("provider_code") == "invalid_json_schema"
            ):
                known.add(attempt_id)
        # Terminal events precede durable usage. Unknown failures can record a
        # synthetic zero, so both accounting and a proven outcome are required.
        if (
            malformed
            or observed != started
            or started - known
            or (run.usage.model_calls != len(started))
        ):
            if task.stage == "cost_reconciliation_required":
                return
            updated = task.model_copy(update={"stage": "cost_reconciliation_required"})
        else:
            updated = task.model_copy(
                update={"settled_cost": run.usage.cost, "stage": run.status.value.lower()}
            )
        await save_value(uow.email, principal, "task", str(run.id), updated, self.clock.now())

    async def _release_refresh_session(self, session_id: UUID) -> None:
        """Release ephemeral refresh resources while preserving durable evidence."""
        try:
            if self.close_session is not None:
                await self.close_session(session_id)
        finally:
            if self.catalogs is not None:
                await self.catalogs.discard(session_id)

    async def _learning_state(self, store: EmailStore, principal: Principal) -> EmailLearningState:
        return (
            await read_value(store, principal, "learning", "shared", EmailLearningState)
            or EmailLearningState()
        )

    async def _bump_profile(self, store: EmailStore, principal: Principal) -> EmailLearningState:
        state = await self._learning_state(store, principal)
        state = state.model_copy(update={"profile_revision": state.profile_revision + 1})
        await save_value(store, principal, "learning", "shared", state, self.clock.now())
        return state

    async def learning(self, principal: Principal) -> EmailLearningState:
        require_scope(principal, "email.read")
        return await self._learning_summary(principal)

    async def _learning_summary(self, principal: Principal) -> EmailLearningState:
        async with self.uow_factory() as uow:
            state = await self._learning_state(uow.email, principal)
            return await self._summarize_learning(uow.email, principal, state)

    async def _summarize_learning(
        self, store: EmailStore, principal: Principal, state: EmailLearningState
    ) -> EmailLearningState:
        examples = [row async for row in records(store, principal, "style")]
        accounts = [
            EmailAccount.model_validate(row.payload)
            async for row in records(store, principal, "account")
        ]
        excluded = [row async for row in records(store, principal, "excluded_source")]
        return state.model_copy(
            update={
                "style_examples": len(examples),
                "excluded_sources": len(excluded),
                "history_processed": sum(account.history_processed for account in accounts),
                "history_complete": bool(accounts)
                and all(account.history_complete for account in accounts),
            }
        )

    async def pause_learning(self, principal: Principal, paused: bool) -> EmailLearningState:
        require_scope(principal, "email.write")
        async with self.uow_factory() as uow, uow.email.lock(principal):
            state = await self._learning_state(uow.email, principal)
            state = state.model_copy(update={"paused": paused})
            await save_value(uow.email, principal, "learning", "shared", state, self.clock.now())
        return await self._learning_summary(principal)

    async def reset_learning(
        self, principal: Principal, scope: Literal["preferences", "style", "all"]
    ) -> EmailLearningState:
        require_scope(principal, "email.write")
        async with self.uow_factory() as uow, uow.email.lock(principal):
            kinds = (
                ["style"]
                if scope == "style"
                else ["feedback", "relationship"]
                if scope == "preferences"
                else ["style", "feedback", "relationship"]
            )
            for kind in kinds:
                for row in [row async for row in records(uow.email, principal, kind)]:
                    await uow.email.delete(principal, kind, row.key, expected_revision=row.revision)
            # A watermark prevents resetting historical learning from immediately
            # relearning the same evidence on the next unchanged refresh.
            for kind in (
                ["style_reset"]
                if scope == "style"
                else ["preferences_reset"]
                if scope == "preferences"
                else ["style_reset", "preferences_reset"]
            ):
                await self._put_data(
                    uow.email,
                    principal,
                    kind,
                    "shared",
                    {"reset_at": self.clock.now().isoformat()},
                )
            await self._bump_profile(uow.email, principal)
        return await self._learning_summary(principal)

    async def _put_data(
        self,
        store: EmailStore,
        principal: Principal,
        kind: str,
        key: str,
        payload: dict[str, object],
    ) -> None:
        old = await store.get(principal, kind, key)
        now = self.clock.now()
        await store.put(
            EmailRecord(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                kind=kind,
                key=key,
                revision=1 if old is None else old.revision + 1,
                payload=payload,
                created_at=now if old is None else old.created_at,
                updated_at=now,
            ),
            expected_revision=0 if old is None else old.revision,
        )

    async def learning_context(
        self, principal: Principal, thread: EmailThread | None
    ) -> dict[str, object]:
        async with self.uow_factory() as uow:
            state = await self._learning_state(uow.email, principal)
            feedback = await self._feedback(uow.email, principal)
            examples = [
                row.payload
                async for row in records(uow.email, principal, "style")
                if str(row.payload.get("account_id")) in self.account_servers
                and f"mcp.{self.account_servers[str(row.payload['account_id'])]['read']}.use"
                in principal.scopes
            ]
            relationships = [
                row.payload async for row in records(uow.email, principal, "relationship")
            ]
            shared_memories = (
                [] if thread is None else await self._relationship_evidence(uow, principal, thread)
            )
        correspondents = set() if thread is None else set(addresses(thread.senders))
        weighted_counts: dict[str, float] = {}
        now = self.clock.now()
        for evidence in relationships:
            occurred = datetime.fromisoformat(str(evidence["sent_at"]))
            weight = 0.5 ** (max(0, (now - occurred).total_seconds()) / (180 * 86400))
            for recipient in cast(list[str], evidence["recipients"]):
                if isinstance(recipient, str) and recipient in correspondents:
                    weighted_counts[recipient] = weighted_counts.get(recipient, 0) + weight
        examples.sort(
            key=lambda item: (
                bool(correspondents & set(cast(list[str], item.get("recipients", [])))),
                item.get("authorship") in {"owner_endorsed", "owner_edit_delta"},
                str(item["sent_at"]),
            ),
            reverse=True,
        )
        return {
            "profile_revision": state.profile_revision,
            "paused": state.paused,
            "owner_feedback": [
                {**item.model_dump(mode="json"), "explanation": (item.explanation or "")[:1000]}
                for item in sorted(feedback, key=lambda value: (value.created_at, str(value.id)))
                if thread is not None and item.undone_at is None and feedback_matches(item, thread)
            ][-12:],
            "reply_partner_counts": weighted_counts,
            "shared_memories": [
                {
                    **item,
                    "statement": str(item["statement"])[:1000],
                    "truncated": len(str(item["statement"])) > 1000,
                }
                for item in shared_memories[:5]
            ],
            "style_examples": examples[:5],
            "style_summary": (
                "Learn phrasing and structure from these attributed examples; "
                "facts and decisions remain specific to their source thread."
            ),
        }

    async def _archive_observed_in(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        thread: EmailThread,
        normalized: dict[str, object],
        source_session_id: UUID,
        run: Run | None,
    ) -> EmailArchiveOperation | None:
        """Settle uncertainty only from complete, later provider receipts in this refresh."""
        operation = thread.archive_operation
        if operation is None or operation.status != "uncertain" or run is None:
            return operation
        if source_session_id != run.session_id or not normalized.get("complete"):
            return operation
        read_server = self.account_servers[thread.account_id]["read"]
        if not {"email.read", f"mcp.{read_server}.use"} <= principal.scopes:
            return operation
        task = await read_value(uow.email, principal, "task", str(run.id), EmailTask)
        session = await uow.sessions.get(run.session_id, principal)
        if (
            task is None
            or task.kind != "refresh"
            or task.session_id != run.session_id
            or thread.account_id not in task.account_ids
            or session.metadata.get("email_operational") is not True
            or session.metadata.get("email_account_servers") != self.account_servers
        ):
            return operation
        prior_task = await read_value(
            uow.email, principal, "task", str(operation.run_id), EmailTask
        )
        consent = None if prior_task is None else prior_task.archive_consent
        if (
            prior_task is None
            or prior_task.kind != "archive"
            or consent is None
            or prior_task.id != operation.operation_id
            or prior_task.thread_id != thread.id
            or consent.tenant_id != principal.tenant_id
            or consent.principal_id != principal.principal_id
            or consent.account_id != thread.account_id
            or consent.provider_thread_id != thread.provider_thread_id
            or consent.archived != operation.target_archived
        ):
            return operation
        try:
            prior_run = await uow.runs.get(operation.run_id, principal)
        except NotFoundError:
            return operation
        if prior_run.status not in TERMINAL_RUN_STATUSES:
            return operation
        call_id = "email-" + hashlib.sha256(f"{operation.run_id}:archive".encode()).hexdigest()[:32]
        writes = [
            item
            for item in await uow.invocations.list_for_run(operation.run_id, principal)
            if item.call_id == call_id
            and item.tool_name == consent.tool_name
            and item.normalized_arguments == consent.arguments
            and item.effect_sent_at is not None
        ]
        if len(writes) != 1:
            return operation
        # Starting after the final write receipt excludes reads already in flight
        # when Gmail accepted (or may have accepted) the mutation.
        written_at = max(writes[0].updated_at, cast(datetime, writes[0].effect_sent_at))
        reads = {
            item.call_id: item for item in await uow.invocations.list_for_run(run.id, principal)
        }
        events = {
            event.sequence: event
            for event in await uow.events.list_after(
                run.session_id, 0, principal, run_id=run.id, created_at_or_after=written_at
            )
            if event.event_type == "tool.call.completed"
        }
        raw_messages = normalized.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            return operation
        labels: dict[str, list[str]] = {}
        for raw in raw_messages:
            if not isinstance(raw, dict) or raw.get("_header_session_id") != str(run.session_id):
                return operation
            sequence = raw.get("_header_event_sequence")
            event = events.get(sequence) if type(sequence) is int else None
            read = None if event is None else reads.get(str(event.payload.get("call_id")))
            page = None if read is None else read.structured_result
            if (
                read is None
                or event is None
                or read.status is not ToolInvocationStatus.SUCCEEDED
                or read.session_id != run.session_id
                or read.created_at <= written_at
                or event.created_at <= written_at
                or read.server_id != read_server
                or read.tool_name != f"mcp.{read_server}.get_thread_page"
                or event.payload.get("name") != read.tool_name
                or (read.normalized_arguments or {}).get("thread_id") != thread.provider_thread_id
                or page is None
                or page.get("source_changed")
                or page.get("thread_id") != thread.provider_thread_id
                or page.get("history_id") != normalized.get("history_id")
                or type(page.get("total_messages")) is not int
                or page.get("total_messages") != len(raw_messages)
                or not isinstance(page.get("messages"), list)
            ):
                return operation
            matching = [
                item
                for item in page["messages"]
                if isinstance(item, dict) and item.get("id") == raw.get("id")
            ]
            if (
                len(matching) != 1
                or matching[0].get("thread_id") != thread.provider_thread_id
                or matching[0].get("label_ids") != raw.get("label_ids")
                or not isinstance(raw.get("label_ids"), list)
                or not all(isinstance(label, str) for label in raw["label_ids"])
                or raw.get("id") in labels
            ):
                return operation
            labels[str(raw["id"])] = raw["label_ids"]
        observed_inbox = any("INBOX" in value for value in labels.values())
        if observed_inbox == operation.target_archived:
            return operation
        return operation.model_copy(update={"status": "completed", "error": None})

    async def import_thread(
        self,
        principal: Principal,
        account_id: str,
        normalized: dict[str, object],
        source_session_id: UUID,
        *,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> EmailThread:
        """Persist one fully identified provider observation; never synthesize evidence time."""
        if account_id not in self.account_servers or normalized.get("source_changed"):
            raise ConflictError("email source changed or account is unavailable")
        provider_id = str(normalized.get("thread_id", ""))
        raw_messages = normalized.get("messages")
        if not provider_id or not isinstance(raw_messages, list):
            raise ValueError("email thread identity is incomplete")
        messages: list[EmailMessage] = []
        for raw in raw_messages:
            if (
                not isinstance(raw, dict)
                or not raw.get("id")
                or raw.get("thread_id") != provider_id
            ):
                raise ValueError("email message identity does not match its thread")
            sent_at = datetime.fromtimestamp(int(str(raw["internal_date"])) / 1000, tz=UTC)

            def parsed(field: str, raw: dict[str, Any] = raw) -> list[str]:
                """Normalize one provider address header for stable source comparison."""
                value = raw.get(field, "")
                return addresses([address for _, address in getaddresses([str(value)]) if address])

            attachments = [
                EmailAttachment.model_validate(item)
                for item in raw.get("attachments", [])
                if isinstance(item, dict)
            ]
            messages.append(
                EmailMessage(
                    id=str(raw["id"]),
                    sender=str(raw.get("from", "")),
                    to=parsed("to"),
                    cc=parsed("cc"),
                    subject=str(raw.get("subject", "")),
                    body=str(raw.get("body", "")),
                    body_offset=int(raw.get("body_offset", 0)),
                    sent_at=sent_at,
                    complete=bool(raw.get("body_complete"))
                    and bool(raw.get("headers_complete"))
                    and not raw.get("body_offset", 0),
                    attachments=attachments,
                    reply_to=parsed("reply_to"),
                    message_id_header=str(raw.get("message_id_header", "")) or None,
                    in_reply_to=str(raw.get("in_reply_to", "")) or None,
                    references=str(raw.get("references", "")).split(),
                    labels=list(raw.get("label_ids", [])),
                    direction="sent" if raw.get("direction") == "sent" else "received",
                )
            )
        messages.sort(key=lambda item: (item.sent_at, item.id))
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "messages": [message.model_dump(mode="json") for message in messages],
                    "complete": normalized.get("complete", False),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        complete = bool(normalized.get("complete")) and all(
            message.complete for message in messages
        )
        in_inbox = any("INBOX" in message.labels for message in messages)
        source_key = hashlib.sha256(f"{account_id}:{provider_id}".encode()).hexdigest()
        now = self.clock.now()
        async with self.uow_factory() as uow, uow.email.lock(principal):
            await self._fence(uow, run, lease)
            if await uow.email.get(principal, "excluded_source", source_key) is not None:
                raise ConflictError("this email source is excluded from learning and retention")
            index = await uow.email.get(principal, "thread_source", source_key)
            previous = (
                None
                if index is None
                else await read_value(
                    uow.email, principal, "thread", str(index.payload["thread_id"]), EmailThread
                )
            )
            archive_operation = (
                None
                if previous is None
                else await self._archive_observed_in(
                    uow, principal, previous, normalized, source_session_id, run
                )
            )
            if previous is not None and (
                previous.source_fingerprint == fingerprint
                or (
                    previous.complete == complete
                    and [message.model_dump(exclude={"labels"}) for message in previous.messages]
                    == [message.model_dump(exclude={"labels"}) for message in messages]
                )
            ):
                if (
                    previous.messages == messages
                    and previous.in_inbox == in_inbox
                    and previous.archive_operation == archive_operation
                ):
                    await self._learn_history(uow.email, principal, previous)
                    return previous
                # Keep legacy label-bearing fingerprints stable: assessments and
                # semantic evidence are keyed to that existing content identity.
                updated = previous.model_copy(
                    update={
                        "messages": messages,
                        "in_inbox": in_inbox,
                        "archive_operation": archive_operation,
                    }
                )
                await save_value(uow.email, principal, "thread", str(updated.id), updated, now)
                return updated
            thread = EmailThread(
                id=self.ids.new_id() if previous is None else previous.id,
                account_id=account_id,
                provider_thread_id=provider_id,
                subject=messages[-1].subject if messages else "Removed conversation",
                senders=list(
                    dict.fromkeys(
                        message.sender for message in messages if message.direction == "received"
                    )
                ),
                updated_at=max((message.sent_at for message in messages), default=now),
                revision=1 if previous is None else previous.revision + 1,
                draft_id=None if previous is None else previous.draft_id,
                session_id=None if previous is None else previous.session_id,
                complete=complete,
                messages=messages,
                source_fingerprint=fingerprint,
                last_accessed_at=now,
                in_inbox=in_inbox,
                archive_operation=archive_operation,
                source_session_ids=list(
                    dict.fromkeys(
                        [
                            *([] if previous is None else previous.source_session_ids),
                            source_session_id,
                        ]
                    )
                ),
                summary="Assessment pending"
                if messages
                else "Conversation removed from the account",
            )
            if thread.draft_id is not None:
                draft = await read_value(
                    uow.email, principal, "draft", str(thread.draft_id), EmailDraft
                )
                if draft is not None and draft.status not in {
                    EmailDraftStatus.SENT,
                    EmailDraftStatus.UNCERTAIN,
                }:
                    await self._cancel_obsolete_send(uow, principal, draft)
                    await save_value(
                        uow.email,
                        principal,
                        "draft",
                        str(draft.id),
                        draft.model_copy(update={"stale": True}),
                        now,
                    )
            await save_value(uow.email, principal, "thread", str(thread.id), thread, now)
            await self._put_data(
                uow.email,
                principal,
                "thread_source",
                source_key,
                {
                    "thread_id": str(thread.id),
                    "observed_message_ids": sorted(
                        {message.id for message in messages} | set()
                        if index is None
                        else {message.id for message in messages}
                        | set(cast(list[str], index.payload.get("observed_message_ids", [])))
                    ),
                },
            )
            await self._learn_history(uow.email, principal, thread)
            return thread

    async def _learn_history(
        self, store: EmailStore, principal: Principal, thread: EmailThread
    ) -> None:
        state = await self._learning_state(store, principal)
        if state.paused:
            return
        account = await read_value(store, principal, "account", thread.account_id, EmailAccount)
        if account is None or account.email_address is None:
            return
        owned = set(addresses(account.verified_addresses or [account.email_address]))
        drafts = [
            EmailDraft.model_validate(row.payload)
            async for row in records(store, principal, "draft_revision")
        ]
        drafts.extend(
            [
                EmailDraft.model_validate(row.payload)
                async for row in records(store, principal, "draft")
            ]
        )
        generated = {draft_body_fingerprint(draft.body) for draft in drafts if draft.body}
        generated.update(
            [
                str(row.payload["body_digest"])
                async for row in records(store, principal, "draft_lineage")
            ]
        )
        style_reset = await store.get(principal, "style_reset", "shared")
        preferences_reset = await store.get(principal, "preferences_reset", "shared")
        changed = False
        for message in thread.messages:
            if (
                message.direction != "sent"
                or not message.complete
                or not owned.intersection(addresses([message.sender]))
            ):
                continue
            digest = draft_body_fingerprint(message.body)
            if digest in generated:
                continue
            recipients = [
                address for address in addresses([*message.to, *message.cc]) if address not in owned
            ]
            if not recipients:
                continue
            lines: list[str] = []
            for line in message.body.splitlines():
                if re.match(
                    r"^(On .+wrote:|From:|[-_]{2,}|Sent from my |Begin forwarded message:)",
                    line,
                    re.IGNORECASE,
                ):
                    break
                if not line.lstrip().startswith(">"):
                    lines.append(line)
            excerpt = "\n".join(lines).strip()[:2000]
            if len(excerpt) < 15 or any(
                token in excerpt.casefold()
                for token in ("unsubscribe", "automatic reply", "out of office")
            ):
                continue
            key = hashlib.sha256(f"{thread.account_id}:{message.id}".encode()).hexdigest()
            data = {
                "account_id": thread.account_id,
                "thread_id": str(thread.id),
                "message_id": message.id,
                "sent_at": message.sent_at.isoformat(),
                "recipients": recipients,
                "excerpt": excerpt,
                "authorship": "historical_sent_attributed",
                "independent": True,
            }
            if (
                style_reset is None or message.sent_at > style_reset.updated_at
            ) and await store.get(principal, "style", key) is None:
                await self._put_data(store, principal, "style", key, data)
                changed = True
            relationship_key = hashlib.sha256(
                f"{thread.account_id}:{thread.provider_thread_id}".encode()
            ).hexdigest()
            if (
                preferences_reset is None or message.sent_at > preferences_reset.updated_at
            ) and await store.get(principal, "relationship", relationship_key) is None:
                await self._put_data(
                    store,
                    principal,
                    "relationship",
                    relationship_key,
                    {key: value for key, value in data.items() if key != "excerpt"},
                )
                changed = True
        await self._prune_styles(store, principal)
        if changed:
            await self._bump_profile(store, principal)

    async def _prune_styles(self, store: EmailStore, principal: Principal) -> None:
        examples = [row async for row in records(store, principal, "style")]
        examples.sort(
            key=lambda row: (
                row.payload.get("authorship") in {"owner_endorsed", "owner_edit_delta"},
                str(row.payload["sent_at"]),
            ),
            reverse=True,
        )
        recipient_counts: dict[str, int] = {}
        kept = 0
        for example in examples:
            recipient = cast(list[str], example.payload["recipients"])[0]
            if kept >= 500 or recipient_counts.get(recipient, 0) >= 10:
                await store.delete(
                    principal, "style", example.key, expected_revision=example.revision
                )
            else:
                kept += 1
                recipient_counts[recipient] = recipient_counts.get(recipient, 0) + 1

    async def save_assessment(
        self,
        principal: Principal,
        thread_id: UUID,
        source_revision: int,
        assessment: dict[str, object],
        *,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> EmailThread:
        async with self.uow_factory() as uow, uow.email.lock(principal):
            await self._fence(uow, run, lease)
            thread = await self._thread(uow.email, principal, thread_id)
            state = await self._learning_state(uow.email, principal)
            if (
                thread.revision != source_revision
                or assessment.get("profile_revision") != state.profile_revision
            ):
                raise ConflictError("email source or learning changed during assessment")

            def score(name: str) -> float:
                value = assessment.get(name, 0)
                if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                    raise ValueError("email assessment feature is invalid")
                return float(value)

            content = score("content_importance")
            relationship = score("relationship_importance")
            senders = set(addresses(thread.senders))
            replies = 0.0
            async for row in records(uow.email, principal, "relationship"):
                if senders.intersection(cast(list[str], row.payload["recipients"])):
                    age = max(
                        0,
                        (
                            self.clock.now() - datetime.fromisoformat(str(row.payload["sent_at"]))
                        ).total_seconds(),
                    )
                    replies += 0.5 ** (age / (180 * 86400))
            memory_evidence = await self._relationship_evidence(uow, principal, thread)
            admitted_ids = {str(item["id"]) for item in memory_evidence}
            proposed_ids = assessment.get("relationship_memory_ids", [])
            if not isinstance(proposed_ids, list) or not all(
                isinstance(item, str) for item in proposed_ids
            ):
                raise ValueError("relationship memory references are malformed")
            supported = bool(admitted_ids.intersection(proposed_ids))
            relationship = min(relationship, 1.0 if supported else min(1.0, replies / 5))
            urgency = score("urgency")
            priority = min(
                1.0, max(content * 0.9, relationship * 0.7 + content * 0.3) + urgency * 0.1
            )
            if assessment.get("bulk"):
                priority *= 0.4
            topics = assessment.get("topics", [])
            if (
                not isinstance(topics, list)
                or any(not isinstance(topic, str) for topic in topics)
                or len(topics) > 20
            ):
                raise ValueError("email assessment topics are invalid")
            thread = thread.model_copy(
                update={
                    "summary": str(assessment.get("summary", ""))[:2000],
                    "reason": str(assessment.get("reason", ""))[:2000],
                    "priority": priority,
                    "topics": topics,
                    "needs_reply": assessment.get("needs_reply") is True
                    and thread.complete
                    and bool(thread.messages)
                    and thread.messages[-1].direction != "sent",
                    "reply_blocked_reason": assessment.get("reply_blocked_reason"),
                    "profile_revision": state.profile_revision,
                    "assessment_version": EMAIL_POLICY_VERSION,
                }
            )
            await save_value(
                uow.email, principal, "thread", str(thread.id), thread, self.clock.now()
            )
            await self._put_data(uow.email, principal, "assessment", str(thread.id), assessment)
            return apply_feedback(thread, await self._feedback(uow.email, principal))

    async def save_generated_draft(
        self,
        principal: Principal,
        thread_id: UUID,
        source_revision: int,
        body: str,
        *,
        run_id: UUID,
        instruction: str | None = None,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> EmailDraft:
        if not body.strip() or len(body) > 500_000:
            raise ValueError("generated draft body is empty or too large")
        async with self.uow_factory() as uow, uow.email.lock(principal):
            await self._fence(uow, run, lease)
            thread = await self._thread(uow.email, principal, thread_id)
            if (
                thread.revision != source_revision
                or not thread.complete
                or not thread.messages
                or thread.reply_blocked_reason
            ):
                raise ConflictError(
                    "a draft requires a complete, current thread and the missing decision"
                )
            account = await read_value(
                uow.email, principal, "account", thread.account_id, EmailAccount
            )
            if account is None or not account.email_address:
                raise ConflictError("verify the sending account before preparing a draft")
            own = set(addresses(account.verified_addresses or [account.email_address]))
            target = thread.messages[-1]
            if target.direction == "sent":
                raise ConflictError("the latest message is already an outgoing reply")
            to = [
                address
                for address in addresses(target.reply_to or [target.sender])
                if address not in own
            ]
            cc = [
                address
                for address in addresses([*target.to, *target.cc])
                if address not in own and address not in to
            ]
            if not to or not target.message_id_header:
                raise ConflictError("the source lacks a verified reply target or Message-ID header")
            old = (
                None
                if thread.draft_id is None
                else await read_value(
                    uow.email, principal, "draft", str(thread.draft_id), EmailDraft
                )
            )
            if old is not None and old.status != EmailDraftStatus.DISCARDED and instruction is None:
                return old
            if old is not None and old.status not in {
                EmailDraftStatus.READY,
                EmailDraftStatus.FAILED,
                EmailDraftStatus.DISCARDED,
            }:
                raise ConflictError("finish the current draft action before regenerating")
            session = await self._session_in(uow, principal, thread)
            state = await self._learning_state(uow.email, principal)
            draft = EmailDraft(
                id=self.ids.new_id()
                if old is None or old.status == EmailDraftStatus.DISCARDED
                else old.id,
                revision=1
                if old is None or old.status == EmailDraftStatus.DISCARDED
                else old.revision + 1,
                thread_id=thread.id,
                account_id=thread.account_id,
                source_revision=thread.revision,
                profile_revision=state.profile_revision,
                to=to,
                cc=cc,
                subject=target.subject
                if target.subject.casefold().startswith("re:")
                else f"Re: {target.subject}",
                body=body,
                session_id=session.id,
                run_id=run_id,
                updated_at=self.clock.now(),
                in_reply_to=target.message_id_header,
                references=list(dict.fromkeys([*target.references, target.message_id_header])),
                generated_body_digest=hashlib.sha256(body.encode()).hexdigest(),
                provider_thread_id=thread.provider_thread_id,
                send_tool_name=f"mcp.{self.account_servers[thread.account_id]['send']}.send_message",
            )
            await save_value(uow.email, principal, "draft", str(draft.id), draft, self.clock.now())
            await self._archive_draft(uow.email, principal, draft)
            await save_value(
                uow.email,
                principal,
                "thread",
                str(thread.id),
                thread.model_copy(update={"draft_id": draft.id, "session_id": session.id}),
                self.clock.now(),
            )
        if self.activate_session is not None:
            await self.activate_session(session.id)
        return draft

    async def _fence(
        self, uow: RepositoryUnitOfWork, run: Run | None, lease: WorkerLease | None
    ) -> None:
        if run is None:
            if lease is not None:
                raise ValueError("a fenced email projection requires its run")
            return
        await uow.events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=run.id,
                event_type="email.projection.updated",
                actor_type="runtime",
                payload={},
            ),
            lease=lease,
        )

    async def _relationship_evidence(
        self, uow: RepositoryUnitOfWork, principal: Principal, thread: EmailThread
    ) -> list[dict[str, object]]:
        senders = addresses(thread.senders)
        if not senders:
            return []
        now = self.clock.now()
        candidates = await uow.memories.query(
            RecallQuery(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                current_scope="general",
                text=" ".join(senders),
                budget_tokens=2000,
                max_items=20,
                min_score=0,
                belief_types=[BeliefType.RELATIONSHIP, BeliefType.FACT],
                sensitivity_ceiling=Sensitivity.INTERNAL,
            )
        )
        admitted: list[dict[str, object]] = []
        for belief in candidates:
            if (
                belief.status is not MemoryStatus.ACTIVE
                or belief.valid_from > now
                or (belief.valid_to is not None and belief.valid_to <= now)
                or (belief.expires_at is not None and belief.expires_at <= now)
            ):
                continue
            if not any(
                sender in (belief.subject + " " + belief.statement).casefold() for sender in senders
            ):
                continue
            active_deal = bool(
                re.search(
                    r"(?:active|prospective|potential|likely|considering|diligence).{0,40}(?:invest|deal)|"
                    r"(?:invest|deal).{0,40}(?:discussion|diligence|consider|active)",
                    belief.subject + " " + belief.statement,
                    re.IGNORECASE,
                )
            )
            if (
                belief.authority is MemoryAuthority.INFERRED
                and belief.last_evidence_at < now - timedelta(days=90 if active_deal else 180)
            ):
                continue
            admitted.append(
                {
                    "id": str(belief.id),
                    "subject": belief.subject,
                    "statement": belief.statement,
                    "authority": belief.authority.value,
                    "last_evidence_at": belief.last_evidence_at.isoformat(),
                }
            )
        return admitted

    async def discussion(self, principal: Principal, thread_id: UUID) -> dict[str, object]:
        require_scope(principal, "email.write")
        require_scope(principal, "session.write")
        async with self.uow_factory() as uow, uow.email.lock(principal):
            thread = await self._thread(uow.email, principal, thread_id)
            session = await self._session_in(uow, principal, thread)
            await uow.events.append(
                NewEvent(
                    session_id=session.id,
                    run_id=None,
                    event_type="email.discussion.opened",
                    actor_type="principal",
                    actor_id=principal.principal_id,
                    payload={},
                    derivation_key=f"email-discussion:{session.id}",
                )
            )
            result = thread_summary(thread.model_copy(update={"session_id": session.id}))
        if self.activate_session is not None:
            await self.activate_session(session.id)
        return result

    async def dismiss(
        self,
        principal: Principal,
        thread_id: UUID,
        expected_revision: int,
        *,
        dismissed: bool = True,
    ) -> dict[str, object]:
        """Update revision-scoped handled state without changing mail or learning."""
        require_scope(principal, "email.write")
        async with self.uow_factory() as uow, uow.email.lock(principal):
            thread = await self._thread(uow.email, principal, thread_id)
            if thread.revision != expected_revision:
                raise ConflictError("the thread changed; review its new content")
            dismissed_revision = thread.revision if dismissed else None
            if thread.dismissed_revision == dismissed_revision:
                return thread_summary(thread)
            thread = thread.model_copy(update={"dismissed_revision": dismissed_revision})
            await save_value(
                uow.email, principal, "thread", str(thread.id), thread, self.clock.now()
            )
            return thread_summary(thread)

    async def discard_draft(
        self, principal: Principal, draft_id: UUID, expected_revision: int
    ) -> EmailDraft:
        require_scope(principal, "email.write")
        async with self.uow_factory() as uow, uow.email.lock(principal):
            draft = await self._draft(uow.email, principal, draft_id)
            if draft.revision != expected_revision or draft.status in {
                EmailDraftStatus.SENDING,
                EmailDraftStatus.UNCERTAIN,
            }:
                raise ConflictError("the draft changed or its send is unresolved")
            if draft.status is EmailDraftStatus.DISCARDED:
                return draft
            draft = draft.model_copy(
                update={"status": EmailDraftStatus.DISCARDED, "updated_at": self.clock.now()}
            )
            await save_value(uow.email, principal, "draft", str(draft.id), draft, self.clock.now())
            return draft

    async def exclude_source(
        self, principal: Principal, thread_id: UUID, expected_revision: int
    ) -> dict[str, object]:
        require_scope(principal, "email.write")
        now = self.clock.now()
        async with self.uow_factory() as uow, uow.email.lock(principal):
            thread = await read_value(uow.email, principal, "thread", str(thread_id), EmailThread)
            if thread is None:
                exclusions = [
                    row
                    async for row in records(uow.email, principal, "excluded_source")
                    if row.payload.get("thread_id") == str(thread_id)
                ]
                if not exclusions:
                    raise NotFoundError("email source not found")
                data = exclusions[0].payload
                account_id = str(data["account_id"])
                provider_id = str(data["provider_thread_id"])
                message_ids = frozenset(cast(list[str], data["message_ids"]))
                audit_session_id = UUID(str(data["audit_session_id"]))
                source_key = exclusions[0].key
            else:
                self._authorize_account(principal, thread.account_id)
                if thread.revision != expected_revision:
                    raise ConflictError("the source changed; review it before excluding it")
                if (
                    thread.session_id is not None
                    and await uow.runs.active_for_session(thread.session_id, principal) is not None
                ):
                    raise ConflictError(
                        "finish or cancel the active thread run before excluding its source"
                    )
                account_id, provider_id = thread.account_id, thread.provider_thread_id
                message_ids = frozenset(message.id for message in thread.messages)
                audit_session = await self._session_in(uow, principal, None)
                audit_session_id = audit_session.id
                source_key = hashlib.sha256(f"{account_id}:{provider_id}".encode()).hexdigest()
                source_index = await uow.email.get(principal, "thread_source", source_key)
                historical_ids = (
                    set()
                    if source_index is None
                    else set(cast(list[str], source_index.payload.get("observed_message_ids", [])))
                )
                receipts = {
                    str(row.payload["message_id"])
                    async for row in records(uow.email, principal, "semantic_source")
                    if row.payload.get("account_id") == account_id
                    and row.payload.get("provider_thread_id") == provider_id
                }
                message_ids |= frozenset(historical_ids | receipts)
                data = {
                    "account_id": account_id,
                    "provider_thread_id": provider_id,
                    "thread_id": str(thread_id),
                    "message_ids": sorted(message_ids),
                    "audit_session_id": str(audit_session_id),
                    "status": "cleanup_pending",
                }
                await self._put_data(uow.email, principal, "excluded_source", source_key, data)
            self._authorize_account(principal, account_id)
            counts = await uow.session_deletions.erase_email_source(
                principal, account_id, provider_id, message_ids, now
            )
            draft_ids: set[str] = set()
            for row in [row async for row in records(uow.email, principal, "draft")]:
                if row.payload.get("thread_id") == str(thread_id):
                    draft_ids.add(row.key)
                    await uow.email.delete(
                        principal, "draft", row.key, expected_revision=row.revision
                    )
            for kind in ("draft_revision", "draft_lineage", "style", "relationship", "assessment"):
                for row in [row async for row in records(uow.email, principal, kind)]:
                    if row.payload.get("thread_id") == str(thread_id) or (
                        kind == "assessment" and row.key == str(thread_id)
                    ):
                        await uow.email.delete(
                            principal, kind, row.key, expected_revision=row.revision
                        )
            for row in [row async for row in records(uow.email, principal, "draft_edit_replay")]:
                if str(row.payload.get("revision_key", "")).split(":")[0] in draft_ids:
                    await uow.email.delete(
                        principal, "draft_edit_replay", row.key, expected_revision=row.revision
                    )
            for kind, key in (("thread", str(thread_id)), ("thread_source", source_key)):
                current_record = await uow.email.get(principal, kind, key)
                if current_record is not None:
                    await uow.email.delete(
                        principal, kind, key, expected_revision=current_record.revision
                    )
            await self._bump_profile(uow.email, principal)
        if self.forget_source is not None:
            await self.forget_source(principal, account_id, provider_id, message_ids)
        if self.cleanup_artifacts is not None:
            await self.cleanup_artifacts()
        async with self.uow_factory() as uow, uow.email.lock(principal):
            remaining = await uow.session_deletions.erase_email_source(
                principal, account_id, provider_id, message_ids, now
            )
            status = "cleanup_pending" if remaining.get("pending_artifacts", 0) else "erased"
            await self._put_data(
                uow.email, principal, "excluded_source", source_key, {**data, "status": status}
            )
            await uow.events.append(
                NewEvent(
                    session_id=audit_session_id,
                    run_id=None,
                    event_type="email.source.erased",
                    actor_type="principal",
                    actor_id=principal.principal_id,
                    payload={"source_id": source_key, "status": status, "counts": counts},
                    derivation_key=f"email-source-erased:{source_key}:{status}",
                )
            )
        return {
            "source_id": source_key,
            "status": status,
            "pending_artifacts": remaining.get("pending_artifacts", 0),
        }

    async def expire_cache(self, principal: Principal) -> int:
        """Retention only: no retrieval, inference, or new work admission."""
        cutoff = self.clock.now() - timedelta(days=30)
        removed = 0
        async with self.uow_factory() as uow, uow.email.lock(principal):
            for row in [row async for row in records(uow.email, principal, "thread")]:
                thread = EmailThread.model_validate(row.payload)
                if thread.last_accessed_at > cutoff or not any(
                    message.body for message in thread.messages
                ):
                    continue
                thread = thread.model_copy(
                    update={
                        "messages": [
                            message.model_copy(update={"body": "", "complete": False})
                            for message in thread.messages
                        ],
                        "complete": False,
                        "source_fingerprint": "",
                        "assessment_version": "",
                    }
                )
                await save_value(uow.email, principal, "thread", row.key, thread, self.clock.now())
                removed += 1
            expired: set[str] = set()
            for row in [row async for row in records(uow.email, principal, "draft")]:
                draft = EmailDraft.model_validate(row.payload)
                if (
                    draft.status not in {EmailDraftStatus.SENT, EmailDraftStatus.DISCARDED}
                    or draft.updated_at > cutoff
                ):
                    continue
                expired.add(str(draft.id))
                if draft.body:
                    await save_value(
                        uow.email,
                        principal,
                        "draft",
                        row.key,
                        draft.model_copy(update={"body": ""}),
                        self.clock.now(),
                    )
                    removed += 1
            for row in [row async for row in records(uow.email, principal, "draft_revision")]:
                if str(row.payload["id"]) in expired:
                    await uow.email.delete(
                        principal, "draft_revision", row.key, expected_revision=row.revision
                    )
                    removed += 1
        return removed

    async def endorse_style(
        self, principal: Principal, draft_id: UUID, expected_revision: int
    ) -> EmailLearningState:
        """Use an explicitly chosen revision as style evidence, never sent authorship."""
        require_scope(principal, "email.write")
        await self.expire_cache(principal)
        async with self.uow_factory() as uow, uow.email.lock(principal):
            draft = await self._draft(uow.email, principal, draft_id)
            if draft.revision != expected_revision:
                raise ConflictError("the draft changed; review its wording before endorsing it")
            key = f"endorsed:{draft.id}:{draft.revision}"
            if await uow.email.get(principal, "style", key) is None:
                excerpt = draft.body.strip()[:2000]
                if not excerpt or not draft.to:
                    raise ValueError("a writing example needs wording and a recipient context")
                await self._put_data(
                    uow.email,
                    principal,
                    "style",
                    key,
                    {
                        "account_id": draft.account_id,
                        "thread_id": str(draft.thread_id),
                        "draft_id": str(draft.id),
                        "draft_revision": draft.revision,
                        "sent_at": self.clock.now().isoformat(),
                        "recipients": draft.to,
                        "excerpt": excerpt,
                        "authorship": "owner_endorsed",
                        "independent": False,
                    },
                )
                await self._prune_styles(uow.email, principal)
                await self._bump_profile(uow.email, principal)
            state = await self._learning_state(uow.email, principal)
            return await self._summarize_learning(uow.email, principal, state)
