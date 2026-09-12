"""Mailbox labels change independently from source content and learned evidence."""

import hashlib
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from agent_core.application.email import read_value, save_value
from agent_core.bootstrap import Composition, build
from agent_core.domain.email import (
    EmailAccount,
    EmailArchiveConsent,
    EmailArchiveOperation,
    EmailDraft,
    EmailRecord,
    EmailTask,
    EmailThread,
)
from agent_core.domain.events import NewEvent
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus, ToolSource
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _mailbox_factory, _page


@pytest.fixture
async def labeled_mailbox() -> AsyncIterator[tuple[Composition, dict[str, Any], EmailThread]]:
    """Seed real ingestion, historical learning, an assessment and an unmodified draft."""
    async with build(
        settings=replace(_email_settings(), email_mode_enabled=True),
        mcp_client_factory=await _mailbox_factory([]),
    ) as app:
        async with app.uow_factory() as uow:
            await save_value(
                uow.email,
                app.principal,
                "account",
                "default",
                EmailAccount(
                    id="default",
                    label="Personal",
                    email_address="owner@example.test",
                    verified_addresses=["owner@example.test"],
                ),
                app.clock.now(),
            )
        page = _page(changed=True)
        page["messages"][1].update(
            internal_date=1789127999000,
            to="colleague@example.test",
            body="Thanks for preparing these materials. I will review them today.",
        )
        page["messages"][0]["label_ids"] = ["INBOX", "UNREAD", "Label_board"]
        session = await app.sessions.create()
        service = app.services.email
        thread = await service.import_thread(app.principal, "default", page, session)
        state = await service.learning(app.principal)
        await service.save_assessment(
            app.principal,
            thread.id,
            thread.revision,
            {
                "profile_revision": state.profile_revision,
                "content_importance": 0.9,
                "summary": "Review the board materials",
                "reason": "A direct request from a colleague",
                "needs_reply": True,
                "topics": ["board"],
                "source_fingerprint": thread.source_fingerprint,
            },
        )
        await service.save_generated_draft(
            app.principal,
            thread.id,
            thread.revision,
            "I will review the materials.",
            run_id=uuid4(),
        )
        async with app.uow_factory() as uow:
            stored = await read_value(
                uow.email, app.principal, "thread", str(thread.id), EmailThread
            )
            assert stored is not None
            thread = stored.model_copy(
                update={
                    "dismissed_revision": thread.revision,
                    "archive_operation": EmailArchiveOperation(
                        operation_id=uuid4(), run_id=uuid4(), target_archived=True
                    ),
                }
            )
            await save_value(
                uow.email, app.principal, "thread", str(thread.id), thread, app.clock.now()
            )
        yield app, page, thread


async def retained_state(app: Composition) -> dict[str, list[EmailRecord]]:
    """Snapshot durable draft, assessment and learned records, including their revisions."""
    async with app.uow_factory() as uow:
        return {
            kind: await uow.email.list(app.principal, kind)
            for kind in (
                "draft",
                "draft_revision",
                "assessment",
                "style",
                "relationship",
                "learning",
                "feedback",
            )
        }


@pytest.mark.parametrize(
    "labels",
    [[], ["UNREAD", "Label_board"], ["INBOX", "Label_board"], ["INBOX", "UNREAD", "STARRED"]],
)
async def test_label_only_sync_preserves_source_drafts_assessment_and_learning(
    labeled_mailbox: tuple[Composition, dict[str, Any], EmailThread], labels: list[str]
) -> None:
    """Inbox, unread and custom-label changes retain the existing content fingerprint."""
    app, page, previous = labeled_mailbox
    before = await retained_state(app)
    assert before["style"] and before["relationship"] and before["assessment"]
    observation = deepcopy(page)
    observation["messages"][0]["label_ids"] = labels
    updated = await app.services.email.import_thread(
        app.principal, "default", observation, await app.sessions.create()
    )
    assert updated.revision == previous.revision
    assert updated.source_fingerprint == previous.source_fingerprint
    assert updated.in_inbox is ("INBOX" in labels)
    assert next(message for message in updated.messages if message.id == "m1").labels == labels
    assert updated.model_dump(exclude={"messages", "in_inbox"}) == previous.model_dump(
        exclude={"messages", "in_inbox"}
    )
    assert await retained_state(app) == before
    replay = await app.services.email.import_thread(
        app.principal, "default", observation, await app.sessions.create()
    )
    assert replay == updated
    assert await retained_state(app) == before


