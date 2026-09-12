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
from agent_core.domain.context import TaskState, WorkingState
from agent_core.domain.email import (
    EMAIL_POLICY_VERSION,
    EMAIL_SLICE_RESERVATION,
    EmailAccount,
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
    feedback_matches,
)
from agent_core.domain.errors import BudgetExceededError, ConflictError, NotFoundError
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
        async with self.uow_factory() as uow:
            feedback = await self._feedback(uow.email, principal)
            threads = [
                apply_feedback(EmailThread.model_validate(row.payload), feedback)
                async for row in records(uow.email, principal, "thread")
            ]
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
        kind: Literal["refresh", "draft", "send"],
        thread_id: UUID | None = None,
        draft_id: UUID | None = None,
        expected_revision: int | None = None,
        instruction: str | None = None,
        idempotency_key: str | None = None,
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
            digest = hashlib.sha256(
                json.dumps(
                    [kind, str(thread_id), str(draft_id), expected_revision, instruction]
                ).encode()
            ).hexdigest()
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
                if kind == "send"
                else min(
                    EMAIL_SLICE_RESERVATION,
                    self.agent.limits.max_cost or EMAIL_SLICE_RESERVATION,
                )
            )
            await self._check_budget(uow.email, principal, reservation)
            session = await self._session_in(uow, principal, thread)
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
            )
            await save_value(uow.email, principal, "task", str(run.id), task, now)
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
        if kind == "refresh":
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

    async def get_task(self, principal: Principal, run_id: UUID) -> EmailTask | None:
        async with self.uow_factory() as uow:
            return await read_value(uow.email, principal, "task", str(run_id), EmailTask)

    async def save_task(self, principal: Principal, task: EmailTask) -> None:
        async with self.uow_factory() as uow, uow.email.lock(principal):
            await save_value(uow.email, principal, "task", str(task.run_id), task, self.clock.now())

    async def settle(self, principal: Principal, run_id: UUID) -> None:
        """Release terminal refresh resources and settle only provable usage."""
        task = await self.get_task(principal, run_id)
        if task is not None and task.kind == "refresh":
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
            if previous is not None and previous.source_fingerprint == fingerprint:
                await self._learn_history(uow.email, principal, previous)
                return previous
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
                complete=bool(normalized.get("complete"))
                and all(message.complete for message in messages),
                messages=messages,
                source_fingerprint=fingerprint,
                last_accessed_at=now,
                in_inbox=any("INBOX" in message.labels for message in messages),
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
