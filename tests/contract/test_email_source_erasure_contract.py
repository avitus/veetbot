"""The same selective erasure contract runs against memory and PostgreSQL."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from agent_core.bootstrap import build
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import MemoryAuthority
from agent_core.domain.messages import (
    AssistantMessage,
    FileReferencePart,
    TextPart,
    ToolResultItem,
)
from agent_core.domain.policies import RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus
from agent_core.domain.trajectory import ArtifactRef
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory, recalled, trace
from tests.contract.semantic_memory_cases import SPACE
from tests.contract.support import NOW, principal, run, session
from tests.contract.test_approval_repository_contract import request as approval_request
from tests.integration.m2_support import memory_settings


async def assert_email_source_erasure(factory: UnitOfWorkFactory) -> None:
    sid, rid, iid, aid, failed_iid = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    belief_id, chat_sid, chat_rid = uuid4(), uuid4(), uuid4()
    metadata = {
        "email_operational": True,
        "email_account_servers": {
            "work": {"read": "gmail_work_read"},
            "personal": {"read": "gmail_personal_read"},
        },
    }
    document = {
        "thread_id": "t1",
        "messages": [
            {"id": "m1", "body": "Sensitive selected source", "from": "ceo@example.test"},
            {"id": "m2", "body": "Keep the other message", "from": "other@example.test"},
        ],
    }
    result = ToolResultItem(
        call_id="c1",
        content=[TextPart(text=json.dumps(document))],
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )
    async with factory() as uow:
        await uow.sessions.create(session().model_copy(update={"id": sid, "metadata": metadata}))
        await uow.runs.create(
            run(status=RunStatus.COMPLETED).model_copy(
                update={
                    "id": rid,
                    "session_id": sid,
                    "final_message": "Sensitive selected source summary",
                }
            )
        )
        source = await uow.events.append(
            NewEvent(
                session_id=sid,
                run_id=rid,
                event_type="tool.call.completed",
                actor_type="runtime",
                payload={
                    "name": "mcp.gmail_work_read.get_thread_page",
                    "result_item": result.model_dump(mode="json"),
                },
            )
        )
        personal = await uow.events.append(
            NewEvent(
                session_id=sid,
                run_id=rid,
                event_type="tool.call.completed",
                actor_type="runtime",
                payload={
                    "name": "mcp.gmail_personal_read.get_thread_page",
                    "result_item": result.model_dump(mode="json"),
                },
            )
        )
        owner = await uow.events.append(
            NewEvent(
                session_id=sid,
                run_id=rid,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=principal().principal_id,
                payload={"content": "Keep the owner's original note"},
            )
        )
        retained = await uow.memories.upsert_belief(
            memory(statement="Keep the owner's original note").model_copy(
                update={"source_session_id": sid, "source_event_ids": [owner.sequence]}
            )
        )
        retained_source = await uow.semantic_memory.capture(principal(), retained.id, SPACE)
        assert retained_source is not None
        await uow.invocations.create(
            ToolInvocation(
                id=iid,
                run_id=rid,
                session_id=sid,
                step_number=1,
                call_id="c1",
                tool_name="mcp.gmail_work_read.get_thread_page",
                tool_version="1",
                side_effect=SideEffectClass.NONE,
                risk=RiskLevel.LOW,
                status=ToolInvocationStatus.SUCCEEDED,
                raw_arguments='{"thread_id":"t1"}',
                idempotency_key=str(iid),
                result_item=result,
                structured_result=document,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        failed_arguments = json.dumps(
            {"thread_id": "t1", "id": "m1", "body": "Preserve the owner's rejected arguments"}
        )
        await uow.invocations.create(
            ToolInvocation(
                id=failed_iid,
                run_id=rid,
                session_id=sid,
                step_number=2,
                call_id="rejected",
                tool_name="mcp.gmail_work_read.get_thread_page",
                tool_version="1",
                side_effect=SideEffectClass.NONE,
                risk=RiskLevel.LOW,
                status=ToolInvocationStatus.FAILED,
                raw_arguments=failed_arguments,
                idempotency_key=str(failed_iid),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await uow.checkpoints.write(
            rid,
            RunCheckpoint(
                run_id=rid,
                version=1,
                status=RunStatus.COMPLETED,
                conversation=[result],
                last_event_sequence=owner.sequence,
                created_at=NOW,
            ),
            full=True,
        )
        await uow.artifacts.create(
            ArtifactRef(
                id=aid,
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                session_id=sid,
                run_id=rid,
                name="derived.txt",
                media_type="text/plain",
                storage_uri="memory://derived",
                sha256="a" * 64,
                size_bytes=12,
                origin="model_output",
                trust=TrustLevel.EXTERNAL_UNTRUSTED,
                expires_at=None,
                created_at=NOW,
            )
        )
    async with factory() as uow:
        await uow.events.append(
            NewEvent(
                session_id=sid,
                run_id=rid,
                event_type="memory.formed",
                actor_type="memory",
                payload={
                    "belief": {
                        "id": str(belief_id),
                        "authority": "inferred",
                        "source_session_id": str(sid),
                        "source_event_ids": [source.sequence],
                        "statement": "Sensitive selected source",
                    }
                },
            )
        )
        await uow.sessions.create(session().model_copy(update={"id": chat_sid}))
        await uow.runs.create(
            run(status=RunStatus.COMPLETED).model_copy(
                update={"id": chat_rid, "session_id": chat_sid}
            )
        )
        context_document = {
            "context": {
                "source": {
                    "account_id": "personal",
                    "provider_thread_id": "t1",
                    "message_ids": ["m1"],
                },
                "thread": {"id": str(uuid4()), "summary": "Keep personal summary"},
                "messages": [
                    {"id": "m1", "body": "Keep personal context", "from": "owner@example.test"}
                ],
            },
            "writing_profile": {
                "examples": [
                    {
                        "source": {
                            "account_id": "work",
                            "provider_thread_id": "t1",
                            "message_ids": ["m1", "m2"],
                        },
                        "thread": {"summary": "Sensitive selected source"},
                        "messages": document["messages"],
                    }
                ],
            },
        }
        context_result = result.model_copy(
            update={"call_id": "context", "content": [TextPart(text=json.dumps(context_document))]}
        )
        context_event = await uow.events.append(
            NewEvent(
                session_id=chat_sid,
                run_id=chat_rid,
                event_type="tool.call.completed",
                actor_type="runtime",
                payload={
                    "name": "email.context",
                    "result_item": context_result.model_dump(mode="json"),
                },
            )
        )
        context_iid = uuid4()
        await uow.invocations.create(
            ToolInvocation(
                id=context_iid,
                run_id=chat_rid,
                session_id=chat_sid,
                step_number=1,
                call_id="context",
                tool_name="email.context",
                tool_version="1",
                side_effect=SideEffectClass.NONE,
                risk=RiskLevel.LOW,
                status=ToolInvocationStatus.SUCCEEDED,
                raw_arguments="{}",
                idempotency_key=str(context_iid),
                result_item=context_result,
                structured_result=context_document,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        selected = recalled(
            belief_id=belief_id.int,
            statement="Sensitive selected source",
            authority=MemoryAuthority.INFERRED,
        )
        independent = recalled(statement="Keep unrelated owner memory")
        recall_trace = trace().model_copy(
            update={
                "id": uuid4(),
                "session_id": chat_sid,
                "run_id": chat_rid,
                "beliefs": [selected, independent],
                "rendered": "Sensitive selected source",
                "returned": [belief_id, independent.belief_id],
            }
        )
        await uow.traces.record(recall_trace)
        await uow.checkpoints.write(
            chat_rid,
            RunCheckpoint(
                run_id=chat_rid,
                version=1,
                status=RunStatus.COMPLETED,
                working_state={"memory_id": str(belief_id), "content": "Sensitive selected source"},
                created_at=NOW,
            ),
            full=True,
        )
    async with factory() as uow:
        await uow.history.catch_up(sid)
        foreign = principal().model_copy(update={"principal_id": "unrelated"})
        assert (
            await uow.session_deletions.erase_email_source(
                foreign, "work", "t1", frozenset({"m1"}), NOW
            )
        ) == {
            "events": 0,
            "invocations": 0,
            "checkpoints": 0,
            "episodes": 0,
            "pending_artifacts": 0,
        }
        counts = await uow.session_deletions.erase_email_source(
            principal(), "work", "t1", frozenset({"m1"}), NOW
        )
        assert counts == {
            "events": 2,
            "invocations": 2,
            "checkpoints": 2,
            "episodes": 0,
            "pending_artifacts": 1,
        }
    async with factory() as uow:
        assert (
            await uow.memories.get_at(
                retained.id, principal(), known_at=datetime.max.replace(tzinfo=UTC)
            )
            == retained
        )
        historical_source = await uow.semantic_memory.capture_revision(
            principal(), retained_source.source, SPACE
        )
        assert historical_source is not None and historical_source.record == retained
        events = await uow.events.list_after(sid, 0, principal())
        redacted = next(event for event in events if event.id == source.id)
        assert "Sensitive selected source" not in json.dumps(redacted.payload)
        assert "Keep the other message" in json.dumps(redacted.payload)
        assert redacted.sequence == source.sequence and redacted.created_at == source.created_at
        assert personal in events and owner in events
        context_events = await uow.events.list_after(chat_sid, 0, principal())
        erased_context = next(event for event in context_events if event.id == context_event.id)
        assert "Sensitive selected source" not in json.dumps(erased_context.payload)
        assert "Keep the other message" in json.dumps(erased_context.payload)
        assert "Keep personal context" in json.dumps(erased_context.payload)
        assert "Keep personal summary" in json.dumps(erased_context.payload)
        context_invocation = await uow.invocations.find_by_idempotency_key(
            chat_rid, str(context_iid)
        )
        assert context_invocation is not None
        assert "Sensitive selected source" not in context_invocation.model_dump_json()
        cached = (await uow.history.read(sid)).model_dump_json()
        assert "Keep the owner's original note" in cached
        assert "Keep the other message" in cached
        invocation = await uow.invocations.find_by_idempotency_key(rid, str(iid))
        assert invocation is not None
        assert "Sensitive selected source" not in invocation.model_dump_json()
        failed_invocation = await uow.invocations.find_by_idempotency_key(rid, str(failed_iid))
        assert failed_invocation is not None
        assert failed_invocation.raw_arguments == failed_arguments
        assert await uow.checkpoints.latest(rid) is None
        assert await uow.checkpoints.latest(chat_rid) is None
        erased_trace = await uow.traces.get(recall_trace.id, principal())
        assert "Sensitive selected source" not in erased_trace.model_dump_json()
        assert erased_trace.beliefs == [independent]
        assert (await uow.runs.get(rid, principal())).final_message is None
        expired = await uow.artifacts.list_expired(NOW, limit=100)
        assert aid in {artifact.id for artifact in expired}
        repeated = await uow.session_deletions.erase_email_source(
            principal(), "work", "t1", frozenset({"m1"}), NOW
        )
        assert repeated["events"] == repeated["invocations"] == repeated["checkpoints"] == 0
        assert repeated["pending_artifacts"] == 1
        assert await uow.artifacts.delete_expired(aid, now=NOW)
    async with factory() as uow:
        assert (
            await uow.session_deletions.erase_email_source(
                principal(), "work", "t1", frozenset({"m1"}), NOW
            )
        )["pending_artifacts"] == 0


async def assert_saved_attachment_erased(factory: UnitOfWorkFactory) -> None:
    """A plain chat has no Gmail metadata, and the file name is in its calls (ADR-0295)."""
    sid, rid, iid, aid = uuid4(), uuid4(), uuid4(), uuid4()
    other_iid, kept_aid, unreceipted_rid, unreceipted_aid, unknown_aid = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    promoted_aid, promoted_erased_aid = uuid4(), uuid4()

    def chip(artifact_id: Any) -> FileReferencePart:
        return FileReferencePart(
            artifact_id=artifact_id, media_type="application/pdf", filename="chip.pdf"
        )

    name = "mcp.gmail_read.save_attachment"
    arguments = {"message_id": "m1", "attachment_index": 0, "filename": "invoice.pdf"}
    other = {"message_id": "m2", "attachment_index": 0, "filename": "keep.pdf"}
    receipt = {"thread_id": "t1", **arguments, "artifact_id": str(aid)}
    item = ToolResultItem(
        call_id="c1",
        content=[TextPart(text=json.dumps(receipt))],
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )
    wrapped = {"name": name, "arguments": arguments}

    def invocation(identifier: Any, raw: dict[str, Any], **extra: Any) -> ToolInvocation:
        return ToolInvocation(
            id=identifier,
            run_id=rid,
            session_id=sid,
            step_number=1,
            call_id=str(identifier),
            tool_name=name,
            tool_version="1",
            side_effect=SideEffectClass.NETWORK_READ,
            risk=RiskLevel.LOW,
            status=ToolInvocationStatus.SUCCEEDED,
            raw_arguments=json.dumps(raw),
            idempotency_key=str(identifier),
            created_at=NOW,
            updated_at=NOW,
            **extra,
        )

    async with factory() as uow:
        await uow.sessions.create(session().model_copy(update={"id": sid}))
        await uow.runs.create(
            run(status=RunStatus.COMPLETED).model_copy(update={"id": rid, "session_id": sid})
        )
        for event_type, payload in (
            (
                "model.response.completed",
                {"conversation_items": [], "tool_calls": [{"name": "tool.call", **wrapped}]},
            ),
            ("tool.call.proposed", {"name": "tool.call", "arguments": wrapped}),
            ("tool.call.completed", {"name": name, "result_item": item.model_dump(mode="json")}),
            (
                "assistant.message.completed",
                {
                    "message": AssistantMessage(
                        content=[
                            TextPart(text="Saved invoice.pdf and keep.pdf"),
                            chip(aid),
                            chip(kept_aid),
                            chip(promoted_aid),
                        ]
                    ).model_dump(mode="json")
                },
            ),
        ):
            await uow.events.append(
                NewEvent(
                    session_id=sid,
                    run_id=rid,
                    event_type=event_type,
                    actor_type="runtime",
                    payload=payload,
                )
            )
        await uow.invocations.create(
            invocation(iid, arguments, result_item=item, structured_result=receipt)
        )
        await uow.invocations.create(invocation(other_iid, other))
        # A crash can lose the receipt, so this run names no file at all.
        await uow.runs.create(
            run(status=RunStatus.COMPLETED).model_copy(
                update={"id": unreceipted_rid, "session_id": sid}
            )
        )
        saved_files: list[tuple[Any, Any, dict[str, Any], Any]] = [
            (
                aid,
                rid,
                {"auto_ingest": "pending", "email_source": {"message_id": "m1"}},
                "email_attachment",
            ),
            # One run can save from two emails; this file is the other email's.
            (
                kept_aid,
                rid,
                {"auto_ingest": "pending", "email_source": {"message_id": "m2"}},
                "email_attachment",
            ),
            (
                unreceipted_aid,
                unreceipted_rid,
                {"email_source": {"message_id": "m1"}},
                "email_attachment",
            ),
            # A source nobody can read is treated as the email being erased.
            (unknown_aid, rid, {}, "email_attachment"),
            # Knowledge taking a file over changes its origin, not its source.
            (promoted_aid, rid, {"email_source": {"message_id": "m2"}}, "knowledge_source"),
            (
                promoted_erased_aid,
                unreceipted_rid,
                {"email_source": {"message_id": "m1"}},
                "knowledge_source",
            ),
        ]
        for artifact_id, artifact_run, metadata, origin in saved_files:
            await uow.artifacts.create(
                ArtifactRef(
                    id=artifact_id,
                    tenant_id=principal().tenant_id,
                    principal_id=principal().principal_id,
                    session_id=sid,
                    run_id=artifact_run,
                    name="invoice.pdf",
                    media_type="application/pdf",
                    storage_uri=f"memory://{artifact_id}",
                    sha256="a" * 64,
                    size_bytes=1,
                    origin=origin,
                    trust=TrustLevel.EXTERNAL_UNTRUSTED,
                    expires_at=None,
                    created_at=NOW,
                    metadata=metadata,
                )
            )
        await uow.approvals.create(
            approval_request().model_copy(
                update={
                    "session_id": sid,
                    "run_id": rid,
                    "action_id": iid,
                    "tool_invocation_id": iid,
                    "tool_name": name,
                    "arguments": arguments,
                }
            )
        )
    async with factory() as uow:
        await uow.history.catch_up(sid)
    async with factory() as uow:
        pending = await uow.artifacts.pending_auto_ingest(principal(), now=NOW, limit=9)
    assert {item.id for item in pending} == {aid, kept_aid}
    # What an intake already under way read before this erasure.
    read_before_erasure = next(item for item in pending if item.id == aid)
    async with factory() as uow:
        counts = await uow.session_deletions.erase_email_source(
            principal(), "work", "t1", frozenset({"m1"}), NOW
        )
    assert counts["pending_artifacts"] == 4
    # An erased file must not come back through a knowledge intake still pending.
    async with factory() as uow:
        assert [
            item.id
            for item in await uow.artifacts.pending_auto_ingest(principal(), now=NOW, limit=9)
        ] == [kept_aid]
    with pytest.raises(NotFoundError):
        async with factory() as uow:
            await uow.artifacts.retain_for_knowledge(aid, principal(), now=NOW)
    # A cutoff sampled just before the erasure must not bring the file back.
    with pytest.raises(NotFoundError):
        async with factory() as uow:
            await uow.artifacts.retain_for_knowledge(
                aid,
                principal(),
                now=NOW - timedelta(seconds=1),
                unchanged_from=read_before_erasure,
            )
    async with factory() as uow:
        events = await uow.events.list_after(sid, 0, principal())
        recorded = {item.id: item for item in await uow.invocations.list_for_run(rid, principal())}
        expired = await uow.artifacts.list_expired(NOW, limit=100)
        approval = await uow.approvals.get_by_action(iid)
    assert approval is not None and "invoice.pdf" not in approval.model_dump_json()
    assert len(events) == 4
    for event in events:
        assert "invoice.pdf" not in json.dumps(event.payload)
    # The reply's text goes; a file that outlives this erasure keeps its link.
    reply = events[3].payload["message"]["content"]
    assert [part.get("artifact_id") for part in reply] == [None, str(kept_aid), str(promoted_aid)]
    assert "keep.pdf" not in json.dumps(reply)
    async with factory() as uow:
        history = await uow.history.read(sid)
    [cached] = [entry for entry in history.items if isinstance(entry, AssistantMessage)]
    assert [getattr(part, "artifact_id", None) for part in cached.content] == [
        None,
        kept_aid,
        promoted_aid,
    ]
    # The call is still identifiable; only the sender's name is gone.
    assert "save_attachment" in json.dumps(events[0].payload)
    assert '"m1"' in json.dumps(events[0].payload)
    assert "invoice.pdf" not in recorded[iid].model_dump_json()
    assert json.loads(recorded[other_iid].raw_arguments)["filename"] == "keep.pdf"
    expired_ids = {artifact.id for artifact in expired}
    assert {aid, unreceipted_aid, unknown_aid, promoted_erased_aid} <= expired_ids
    assert not {kept_aid, promoted_aid} & expired_ids


async def assert_denied_save_erased(factory: UnitOfWorkFactory) -> None:
    """A denied save leaves no receipt, only the calls that named the file (ADR-0295)."""
    sid, rid, iid = uuid4(), uuid4(), uuid4()
    name = "mcp.gmail_read.save_attachment"
    arguments = {"message_id": "m1", "attachment_index": 0, "filename": "invoice.pdf"}
    async with factory() as uow:
        await uow.sessions.create(session().model_copy(update={"id": sid}))
        await uow.runs.create(
            run(status=RunStatus.COMPLETED).model_copy(
                update={"id": rid, "session_id": sid, "final_message": "invoice.pdf was denied"}
            )
        )
        await uow.events.append(
            NewEvent(
                session_id=sid,
                run_id=rid,
                event_type="model.response.completed",
                actor_type="runtime",
                payload={"tool_calls": [{"name": "tool.call", "arguments": json.dumps(arguments)}]},
            )
        )
        await uow.invocations.create(
            ToolInvocation(
                id=iid,
                run_id=rid,
                session_id=sid,
                step_number=1,
                call_id="denied",
                tool_name=name,
                tool_version="1",
                side_effect=SideEffectClass.NETWORK_READ,
                risk=RiskLevel.LOW,
                status=ToolInvocationStatus.DENIED,
                raw_arguments=json.dumps(arguments),
                idempotency_key=str(iid),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await uow.approvals.create(
            approval_request().model_copy(
                update={
                    "session_id": sid,
                    "run_id": rid,
                    "action_id": iid,
                    "tool_invocation_id": iid,
                    "tool_name": name,
                    "arguments": arguments,
                }
            )
        )
    async with factory() as uow:
        await uow.session_deletions.erase_email_source(
            principal(), "work", "t1", frozenset({"m1"}), NOW
        )
    async with factory() as uow:
        [event] = await uow.events.list_after(sid, 0, principal())
        [invocation] = await uow.invocations.list_for_run(rid, principal())
        approval = await uow.approvals.get_by_action(iid)
        finished = await uow.runs.get(rid, principal())
    assert "invoice.pdf" not in json.dumps(event.payload)
    assert "invoice.pdf" not in invocation.model_dump_json()
    assert approval is not None and "invoice.pdf" not in approval.model_dump_json()
    assert finished.final_message is None


async def test_memory_denied_save_erasure_contract() -> None:
    async with build(
        settings=memory_settings(), storage="memory", principal=principal()
    ) as composition:
        await assert_denied_save_erased(composition.uow_factory)


async def test_memory_saved_attachment_erasure_contract() -> None:
    async with build(
        settings=memory_settings(), storage="memory", principal=principal()
    ) as composition:
        await assert_saved_attachment_erased(composition.uow_factory)


async def test_memory_source_erasure_contract() -> None:
    async with build(
        settings=memory_settings(), storage="memory", principal=principal()
    ) as composition:
        await assert_email_source_erasure(composition.uow_factory)


async def assert_erasure_waits_for_potential_producer(
    factory: UnitOfWorkFactory,
    kind: str,
) -> None:
    sid, rid, iid = uuid4(), uuid4(), uuid4()
    metadata = (
        {"email_account_servers": {"work": {"read": "gmail_work_read"}}} if kind == "bound" else {}
    )
    scopes = (
        {"email.read"}
        if kind == "chat_scope"
        else {"mcp.gmail_work_read.read"}
        if kind == "gmail_scope"
        else set()
    )
    async with factory() as uow:
        await uow.sessions.create(session().model_copy(update={"id": sid, "metadata": metadata}))
        await uow.runs.create(
            run(status=RunStatus.RUNNING).model_copy(
                update={"id": rid, "session_id": sid, "principal_scopes": scopes}
            )
        )
        if kind == "invocation":
            await uow.invocations.create(
                ToolInvocation(
                    id=iid,
                    session_id=sid,
                    run_id=rid,
                    step_number=1,
                    call_id="pending",
                    tool_name="email.context",
                    tool_version="1",
                    side_effect=SideEffectClass.NONE,
                    risk=RiskLevel.LOW,
                    status=ToolInvocationStatus.RUNNING,
                    raw_arguments="{}",
                    idempotency_key=str(iid),
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
    if kind == "unrelated":
        async with factory() as uow:
            counts = await uow.session_deletions.erase_email_source(
                principal(), "work", "t1", frozenset({"m1"}), NOW
            )
            assert all(value == 0 for value in counts.values())
        return
    with pytest.raises(ConflictError, match="settle"):
        async with factory() as uow:
            await uow.session_deletions.erase_email_source(
                principal(), "work", "t1", frozenset({"m1"}), NOW
            )
    async with factory() as uow:
        await uow.runs.transition(rid, RunStatus.RUNNING, RunStatus.COMPLETED)
        counts = await uow.session_deletions.erase_email_source(
            principal(), "work", "t1", frozenset({"m1"}), NOW
        )
        assert all(value == 0 for value in counts.values())


@pytest.mark.parametrize("kind", ["bound", "chat_scope", "gmail_scope", "invocation", "unrelated"])
async def test_memory_erasure_waits_for_inflight_source_producer(kind: str) -> None:
    async with build(
        settings=memory_settings(), storage="memory", principal=principal()
    ) as composition:
        await assert_erasure_waits_for_potential_producer(composition.uow_factory, kind)