async def test_label_round_trip_then_new_content_reopens_attention_and_stales_draft(
    labeled_mailbox: tuple[Composition, dict[str, Any], EmailThread],
) -> None:
    """Legacy label-bearing fingerprints stay usable until real source material changes."""
    app, page, previous = labeled_mailbox
    archived = deepcopy(page)
    archived["messages"][0]["label_ids"] = ["UNREAD", "Label_board"]
    await app.services.email.import_thread(
        app.principal, "default", archived, await app.sessions.create()
    )
    restored = await app.services.email.import_thread(
        app.principal, "default", page, await app.sessions.create()
    )
    assert restored == previous
    changed = deepcopy(page)
    changed["messages"][0]["body"] += " Please send your decision by Friday."
    updated = await app.services.email.import_thread(
        app.principal, "default", changed, await app.sessions.create()
    )
    assert updated.revision == previous.revision + 1
    assert updated.source_fingerprint != previous.source_fingerprint
    assert updated.dismissed_revision != updated.revision
    assert updated.summary == "Assessment pending"
    async with app.uow_factory() as uow:
        draft = await read_value(
            uow.email, app.principal, "draft", str(previous.draft_id), EmailDraft
        )
    assert draft is not None and draft.stale
    assert draft.body == "I will review the materials."


@pytest.mark.parametrize("change", ["body", "new_message", "completeness"])
async def test_real_source_changes_retain_pending_archive_operation(
    labeled_mailbox: tuple[Composition, dict[str, Any], EmailThread], change: str
) -> None:
    """New material reopens attention without losing the durable mailbox action identity."""
    app, page, previous = labeled_mailbox
    changed = deepcopy(page)
    if change == "body":
        changed["messages"][0]["body"] += " The agenda has changed."
    elif change == "new_message":
        changed["messages"].append(
            {**changed["messages"][0], "id": "m3", "internal_date": 1789128002000}
        )
    else:
        changed["complete"] = False
    updated = await app.services.email.import_thread(
        app.principal, "default", changed, await app.sessions.create()
    )
    assert updated.revision == previous.revision + 1
    assert updated.dismissed_revision != updated.revision
    assert updated.source_fingerprint != previous.source_fingerprint
    assert updated.archive_operation == previous.archive_operation


