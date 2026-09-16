"""Forgetting removes person influence, preserves others, and returns a safe receipt."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_core.application.people import PublicPeopleService
from agent_core.application.people_erasure import PeopleErasureService
from agent_core.domain.agents import Principal
from agent_core.domain.memory import RecallResult, Sensitivity
from agent_core.domain.people import PersonMemoryLink
from agent_core.domain.people_views import CreatePerson, PeopleForgetRequest
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory
from tests.contract.support import NOW, memory_uow_factory, principal, session


def test_generated_copy_redaction_covers_working_state_and_structured_failure_details() -> None:
    from agent_core.adapters.persistence.people_erasure import redact

    value = {
        "working_state": {
            "objective": "Sam likes chess",
            "constraints": ["Sam likes chess"],
            "tasks": [{"description": "Sam likes chess", "status": "open"}],
            "open_questions": ["Sam likes chess"],
            "next_action": "Sam likes chess",
        },
        "failure": {"message": "Sam likes chess", "details": {"custom": "Sam likes chess"}},
        "compacted_summary": "Sam likes chess",
    }
    assert "Sam likes chess" not in str(redact(value))


async def test_runtime_cancels_after_people_erasure_without_restoring_a_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio
    from collections.abc import AsyncIterator

    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.bootstrap import build
    from agent_core.domain.messages import (
        FakeModelScript,
        ModelAttempt,
        ModelEvent,
        ModelRequest,
        ResolvedModel,
        ScriptedTurn,
    )
    from agent_core.domain.runs import RunStatus
    from tests.integration.m2_support import memory_settings

    original_stream = FakeModelProvider.stream
    snapshot_registered = False

    async def erase_before_response(
        provider: FakeModelProvider,
        request: ModelRequest,
        resolved: ResolvedModel,
        attempt: ModelAttempt,
    ) -> AsyncIterator[ModelEvent]:
        nonlocal snapshot_registered
        async with app.uow_factory() as uow:
            active = await uow.runs.get(attempt.run_id, app.principal)
            events = await uow.events.list_after(active.session_id, 0, app.principal)
            snapshot_registered = any(
                event.event_type == "context.snapshot.used" for event in events
            )
            await uow.session_deletions.erase_people_copies(
                app.principal, [seed.id], app.clock.now()
            )
        async for event in original_stream(provider, request, resolved, attempt):
            yield event

    monkeypatch.setattr(FakeModelProvider, "stream", erase_before_response)
    async with build(
        settings=memory_settings(),
        storage="memory",
        script=FakeModelScript(turns=[ScriptedTurn(text="Sam likes chess")]),
        fixed_clock_at=NOW,
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(
                session().model_copy(
                    update={
                        "tenant_id": app.principal.tenant_id,
                        "principal_id": app.principal.principal_id,
                    }
                )
            )
            seed = memory(statement="Sam likes chess").model_copy(
                update={
                    "tenant_id": app.principal.tenant_id,
                    "principal_id": app.principal.principal_id,
                }
            )
            await uow.memories.upsert_belief(seed)
        run_id = await app.runs.submit("Prepare my meeting notes.")
        final = await asyncio.wait_for(app.runs.wait_terminal(run_id), timeout=3)
        assert snapshot_registered, (
            "runtime must register the frozen snapshot before provider egress"
        )
        assert final.status is RunStatus.CANCELLED and final.final_message is None, [
            getattr(record, "error_class", record.message) for record in caplog.records
        ]
        async with app.uow_factory() as uow:
            assert await uow.checkpoints.latest(run_id) is None
            events = await uow.events.list_after(final.session_id, 0, app.principal)
        assert all("Sam likes chess" not in event.model_dump_json() for event in events)


async def test_forgetting_long_history_uses_bounded_receipts_and_preserves_suppression() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    await long_erasure_contract(factory, clock, owner, count=2001)


async def long_erasure_contract(
    factory: UnitOfWorkFactory,
    clock: Clock,
    owner: Principal,
    *,
    count: int,
) -> None:
    from agent_core.domain.people import PeopleQuery, PeopleSource, PersonMention

    person = await PublicPeopleService(factory, clock).create(
        owner,
        CreatePerson(session_id=session().id, display_name="Sam"),
        key="sam",
        ceiling=Sensitivity.SENSITIVE,
    )
    sources = []
    async with factory() as uow:
        original = await uow.people.get(owner, person.support_ids[0], ceiling=Sensitivity.SENSITIVE)
        assert isinstance(original, PeopleSource)
        for _ in range(count):
            source = original.model_copy(update={"id": uuid4()})
            sources.append(source.id)
            await uow.people.put(source, expected_revision=0)
            await uow.people.put(
                PersonMention(
                    id=uuid4(),
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    created_at=NOW,
                    updated_at=NOW,
                    source_id=source.id,
                    person_id=person.id,
                    start=0,
                    end=3,
                    support_ids=[source.id],
                ),
                expected_revision=0,
            )
    erasure = PeopleErasureService(factory, clock)
    preview = await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(
            session_id=session().id,
            phase="preview",
            expected_revision=1,
        ),
        key="preview",
        ceiling=Sensitivity.SENSITIVE,
    )
    assert preview.counts["mention"] == count
    complete = await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(
            session_id=session().id,
            phase="apply",
            expected_revision=preview.revision,
            operation_id=preview.id,
        ),
        key="apply",
        ceiling=Sensitivity.SENSITIVE,
    )
    assert complete.state == "cleanup_pending"
    complete = await erasure.get(owner, complete.id, ceiling=Sensitivity.SENSITIVE)
    assert complete.state == "completed"
    async with factory() as uow:
        assert await uow.people.get(owner, person.id, ceiling=Sensitivity.SENSITIVE) is None
        for source_id in sources:
            assert await uow.people.source_suppressed(owner, source_id)
        from agent_core.domain.people_sources import source_id as event_source_id

        assert await uow.people.source_suppressed(
            owner, event_source_id(owner, original.session_id, original.event_sequence)
        )
        page = await uow.people.query(
            PeopleQuery(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                kinds=["erasure"],
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                limit=100,
            )
        )
        assert len(page) > 1
        assert all(len(row.model_dump_json()) < 150_000 for row in page)


async def test_forgetting_is_previewed_and_erases_only_dependent_memories() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    service = PublicPeopleService(factory, clock)
    people = [
        await service.create(
            owner,
            CreatePerson(session_id=session().id, display_name=name),
            key=name,
            ceiling=Sensitivity.SENSITIVE,
        )
        for name in ("Sam", "Alex")
    ]
    beliefs = [
        memory(belief_id=500 + i, statement=f"{p.display_name} likes chess")
        for i, p in enumerate(people)
    ]
    async with factory() as uow:
        for person, belief in zip(people, beliefs, strict=True):
            await uow.memories.upsert_belief(belief)
            await uow.people.put(
                PersonMemoryLink(
                    id=uuid4(),
                    person_id=person.id,
                    belief_id=belief.id,
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                expected_revision=0,
            )
    erasure = PeopleErasureService(factory, clock)
    preview = await erasure.forget(
        owner,
        people[0].id,
        PeopleForgetRequest(session_id=session().id, phase="preview", expected_revision=1),
        key="preview",
        ceiling=Sensitivity.SENSITIVE,
    )
    assert preview.state == "preview" and preview.counts["beliefs"] == 1
    async with factory() as uow:
        assert await uow.people.get(owner, people[0].id, ceiling=Sensitivity.SENSITIVE) is not None
    request = PeopleForgetRequest(
        session_id=session().id,
        phase="apply",
        expected_revision=preview.revision,
        operation_id=preview.id,
    )
    complete = await erasure.forget(
        owner, people[0].id, request, key="apply", ceiling=Sensitivity.SENSITIVE
    )
    assert complete.state == "completed"
    async with factory() as uow:
        from agent_core.application.people_erasure_batches import read_manifest
        from agent_core.domain.people import PeopleErasure

        receipt = await uow.people.get(owner, complete.id, ceiling=Sensitivity.SENSITIVE)
        assert isinstance(receipt, PeopleErasure)
        manifest = await read_manifest(uow.people, owner, receipt)
        assert people[0].id in getattr(manifest, "blocked_record_ids", []), (
            "completed receipts must retain opaque identities for post-snapshot replay"
        )
        assert beliefs[0].id in getattr(manifest, "blocked_belief_ids", [])
        assert people[1].id not in manifest.blocked_record_ids
        assert beliefs[1].id not in manifest.blocked_belief_ids
    assert (
        await erasure.forget(
            owner, people[0].id, request, key="apply", ceiling=Sensitivity.SENSITIVE
        )
        == complete
    )
    async with factory() as uow:
        assert await uow.people.get(owner, people[0].id, ceiling=Sensitivity.SENSITIVE) is None
        assert await uow.people.get(owner, people[1].id, ceiling=Sensitivity.SENSITIVE) is not None
        assert [row.id for row in await uow.memories.list_memories(owner)] == [beliefs[1].id]
        assert await uow.sessions.get(session().id, owner) is not None


async def test_forgetting_scrubs_memory_audit_copies_but_keeps_owner_message() -> None:
    from agent_core.domain.events import NewEvent

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    service = PublicPeopleService(factory, clock)
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Sam"),
        key="sam",
        ceiling=Sensitivity.SENSITIVE,
    )
    belief = memory(statement="Sam likes chess")
    async with factory() as uow:
        await uow.memories.upsert_belief(belief)
        await uow.people.put(
            PersonMemoryLink(
                id=uuid4(),
                person_id=person.id,
                belief_id=belief.id,
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                created_at=NOW,
                updated_at=NOW,
            ),
            expected_revision=0,
        )
        original = await uow.events.append(
            NewEvent(
                session_id=session().id,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=owner.principal_id,
                payload={"content": "Sam likes chess"},
            )
        )
        audit = await uow.events.append(
            NewEvent(
                session_id=session().id,
                run_id=None,
                event_type="memory.formed",
                actor_type="principal",
                actor_id=owner.principal_id,
                payload={"belief": belief.model_dump(mode="json")},
            )
        )
    erasure = PeopleErasureService(factory, clock)
    preview = await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(session_id=session().id, phase="preview", expected_revision=1),
        key="preview",
        ceiling=Sensitivity.SENSITIVE,
    )
    await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(
            session_id=session().id, phase="apply", expected_revision=1, operation_id=preview.id
        ),
        key="apply",
        ceiling=Sensitivity.SENSITIVE,
    )
    async with factory() as uow:
        events = await uow.events.list_after(session().id, 0, owner)
        assert next(e for e in events if e.id == original.id).payload == original.payload
        assert (
            "Sam likes chess" not in next(e for e in events if e.id == audit.id).model_dump_json()
        )


async def test_forget_blocks_old_source_reextraction_without_hiding_other_supported_people() -> (
    None
):
    from agent_core.domain.people import Person

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    service = PublicPeopleService(factory, clock)
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Sam"),
        key="sam",
        ceiling=Sensitivity.SENSITIVE,
    )
    other = Person(
        id=uuid4(),
        display_name="Alex",
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        created_at=NOW,
        updated_at=NOW,
        support_ids=person.support_ids,
    )
    async with factory() as uow:
        await uow.people.put(other, expected_revision=0)
    erasure = PeopleErasureService(factory, clock)
    preview = await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(session_id=session().id, phase="preview", expected_revision=1),
        key="preview",
        ceiling=Sensitivity.SENSITIVE,
    )
    await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(
            session_id=session().id, phase="apply", expected_revision=1, operation_id=preview.id
        ),
        key="apply",
        ceiling=Sensitivity.SENSITIVE,
    )
    async with factory() as uow:
        assert await uow.people.source_suppressed(owner, person.support_ids[0])
        assert await uow.people.get(owner, other.id, ceiling=Sensitivity.SENSITIVE) == other


@pytest.mark.parametrize("artifact_count", [1, 3001])
async def test_cleanup_receipt_completes_only_after_generated_artifact_is_deleted(
    artifact_count: int,
) -> None:
    from agent_core.domain.events import NewEvent
    from agent_core.domain.runs import RunStatus
    from tests.contract.support import run
    from tests.contract.test_artifact_repository_contract import _artifact

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    person = await PublicPeopleService(factory, clock).create(
        owner,
        CreatePerson(session_id=session().id, display_name="Sam"),
        key="sam",
        ceiling=Sensitivity.SENSITIVE,
    )
    completed_run = run(status=RunStatus.COMPLETED)
    artifact = _artifact().model_copy(
        update={
            "tenant_id": owner.tenant_id,
            "principal_id": owner.principal_id,
            "session_id": session().id,
            "run_id": completed_run.id,
        }
    )
    artifacts = [artifact.model_copy(update={"id": uuid4()}) for _ in range(artifact_count)]
    async with factory() as uow:
        await uow.runs.create(completed_run)
        for item in artifacts:
            await uow.artifacts.create(item)
        await uow.events.append(
            NewEvent(
                session_id=session().id,
                run_id=completed_run.id,
                event_type="assistant.message.completed",
                actor_type="agent",
                actor_id="agent",
                payload={"person_id": str(person.id), "content": "Sam likes chess"},
            )
        )
    erasure = PeopleErasureService(factory, clock)
    preview = await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(session_id=session().id, phase="preview", expected_revision=1),
        key="preview",
        ceiling=Sensitivity.SENSITIVE,
    )
    receipt = await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(
            session_id=session().id, phase="apply", expected_revision=1, operation_id=preview.id
        ),
        key="apply",
        ceiling=Sensitivity.SENSITIVE,
    )
    assert receipt.state == "cleanup_pending"
    assert (
        await erasure.get(owner, receipt.id, ceiling=Sensitivity.SENSITIVE)
    ).state == "cleanup_pending"
    async with factory() as uow:
        for item in artifacts[:-1]:
            assert await uow.artifacts.delete_expired(item.id, now=clock.now())
    assert (
        await erasure.get(owner, receipt.id, ceiling=Sensitivity.SENSITIVE)
    ).state == "cleanup_pending"
    async with factory() as uow:
        assert await uow.artifacts.delete_expired(artifacts[-1].id, now=clock.now())
    assert (
        await erasure.get(owner, receipt.id, ceiling=Sensitivity.SENSITIVE)
    ).state == "completed"


@pytest.mark.parametrize("assignments", [0, 300])
@pytest.mark.parametrize("reference", ["event", "recall"])
async def test_forget_fences_immediately_and_finishes_copies_after_active_run_settles(
    assignments: int,
    reference: str,
) -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    await active_erasure_contract(
        factory, clock, owner, assignments=assignments, reference=reference
    )


async def active_erasure_contract(
    factory: UnitOfWorkFactory,
    clock: Clock,
    owner: Principal,
    *,
    assignments: int,
    reference: str,
    late_artifact_count: int = 1,
) -> None:
    from agent_core.domain.errors import RunCancelledError
    from agent_core.domain.events import NewEvent
    from agent_core.domain.policies import RiskLevel, SideEffectClass
    from agent_core.domain.runs import RunCheckpoint, RunStatus
    from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus
    from tests.contract.support import run
    from tests.contract.test_artifact_repository_contract import _artifact

    service = PublicPeopleService(factory, clock)
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Sam"),
        key="sam-active",
        ceiling=Sensitivity.SENSITIVE,
    )
    from agent_core.domain.people import PersonMention

    async with factory() as uow:
        for _ in range(assignments):
            await uow.people.put(
                PersonMention(
                    id=uuid4(),
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    created_at=NOW,
                    updated_at=NOW,
                    source_id=person.support_ids[0],
                    person_id=person.id,
                    start=0,
                    end=3,
                    support_ids=person.support_ids,
                ),
                expected_revision=0,
            )
    active = run(status=RunStatus.RUNNING).model_copy(update={"id": uuid4()})
    invocation = ToolInvocation(
        id=uuid4(),
        run_id=active.id,
        session_id=session().id,
        step_number=1,
        call_id="people-erasure-call",
        tool_name="memory.recall",
        tool_version="1.0.0",
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        status=ToolInvocationStatus.RUNNING,
        raw_arguments='{"query":"Sam likes chess"}',
        idempotency_key=str(uuid4()),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    async with factory() as uow:
        await uow.runs.create(active)
        await uow.invocations.create(invocation)
        if assignments:
            for _ in range(260):
                await uow.events.append(
                    NewEvent(
                        session_id=session().id,
                        run_id=active.id,
                        event_type="assistant.message.delta",
                        actor_type="agent",
                        payload={"content": "Sam likes chess"},
                    )
                )
        if reference == "recall":
            from tests.contract.memory_fixtures import trace

            value = trace().model_copy(update={"run_id": active.id})
            await uow.traces.record(
                value.model_copy(
                    update={
                        "query": value.query.model_copy(update={"people_scope": (person.id,)}),
                    }
                )
            )
            completed = run(status=RunStatus.COMPLETED).model_copy(update={"id": uuid4()})
            await uow.runs.create(completed)
            await uow.traces.record(
                value.model_copy(
                    update={
                        "id": uuid4(),
                        "run_id": completed.id,
                        "query": value.query.model_copy(update={"people_scope": (person.id,)}),
                    }
                )
            )
            await uow.events.append(
                NewEvent(
                    session_id=session().id,
                    run_id=completed.id,
                    event_type="assistant.message.completed",
                    actor_type="agent",
                    payload={"content": "Sam likes chess"},
                )
            )
        else:
            await uow.events.append(
                NewEvent(
                    session_id=session().id,
                    run_id=active.id,
                    event_type="assistant.message.created",
                    actor_type="agent",
                    payload={"person_id": str(person.id), "content": "Sam likes chess"},
                )
            )
    erasure = PeopleErasureService(factory, clock)
    preview = await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(
            session_id=session().id,
            phase="preview",
            expected_revision=1,
        ),
        key="active-preview",
        ceiling=Sensitivity.SENSITIVE,
    )
    applied = await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(
            session_id=session().id,
            phase="apply",
            expected_revision=preview.revision,
            operation_id=preview.id,
        ),
        key="active-apply",
        ceiling=Sensitivity.SENSITIVE,
    )
    assert applied.state == "cleanup_pending"
    assert (
        await erasure.get(owner, applied.id, ceiling=Sensitivity.SENSITIVE)
    ).state == "cleanup_pending"
    async with factory() as uow:
        assert await uow.people.get(owner, person.id, ceiling=Sensitivity.SENSITIVE) is None
        assert (await uow.runs.get(active.id, owner)).cancel_requested_at is not None
        visible = await uow.events.list_after(session().id, 0, owner)
        assert all("Sam likes chess" not in event.model_dump_json() for event in visible)
        with pytest.raises(RunCancelledError, match="People erasure"):
            await uow.checkpoints.write(
                active.id,
                RunCheckpoint(
                    run_id=active.id,
                    version=1,
                    status=RunStatus.RUNNING,
                    compacted_summary="Sam likes chess",
                    created_at=clock.now(),
                ),
                full=True,
            )
        assert await uow.checkpoints.latest(active.id) is None
        for stored in await uow.invocations.list_for_run(active.id, owner):
            assert "Sam likes chess" not in stored.model_dump_json()
        with pytest.raises(RunCancelledError, match="People erasure"):
            await uow.invocations.create(
                invocation.model_copy(update={"id": uuid4(), "idempotency_key": str(uuid4())})
            )
        with pytest.raises(RunCancelledError, match="People erasure"):
            await uow.invocations.transition(
                invocation.id,
                ToolInvocationStatus.RUNNING,
                invocation.model_copy(update={"status": ToolInvocationStatus.SUCCEEDED}),
            )
        # A late provider result need not repeat the original context identifiers.
        late = await uow.events.append(
            NewEvent(
                session_id=session().id,
                run_id=active.id,
                event_type="assistant.message.completed",
                actor_type="agent",
                payload={"content": "Sam likes chess"},
            )
        )
        assert "Sam likes chess" not in late.model_dump_json()
        visible = await uow.events.list_after(session().id, 0, owner)
        assert all("Sam likes chess" not in event.model_dump_json() for event in visible)
        terminal = RunCheckpoint(
            run_id=active.id,
            version=2,
            status=RunStatus.CANCELLED,
            compacted_summary="Sam likes chess",
            created_at=clock.now(),
        )
        # Runtime finalization still advances the run after its ordinary
        # checkpoint write. Erasure must not trap cancellation in a retry loop.
        assert await uow.checkpoints.write(active.id, terminal, full=True) == terminal.version
        assert await uow.checkpoints.latest(active.id) is None
        await uow.runs.transition(
            active.id, RunStatus.RUNNING, RunStatus.CANCELLED, final_message="Sam likes chess"
        )
        assert (await uow.runs.get(active.id, owner)).final_message is None
        late_artifacts = []
        for _ in range(late_artifact_count):
            late_artifact = await uow.artifacts.create(
                _artifact().model_copy(
                    update={
                        "id": uuid4(),
                        "tenant_id": owner.tenant_id,
                        "principal_id": owner.principal_id,
                        "session_id": session().id,
                        "run_id": active.id,
                    }
                )
            )
            assert late_artifact.expires_at is not None and late_artifact.expires_at <= clock.now()
            late_artifacts.append(late_artifact)
        with pytest.raises(RunCancelledError, match="People erasure"):
            await uow.artifacts.retain_for_knowledge(late_artifacts[0].id, owner)
        from datetime import timedelta

        extended = await uow.artifacts.expire(
            late_artifacts[0].id, owner, clock.now() + timedelta(days=30)
        )
        assert extended.expires_at is not None and extended.expires_at <= clock.now()
        from tests.contract.memory_fixtures import prepared_knowledge

        prepared = prepared_knowledge()
        with pytest.raises(RunCancelledError, match="People erasure"):
            await uow.knowledge.ingest(
                prepared.model_copy(
                    update={
                        "document": prepared.document.model_copy(
                            update={"source_ref": late_artifacts[0]}
                        )
                    }
                )
            )
        from tests.contract.memory_fixtures import trace

        with pytest.raises(RunCancelledError, match="People erasure"):
            await uow.traces.record(trace().model_copy(update={"id": uuid4(), "run_id": active.id}))
    assert await erasure.resume_pending(owner, limit=1) == 1
    assert (
        await erasure.get(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
    ).state == "cleanup_pending"
    root, pages = await erasure.export(owner, preview.id)
    assert all(part.batch_count == 0 for part in pages)
    assert sum(len(part.blocked_record_ids) for part in pages or [root]) >= assignments + 1
    async with factory() as uow:
        for artifact in late_artifacts[:-1]:
            assert await uow.artifacts.delete_expired(artifact.id, now=clock.now())
    assert (
        await erasure.get(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
    ).state == "cleanup_pending"
    async with factory() as uow:
        assert await uow.artifacts.delete_expired(late_artifacts[-1].id, now=clock.now())
    finished = await erasure.get(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
    assert finished.state == "completed"
    async with factory() as uow:
        events = await uow.events.list_after(session().id, 0, owner)
    assert all("Sam likes chess" not in event.model_dump_json() for event in events)


async def test_late_artifact_batches_preserve_exportable_erasure_receipts() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    await active_erasure_contract(
        factory, clock, owner, assignments=300, reference="recall", late_artifact_count=3001
    )


async def test_forget_removes_email_assessment_copies_and_preserves_original_message() -> None:
    from agent_core.domain.email import EmailRecord

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    service = PublicPeopleService(factory, clock)
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Sam"),
        key="sam-mail",
        ceiling=Sensitivity.SENSITIVE,
    )
    belief = memory(statement="Sam likes chess")
    thread_id = str(uuid4())
    async with factory() as uow:
        await uow.memories.upsert_belief(belief)
        await uow.people.put(
            PersonMemoryLink(
                id=uuid4(),
                person_id=person.id,
                belief_id=belief.id,
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                created_at=NOW,
                updated_at=NOW,
            ),
            expected_revision=0,
        )
        records: list[tuple[str, str, dict[str, Any]]] = [
            (
                "semantic_source",
                "source",
                {
                    "memory_ids": [str(belief.id)],
                    "facts": {"fact-digest": str(belief.id)},
                    "account_id": "work",
                    "provider_thread_id": "thread",
                },
            ),
            (
                "thread",
                thread_id,
                {
                    "account_id": "work",
                    "provider_thread_id": "thread",
                    "summary": "Sam likes chess",
                    "messages": [{"body": "Sam likes chess"}],
                },
            ),
            (
                "assessment",
                thread_id,
                {
                    "summary": "Sam likes chess",
                    "people_facts": [{"value": "Sam likes chess"}],
                    "semantic_facts": [],
                },
            ),
        ]
        for kind, key, payload in records:
            await uow.email.put(
                EmailRecord(
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    kind=kind,
                    key=key,
                    revision=1,
                    created_at=NOW,
                    updated_at=NOW,
                    payload=payload,
                ),
                expected_revision=0,
            )
    erasure = PeopleErasureService(factory, clock)
    preview = await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(session_id=session().id, phase="preview", expected_revision=1),
        key="mail-preview",
        ceiling=Sensitivity.SENSITIVE,
    )
    await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(
            session_id=session().id,
            phase="apply",
            expected_revision=preview.revision,
            operation_id=preview.id,
        ),
        key="mail-apply",
        ceiling=Sensitivity.SENSITIVE,
    )
    async with factory() as uow:
        assessment = await uow.email.get(owner, "assessment", thread_id)
        thread = await uow.email.get(owner, "thread", thread_id)
        source = await uow.email.get(owner, "semantic_source", "source")
    assert assessment is None or "Sam" not in assessment.model_dump_json()
    assert thread is not None
    messages = thread.payload["messages"]
    assert isinstance(messages, list) and messages[0]["body"] == "Sam likes chess"
    assert source is not None and source.payload["excluded"]
    assert source.payload["facts"] == {}


@pytest.mark.parametrize("assignments", [0, 300])
async def test_newer_erasure_receipt_reapplies_to_an_older_snapshot(assignments: int) -> None:
    clock, live = await memory_uow_factory()
    _, restored = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    await restore_erasure_contract(live, restored, clock, owner, assignments=assignments)


async def restore_erasure_contract(
    live: UnitOfWorkFactory,
    restored: UnitOfWorkFactory,
    clock: Clock,
    owner: Principal,
    *,
    assignments: int,
) -> None:
    from uuid import uuid5

    from agent_core.application.people_erasure_batches import batch_id
    from agent_core.domain.errors import NotFoundError, ToolValidationError
    from agent_core.domain.people import PeopleErasure, PersonMention

    targets = []
    for factory in (live, restored):
        service = PublicPeopleService(factory, clock)
        person = await service.create(
            owner,
            CreatePerson(session_id=session().id, display_name="Sam"),
            key="restore-sam",
            ceiling=Sensitivity.SENSITIVE,
        )
        other = await service.create(
            owner,
            CreatePerson(session_id=session().id, display_name="Alex"),
            key="restore-alex",
            ceiling=Sensitivity.SENSITIVE,
        )
        belief = memory(statement="Sam likes chess")
        async with factory() as uow:
            await uow.memories.upsert_belief(belief)
            await uow.people.put(
                PersonMemoryLink(
                    id=uuid5(person.id, "fact"),
                    person_id=person.id,
                    belief_id=belief.id,
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                expected_revision=0,
            )
            for index in range(assignments):
                await uow.people.put(
                    PersonMention(
                        id=uuid5(person.id, f"mention-{index}"),
                        tenant_id=owner.tenant_id,
                        principal_id=owner.principal_id,
                        created_at=NOW,
                        updated_at=NOW,
                        person_id=person.id,
                        source_id=person.support_ids[0],
                        support_ids=person.support_ids,
                        start=0,
                        end=3,
                    ),
                    expected_revision=0,
                )
        targets.append((person, other, belief))
    person, other, belief = targets[0]
    live_service = PeopleErasureService(live, clock)
    preview = await live_service.forget(
        owner,
        person.id,
        PeopleForgetRequest(session_id=session().id, phase="preview", expected_revision=1),
        key="preview",
        ceiling=Sensitivity.SENSITIVE,
    )
    await live_service.forget(
        owner,
        person.id,
        PeopleForgetRequest(
            session_id=session().id,
            phase="apply",
            operation_id=preview.id,
            expected_revision=preview.revision,
        ),
        key="apply",
        ceiling=Sensitivity.SENSITIVE,
    )
    async with live() as uow:
        receipt = await uow.people.get(owner, preview.id, ceiling=Sensitivity.RESTRICTED)
        assert isinstance(receipt, PeopleErasure)
        parts = []
        for index in range(receipt.batch_count):
            part = await uow.people.get(
                owner, batch_id(receipt.id, index), ceiling=Sensitivity.RESTRICTED
            )
            assert isinstance(part, PeopleErasure)
            parts.append(part)
    restore_service = PeopleErasureService(restored, clock)
    with pytest.raises(ToolValidationError, match="applied receipt"):
        await restore_service.reapply(
            owner, receipt.model_copy(update={"state": "preview"}), parts, session_id=session().id
        )
    with pytest.raises(ToolValidationError, match="exact owner"):
        await restore_service.reapply(
            owner.model_copy(update={"principal_id": "other"}),
            receipt,
            parts,
            session_id=session().id,
        )
    if parts:
        with pytest.raises(ToolValidationError, match="incomplete"):
            await restore_service.reapply(owner, receipt, parts[:-1], session_id=session().id)
    result = await restore_service.reapply(owner, receipt, parts, session_id=session().id)
    assert result.state == ("cleanup_pending" if assignments else "completed")
    assert await restore_service.reapply(owner, receipt, parts, session_id=session().id) == result
    result = await restore_service.get(owner, result.id, ceiling=Sensitivity.RESTRICTED)
    assert result.state == "completed"
    async with restored() as uow:
        assert await uow.people.get(owner, person.id, ceiling=Sensitivity.RESTRICTED) is None
        assert await uow.people.get(owner, other.id, ceiling=Sensitivity.RESTRICTED) is not None
        assert await uow.people.source_suppressed(owner, person.support_ids[0])
        with pytest.raises(NotFoundError):
            await uow.memories.get(belief.id, owner)


async def test_disabled_people_maintenance_completes_pending_erasure_without_a_new_run() -> None:
    from datetime import timedelta

    from agent_core.bootstrap import build
    from agent_core.domain.people import PeopleErasure
    from agent_core.runtime.worker import MaintenanceWorker
    from tests.integration.m2_support import memory_settings

    owner = principal()
    async with build(
        settings=memory_settings(), storage="memory", principal=owner, fixed_clock_at=NOW
    ) as app:
        assert app.services.people is None
        pending = PeopleErasure(
            id=uuid4(),
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            created_at=NOW,
            updated_at=NOW,
            target_id=uuid4(),
            expected_revisions={},
            state="cleanup_pending",
            expires_at=NOW + timedelta(minutes=1),
            request_hash="0" * 64,
            pending_artifact_ids=[uuid4()],
        )
        async with app.uow_factory() as uow:
            await uow.people.put(pending, expected_revision=0)
        worker = app.maintenance_factory()
        assert isinstance(worker, MaintenanceWorker)
        await worker.run_once()
        async with app.uow_factory() as uow:
            complete = await uow.people.get(owner, pending.id, ceiling=Sensitivity.RESTRICTED)
            assert isinstance(complete, PeopleErasure) and complete.state == "completed"


async def test_forget_fences_large_graph_then_resumes_physical_cleanup() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    await staged_erasure_contract(factory, clock, owner)


async def staged_erasure_contract(
    factory: UnitOfWorkFactory, clock: Clock, owner: Principal
) -> None:
    from agent_core.domain.errors import NotFoundError
    from agent_core.domain.people import PeopleErasure

    person = await PublicPeopleService(factory, clock).create(
        owner,
        CreatePerson(session_id=session().id, display_name="Sam"),
        key="staged-person",
        ceiling=Sensitivity.SENSITIVE,
    )
    beliefs = [
        memory(belief_id=8000 + i).model_copy(update={"store_position": i + 1}) for i in range(257)
    ]
    async with factory() as uow:
        for belief in beliefs:
            await uow.memories.upsert_belief(belief)
            await uow.people.put(
                PersonMemoryLink(
                    id=uuid4(),
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    created_at=NOW,
                    updated_at=NOW,
                    person_id=person.id,
                    belief_id=belief.id,
                ),
                expected_revision=0,
            )
    service = PeopleErasureService(factory, clock)
    preview = await service.forget(
        owner,
        person.id,
        PeopleForgetRequest(
            session_id=session().id,
            phase="preview",
            expected_revision=1,
        ),
        key="staged-preview",
        ceiling=Sensitivity.SENSITIVE,
    )
    applied = await service.forget(
        owner,
        person.id,
        PeopleForgetRequest(
            session_id=session().id,
            phase="apply",
            expected_revision=preview.revision,
            operation_id=preview.id,
        ),
        key="staged-apply",
        ceiling=Sensitivity.SENSITIVE,
    )
    assert applied.state == "cleanup_pending"
    async with factory() as uow:
        assert await uow.people.get(owner, person.id, ceiling=Sensitivity.RESTRICTED) is None
        for belief in beliefs:
            with pytest.raises(NotFoundError):
                await uow.memories.get_at(belief.id, owner, known_at=NOW)
        receipt = await uow.people.get(owner, applied.id, ceiling=Sensitivity.RESTRICTED)
        assert isinstance(receipt, PeopleErasure)
        assert receipt.pending_people
    # A fresh service uses persisted cursors rather than process-local work.
    restarted = PeopleErasureService(factory, clock)
    for _ in range(10):
        result = await restarted.get(owner, applied.id, ceiling=Sensitivity.SENSITIVE)
        if result.state == "completed":
            break
    assert result.state == "completed"
    async with factory() as uow:
        tombstones = await uow.memories.outstanding_rejections(owner.tenant_id, owner.principal_id)
        assert {row.belief_id for row in tombstones} == {row.id for row in beliefs}
        assert not await uow.people.purge_erased(owner)


async def test_forget_follows_generated_artifacts_into_later_run_context() -> None:
    clock, factory = await memory_uow_factory()
    await derived_knowledge_erasure_contract(factory, clock, principal())


async def derived_knowledge_erasure_contract(
    factory: UnitOfWorkFactory, clock: Clock, owner: Principal
) -> None:
    from agent_core.domain.errors import NotFoundError
    from agent_core.domain.events import NewEvent
    from agent_core.domain.memory import TracedPassage
    from agent_core.domain.runs import RunStatus
    from tests.contract.memory_fixtures import prepared_knowledge, trace
    from tests.contract.support import run

    person_id = uuid4()
    original = run(status=RunStatus.COMPLETED)
    following = original.model_copy(update={"id": uuid4()})
    prepared = prepared_knowledge()
    followed = trace().model_copy(
        update={
            "id": uuid4(),
            "run_id": following.id,
            "passages": [
                TracedPassage(
                    chunk_id=prepared.chunks[0].chunk_id,
                    document_id=prepared.document.document_id,
                    title="Generated guide",
                    heading_path=[],
                    text="Sam likes chess",
                    sensitivity=Sensitivity.INTERNAL,
                )
            ],
        }
    )
    async with factory() as uow:
        await uow.runs.create(original)
        await uow.runs.create(following)
        await uow.events.append(
            NewEvent(
                session_id=session().id,
                run_id=original.id,
                event_type="people.context.used",
                actor_type="memory",
                payload={"person_ids": [str(person_id)]},
            )
        )
        await uow.artifacts.create(prepared.document.source_ref)
        await uow.knowledge.ingest(prepared)
        await uow.traces.record(followed)
        await uow.events.append(
            NewEvent(
                session_id=session().id,
                run_id=following.id,
                event_type="assistant.message.created",
                actor_type="agent",
                payload={"content": "Sam likes chess"},
            )
        )
    async with factory() as uow:
        await uow.session_deletions.erase_people_copies(owner, [person_id], clock.now())
    async with factory() as uow:
        visible = await uow.events.list_after(session().id, 0, owner)
        assert all("Sam likes chess" not in event.model_dump_json() for event in visible)
        with pytest.raises(NotFoundError):
            await uow.traces.get(followed.id, owner)


async def test_forget_invalidates_frozen_memory_snapshots_and_cached_plans() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    await frozen_snapshot_erasure_contract(factory, clock, owner)


@pytest.mark.parametrize("operation", ["people-remove", "memory-delete"])
async def test_remove_one_fact_erases_its_copied_influence(operation: str) -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    await frozen_snapshot_erasure_contract(factory, clock, owner, operation=operation)


async def frozen_snapshot_erasure_contract(
    factory: UnitOfWorkFactory, clock: Clock, owner: Principal, *, operation: str = "forget"
) -> None:
    from pathlib import Path

    import yaml

    from agent_core.context.estimator import ConservativeTokenEstimator
    from agent_core.context.planner import EventContextPlanner
    from agent_core.domain.messages import ResolvedModel
    from agent_core.domain.runs import RunStatus
    from agent_core.memory.retrieval import HybridMemoryRetriever
    from agent_core.tools.registry import StaticToolRegistry
    from tests.contract.support import agent, ids, run

    person = await PublicPeopleService(factory, clock).create(
        owner,
        CreatePerson(session_id=session().id, display_name="Sam"),
        key="snapshot-person",
        ceiling=Sensitivity.SENSITIVE,
    )
    belief = memory(statement="Sam prefers morning meetings")
    async with factory() as uow:
        await uow.runs.create(run(status=RunStatus.RUNNING))
        await uow.memories.upsert_belief(belief)
        from agent_core.domain.memory import BeliefRejection, RejectionKind

        await uow.memories.reject(
            BeliefRejection(
                id=uuid4(),
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                belief_id=belief.id,
                kind=RejectionKind.NOT_HERE,
                subject="Sam prefers morning meetings",
                statement=belief.statement,
                statement_sha256="0" * 64,
                belief_type=belief.belief_type,
                scope=belief.scope,
                created_at=clock.now(),
            ),
            belief,
        )
        await uow.people.put(
            PersonMemoryLink(
                id=uuid4(),
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                created_at=NOW,
                updated_at=NOW,
                person_id=person.id,
                belief_id=belief.id,
            ),
            expected_revision=0,
        )
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text()
    )
    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        owner,
        config,
        policy_version="test",
        memory_retriever=HybridMemoryRetriever(factory, clock, ids(), owner),
    )
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)
    original = await planner.plan(session(), agent(), owner, model)
    assert "Sam prefers morning meetings" in original.memory_snapshot
    if operation == "forget":
        service = PeopleErasureService(factory, clock)
        preview = await service.forget(
            owner,
            person.id,
            PeopleForgetRequest(
                session_id=session().id,
                phase="preview",
                expected_revision=1,
            ),
            key="snapshot-preview",
            ceiling=Sensitivity.SENSITIVE,
        )
        await service.forget(
            owner,
            person.id,
            PeopleForgetRequest(
                session_id=session().id,
                phase="apply",
                expected_revision=preview.revision,
                operation_id=preview.id,
            ),
            key="snapshot-apply",
            ceiling=Sensitivity.SENSITIVE,
        )
    else:
        from agent_core.domain.people_views import PeopleCorrectionRequest
        from agent_core.memory.formation import GovernedMemoryService

        memories = GovernedMemoryService(factory, clock, ids(), owner)
        if operation == "people-remove":
            people = PublicPeopleService(factory, clock, memory_for=lambda _: memories)
            removed = await people.correct(
                owner,
                person.id,
                PeopleCorrectionRequest(
                    session_id=session().id,
                    belief_id=belief.id,
                    expected_revision=person.revision,
                    expected_position=belief.store_position,
                    operation="remove",
                ),
                key="remove-fact",
                ceiling=Sensitivity.SENSITIVE,
            )
            assert removed.removed
            assert removed.model_dump()["erasure"]["state"] == "cleanup_pending"
        else:
            await memories.delete(belief.id)
        async with factory() as uow:
            assert await uow.people.get(owner, person.id, ceiling=Sensitivity.SENSITIVE)
            from agent_core.domain.errors import NotFoundError

            with pytest.raises(NotFoundError):
                await uow.memories.get(belief.id, owner)
    async with factory() as uow:
        events = await uow.events.list_after(session().id, 0, owner)
        assert all(
            "Sam prefers morning meetings" not in event.model_dump_json() for event in events
        )
        rejections = await uow.memories.outstanding_rejections(owner.tenant_id, owner.principal_id)
        assert all(
            "Sam prefers morning meetings" not in row.model_dump_json() for row in rejections
        )
        assert (await uow.runs.get(run().id, owner)).cancel_requested_at is not None
    cached = await planner.current(session().id)
    assert cached is not None and "Sam prefers morning meetings" not in cached.memory_snapshot
    rebuilt = await planner.plan(session(), agent(), owner, model)
    assert rebuilt.epoch > original.epoch
    assert "Sam prefers morning meetings" not in rebuilt.memory_snapshot
    from agent_core.domain.errors import RunCancelledError

    with pytest.raises(RunCancelledError, match="snapshot"):
        await planner._append(
            original.model_copy(update={"epoch": rebuilt.epoch + 1}),
            "context.epoch.rotated",
            "stale-snapshot-after-erasure",
        )


@pytest.mark.parametrize("source_kind", ["email", "session"])
async def test_source_erasure_removes_cross_session_people_copies(source_kind: str) -> None:
    clock, factory = await memory_uow_factory()
    await source_copy_erasure_contract(factory, clock, principal(), source_kind)


async def source_copy_erasure_contract(
    factory: UnitOfWorkFactory, clock: Clock, owner: Principal, source_kind: str
) -> None:
    from agent_core.domain.events import NewEvent
    from agent_core.domain.memory import TracedPersonContext
    from agent_core.domain.people import PeopleErasure, PeopleQuery, PeopleSource, Person
    from agent_core.domain.runs import RunStatus
    from tests.contract.memory_fixtures import trace
    from tests.contract.support import run

    other = session().model_copy(update={"id": uuid4()})
    dependent = run(status=RunStatus.COMPLETED).model_copy(update={"session_id": other.id})
    fields: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": clock.now(),
        "updated_at": clock.now(),
    }
    source = PeopleSource(
        id=uuid4(),
        session_id=session().id,
        event_sequence=1,
        source_kind="email" if source_kind == "email" else "owner",
        account_id="work" if source_kind == "email" else None,
        thread_id="source-thread" if source_kind == "email" else None,
        message_id="source-message" if source_kind == "email" else None,
        source_revision="test@1",
        evidence_at=clock.now(),
        **fields,
    )
    person = Person(id=uuid4(), display_name="Sam", support_ids=[source.id], **fields)
    owner_belief = memory(statement="Owner-confirmed preference")
    remembered = trace().model_copy(
        update={
            "session_id": other.id,
            "run_id": dependent.id,
            "people": [
                TracedPersonContext(
                    record_id=person.id,
                    revision=1,
                    person_ids=[person.id],
                    kind="person",
                    text="Sam likes chess",
                    sensitivity=Sensitivity.SENSITIVE,
                    source_ids=[source.id],
                )
            ],
        }
    )
    async with factory() as uow:
        await uow.sessions.create(other)
        await uow.runs.create(dependent)
        await uow.people.put(source, expected_revision=0)
        await uow.people.put(person, expected_revision=0)
        await uow.memories.upsert_belief(owner_belief)
        await uow.people.put(
            PersonMemoryLink(
                id=uuid4(),
                person_id=person.id,
                belief_id=owner_belief.id,
                support_ids=[source.id],
                **fields,
            ),
            expected_revision=0,
        )
        await uow.traces.record(remembered)
        await uow.events.append(
            NewEvent(
                session_id=other.id,
                run_id=None,
                actor_type="runtime",
                event_type="context.plan.created",
                payload={
                    "plan": {
                        "memory_snapshot": "Sam likes chess",
                        "snapshot_id": str(remembered.id),
                    }
                },
            )
        )
        for _ in range(600):
            await uow.events.append(
                NewEvent(
                    session_id=other.id,
                    run_id=dependent.id,
                    actor_type="runtime",
                    event_type="assistant.message.completed",
                    payload={"content": "Sam likes chess"},
                )
            )
    async with factory() as uow:
        if source_kind == "email":
            source_result = await uow.session_deletions.erase_email_source(
                owner, "work", "source-thread", frozenset({"source-message"}), clock.now()
            )
        else:
            await uow.session_deletions.delete(session().id, owner, clock.now())
        visible = await uow.events.list_after(other.id, 0, owner, limit=1000)
        assert len(visible) == 601
        assert all("Sam likes chess" not in event.model_dump_json() for event in visible)
        receipt_rows = await uow.people.query(
            PeopleQuery(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                kinds=["erasure"],
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                limit=100,
            )
        )
        receipts = [row for row in receipt_rows if isinstance(row, PeopleErasure)]
        assert len(receipts) == len(receipt_rows)
        assert receipts, "source cleanup needs durable continuation and restore evidence"
        if source_kind == "email":
            assert (
                await uow.memories.get(owner_belief.id, owner)
            ).statement == owner_belief.statement
            assert all(owner_belief.id not in receipt.blocked_belief_ids for receipt in receipts)
            expected_pending = sum(receipt.state == "cleanup_pending" for receipt in receipts)
            assert source_result.get("pending_people_cleanup", 0) == expected_pending
    if source_kind == "email":
        async with factory() as uow:
            repeated = await uow.session_deletions.erase_email_source(
                owner,
                "work",
                "source-thread",
                frozenset({"source-message", "later-message"}),
                clock.now(),
            )
            assert repeated.get("pending_people_cleanup", 0) == expected_pending
    service = PeopleErasureService(factory, clock)
    for receipt in receipts:
        result = await service.get(
            owner.model_copy(update={"scopes": {"people.read"}}),
            receipt.id,
            ceiling=Sensitivity.RESTRICTED,
        )
        assert result.state == "completed"
        exported, parts = await service.export(
            owner.model_copy(update={"scopes": {"people.read"}}), receipt.id
        )
        assert exported.state == "completed" and not parts
    async with factory() as uow:
        late = await uow.events.append(
            NewEvent(
                session_id=other.id,
                run_id=dependent.id,
                actor_type="runtime",
                event_type="assistant.message.completed",
                payload={"content": "Sam likes chess"},
            )
        )
        assert "Sam likes chess" not in late.model_dump_json()


def test_source_cleanup_receipt_pages_are_independently_exportable() -> None:
    from agent_core.adapters.persistence.people_erasure import source_cleanup_receipts
    from agent_core.application.people_erasure_restore import replay_manifest
    from agent_core.domain.people import PeopleCopyCleanup

    source = uuid4()
    copies = [source, *(uuid4() for _ in range(600))]
    runs = [uuid4() for _ in range(1000)]
    receipts = source_cleanup_receipts(
        principal(),
        [source],
        copies,
        PeopleCopyCleanup(pending_run_ids=runs, artifact_ids=[], counts={}),
        NOW,
    )
    assert len(receipts) == 4
    for receipt in receipts:
        assert len(receipt.blocked_record_ids) <= 256
        assert len(receipt.pending_run_ids) <= 256
        replay_manifest(principal(), receipt, [])
    assert set(runs) == {key for receipt in receipts for key in receipt.pending_run_ids}
    assert set(copies) <= {key for receipt in receipts for key in receipt.blocked_record_ids}


async def test_recall_waits_for_initial_erasure_fence_without_blocking_another_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, factory = await memory_uow_factory()
    await initial_erasure_contention_contract(factory, clock, monkeypatch)


async def initial_erasure_contention_contract(
    factory: UnitOfWorkFactory,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from agent_core.application.people_context import PeopleContextService
    from agent_core.domain.people import PeopleRecord
    from agent_core.memory.retrieval import HybridMemoryRetriever
    from agent_core.ports.persistence import RepositoryUnitOfWork
    from tests.contract.memory_fixtures import recall_query
    from tests.contract.support import ids

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    people = PublicPeopleService(factory, clock)
    person = await people.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Sam"),
        key="contention-person",
        ceiling=Sensitivity.SENSITIVE,
    )
    belief = memory(statement="Sam prefers morning meetings")
    independent = memory(
        belief_id=99007, statement="Morning meetings should include an agenda"
    ).model_copy(update={"store_position": 2})
    async with factory() as uow:
        await uow.memories.upsert_belief(belief)
        await uow.memories.upsert_belief(independent)
        await uow.people.put(
            PersonMemoryLink(
                id=uuid4(),
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                created_at=clock.now(),
                updated_at=clock.now(),
                person_id=person.id,
                belief_id=belief.id,
                support_ids=person.support_ids,
            ),
            expected_revision=0,
        )
    context = PeopleContextService(factory, HybridMemoryRetriever(factory, clock, ids(), owner))
    query = recall_query(text="Sam morning meetings", min_score=0.0)
    before = await context.automatic_recall(owner, query, session_id=session().id)
    assert any(item.record_id == person.id for item in before.people)
    assert any(item.belief_id == belief.id for item in before.items)
    erasure = PeopleErasureService(factory, clock)
    preview = await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(session_id=session().id, phase="preview", expected_revision=1),
        key="contention-preview",
        ceiling=Sensitivity.SENSITIVE,
    )
    discovered, release, attempting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    affected = erasure._affected

    async def pause_discovery(
        uow: RepositoryUnitOfWork,
        principal: Principal,
        person_id: UUID,
        ceiling: Sensitivity,
    ) -> list[PeopleRecord]:
        rows = await affected(uow, principal, person_id, ceiling)
        discovered.set()
        await release.wait()
        return rows

    monkeypatch.setattr(erasure, "_affected", pause_discovery)
    deleting = asyncio.create_task(
        erasure.forget(
            owner,
            person.id,
            PeopleForgetRequest(
                session_id=session().id,
                phase="apply",
                operation_id=preview.id,
                expected_revision=preview.revision,
            ),
            key="contention-apply",
            ceiling=Sensitivity.SENSITIVE,
        )
    )
    recalling = None
    after = None
    try:
        await asyncio.wait_for(discovered.wait(), timeout=5)

        async def recall() -> RecallResult:
            attempting.set()
            return await context.automatic_recall(owner, query, session_id=session().id)

        recalling = asyncio.create_task(recall())
        await asyncio.wait_for(attempting.wait(), timeout=5)
        done, _ = await asyncio.wait([recalling], timeout=0.05)
        assert not done, "recall must wait for the initial owner erasure transaction"
        other = owner.model_copy(update={"principal_id": "unaffected-owner"})
        unrelated = await asyncio.wait_for(
            people.list(other, ceiling=Sensitivity.SENSITIVE),
            timeout=5,
        )
        assert unrelated.items == []
    finally:
        release.set()
        await asyncio.wait_for(deleting, timeout=10)
        if recalling is not None:
            after = await asyncio.wait_for(recalling, timeout=10)
    assert after is not None and after.people == []
    assert [item.belief_id for item in after.items] == [independent.id]