async def observed_archive(
    app: Composition,
    page: dict[str, Any],
    thread: EmailThread,
    *,
    archived: bool,
    evidence: str = "valid",
) -> tuple[Run, dict[str, Any]]:
    """Persist a dispatched uncertain write and subsequent governed provider-read receipt."""

    async def leave_queued(run_id: object) -> None:
        """Keep the refresh under test from invoking scripted provider work."""
        pass

    app.services.email.dispatch = leave_queued
    operation = await app.services.email.submit_task(app.principal, kind="refresh")
    old_session = await app.sessions.create()
    async with app.uow_factory() as uow:
        run = await uow.runs.transition(operation.run_id, RunStatus.QUEUED, RunStatus.RUNNING)
        old_run = run.model_copy(
            update={
                "id": uuid4(),
                "session_id": old_session,
                "status": RunStatus.RUNNING if evidence == "write_running" else RunStatus.COMPLETED,
            }
        )
        await uow.runs.create(old_run)
        written_at = app.clock.now() - timedelta(minutes=1)
        consent = EmailArchiveConsent(
            tenant_id=app.principal.tenant_id,
            principal_id=app.principal.principal_id,
            account_id="default",
            provider_thread_id=thread.provider_thread_id,
            read_server_id="gmail_read",
            write_server_id="gmail_write",
            expected_revision=thread.revision,
            archived=archived,
            expires_at=written_at + timedelta(seconds=120),
        )
        task = EmailTask(
            id=uuid4(),
            run_id=old_run.id,
            session_id=old_run.session_id,
            kind="archive",
            account_ids=["default"],
            thread_id=thread.id,
            expected_revision=thread.revision,
            archive_consent=consent,
            created_at=written_at,
        )
        await save_value(uow.email, app.principal, "task", str(task.run_id), task, written_at)
        uncertain = thread.model_copy(
            update={
                "archive_operation": EmailArchiveOperation(
                    operation_id=task.id,
                    run_id=old_run.id,
                    target_archived=archived,
                    status="uncertain",
                    error="The provider outcome is unknown.",
                )
            }
        )
        await save_value(uow.email, app.principal, "thread", str(thread.id), uncertain, written_at)
        write = ToolInvocation(
            id=uuid4(),
            run_id=old_run.id,
            session_id=old_run.session_id,
            step_number=1,
            call_id="email-" + hashlib.sha256(f"{old_run.id}:archive".encode()).hexdigest()[:32],
            tool_name=consent.tool_name,
            tool_version="1",
            tool_source=ToolSource.MCP,
            server_id="gmail_write",
            idempotency_class=IdempotencyClass.NON_IDEMPOTENT,
            side_effect=SideEffectClass.EXTERNAL_WRITE,
            risk=RiskLevel.HIGH,
            status=ToolInvocationStatus.UNCERTAIN,
            raw_arguments=json.dumps(consent.arguments),
            normalized_arguments=consent.arguments,
            idempotency_key=str(uuid4()),
            effect_sent_at=written_at,
            created_at=written_at,
            updated_at=written_at,
        )
        await uow.invocations.create(write)
        observation = deepcopy(page)
        for message in observation["messages"]:
            message["label_ids"] = [label for label in message["label_ids"] if label != "INBOX"]
        if not archived:
            observation["messages"][0]["label_ids"].append("INBOX")
        if evidence == "opposite_state":
            observation["messages"][0]["label_ids"] = ["INBOX"] if archived else []
        result = deepcopy(observation)
        if evidence == "wrong_labels":
            result["messages"][0]["label_ids"] = ["INBOX"] if archived else []
        if evidence == "partial":
            result["total_messages"] += 1
        if evidence == "wrong_thread":
            result["thread_id"] = "another-thread"
        read = ToolInvocation(
            id=uuid4(),
            run_id=run.id,
            session_id=run.session_id,
            step_number=1,
            call_id="provider-read",
            tool_name="mcp.gmail_read.get_thread_page",
            tool_version="1",
            tool_source=ToolSource.MCP,
            server_id="gmail_read",
            side_effect=SideEffectClass.NETWORK_READ,
            risk=RiskLevel.LOW,
            status=ToolInvocationStatus.SUCCEEDED,
            raw_arguments=json.dumps({"thread_id": thread.provider_thread_id}),
            normalized_arguments={"thread_id": thread.provider_thread_id},
            idempotency_key=str(uuid4()),
            structured_result=result,
            created_at=app.clock.now(),
            updated_at=app.clock.now(),
        )
        if evidence == "stale":
            read = read.model_copy(update={"created_at": written_at - timedelta(seconds=1)})
        if evidence == "wrong_account":
            read = read.model_copy(
                update={"tool_name": "mcp.other_read.get_thread_page", "server_id": "other_read"}
            )
        if evidence == "failed":
            read = read.model_copy(update={"status": ToolInvocationStatus.FAILED})
        await uow.invocations.create(read)
        event = await uow.events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=old_run.id if evidence == "wrong_run" else run.id,
                event_type="tool.call.failed"
                if evidence == "failed_event"
                else "tool.call.completed",
                actor_type="tool",
                payload={"call_id": read.call_id, "name": read.tool_name},
            )
        )
        for message in observation["messages"]:
            message["_header_session_id"] = str(run.session_id)
            message["_header_event_sequence"] = event.sequence
        if evidence == "missing":
            observation["messages"][0].pop("_header_event_sequence")
        if evidence == "cached_session":
            observation["messages"][0]["_header_session_id"] = str(uuid4())
    return run, observation


@pytest.mark.parametrize("archived", [True, False])
async def test_fresh_completed_read_reconciles_uncertain_archive_without_repeating_write(
    labeled_mailbox: tuple[Composition, dict[str, Any], EmailThread],
    archived: bool,
) -> None:
    """Only a later authoritative read of the target Inbox state settles uncertainty."""
    app, page, thread = labeled_mailbox
    run, observation = await observed_archive(app, page, thread, archived=archived)
    before = await retained_state(app)
    updated = await app.services.email.import_thread(
        app.principal,
        "default",
        observation,
        run.session_id,
        run=run,
    )
    assert updated.archive_operation is not None
    assert updated.archive_operation.status == "completed"
    assert updated.archive_operation.error is None
    assert updated.in_inbox is not archived
    assert updated.revision == thread.revision
    assert updated.source_fingerprint == thread.source_fingerprint
    assert await retained_state(app) == before
    async with app.uow_factory() as uow:
        writes = await uow.invocations.list_for_run(updated.archive_operation.run_id, app.principal)
    assert len(writes) == 1 and writes[0].status is ToolInvocationStatus.UNCERTAIN


@pytest.mark.parametrize(
    "evidence",
    [
        "missing",
        "stale",
        "wrong_run",
        "cached_session",
        "wrong_account",
        "wrong_thread",
        "failed",
        "failed_event",
        "partial",
        "wrong_labels",
        "opposite_state",
        "write_running",
    ],
)
async def test_unproven_observation_cannot_clear_uncertain_archive(
    labeled_mailbox: tuple[Composition, dict[str, Any], EmailThread],
    evidence: str,
) -> None:
    """Incomplete, cached, unrelated or unsuccessful reads retain the no-retry boundary."""
    app, page, thread = labeled_mailbox
    run, observation = await observed_archive(app, page, thread, archived=True, evidence=evidence)
    updated = await app.services.email.import_thread(
        app.principal,
        "default",
        observation,
        run.session_id,
        run=run,
    )
    assert updated.archive_operation is not None
    assert updated.archive_operation.status == "uncertain"


@pytest.mark.parametrize("kind", ["refresh", "draft", "send"])
async def test_legacy_five_field_digest_replays_and_coalesces_existing_task(
    labeled_mailbox: tuple[Composition, dict[str, Any], EmailThread],
    kind: str,
) -> None:
    """Existing pre-archive requests retain their identity after the new intent is added."""
    app, _, thread = labeled_mailbox
    service = app.services.email

    async def leave_queued(run_id: object) -> None:
        """Leave the legacy operation pending to exercise admission coalescing."""
        pass

    service.dispatch = leave_queued
    initial = await service.submit_task(app.principal, kind="refresh")
    task = await service.get_task(app.principal, initial.run_id)
    assert task is not None
    thread_id = None if kind == "refresh" else thread.id
    draft_id = thread.draft_id if kind == "send" else None
    expected = None if kind == "refresh" else thread.revision
    instruction = "Keep the answer concise." if kind == "draft" else None
    legacy_task = task.model_copy(
        update={
            "kind": kind,
            "thread_id": thread_id,
            "draft_id": draft_id,
            "expected_revision": expected,
            "instruction": instruction,
        }
    )
    key = "legacy-request"
    digest = hashlib.sha256(
        json.dumps(
            [
                kind,
                str(thread_id),
                str(draft_id),
                expected,
                instruction,
            ]
        ).encode()
    ).hexdigest()
    async with app.uow_factory() as uow, uow.email.lock(app.principal):
        await save_value(
            uow.email, app.principal, "task", str(task.run_id), legacy_task, app.clock.now()
        )
        await uow.email.put(
            EmailRecord(
                tenant_id=app.principal.tenant_id,
                principal_id=app.principal.principal_id,
                kind="task_replay",
                key=hashlib.sha256(key.encode()).hexdigest(),
                revision=1,
                payload={"run_id": str(task.run_id), "request_digest": digest},
                created_at=app.clock.now(),
                updated_at=app.clock.now(),
            ),
            expected_revision=0,
        )
    arguments: dict[str, Any] = {
        "kind": kind,
        "thread_id": thread_id,
        "draft_id": draft_id,
        "expected_revision": expected,
        "instruction": instruction,
    }
    replay = await service.submit_task(app.principal, **arguments, idempotency_key=key)
    coalesced = await service.submit_task(app.principal, **arguments, idempotency_key="fresh-key")
    assert replay.replayed and coalesced.replayed
    assert replay.run_id == coalesced.run_id == task.run_id
    assert replay.operation_id == coalesced.operation_id == task.id
