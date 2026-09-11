"""M26 semantic source admission; synthetic fixtures are not activation evidence."""

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.domain.email import EmailRecord
from agent_core.domain.errors import ConflictError, ToolTrustRejectedError, ToolValidationError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryDerivation,
    MemoryLongevity,
    Portability,
    Sensitivity,
)
from agent_core.domain.messages import TextPart, ToolResultItem
from agent_core.domain.policies import TrustLevel
from agent_core.memory.email_semantics import (
    EmailSemanticEvaluationEvidence,
    EmailSemanticFact,
    EmailSemanticFormationService,
    EmailSemanticSource,
    semantic_implementation_sha256,
)
from agent_core.memory.retrieval import HybridMemoryRetriever
from tests.contract.memory_fixtures import formation_stack
from tests.contract.support import NOW, principal, session


async def semantic_stack(
    *, enabled: bool = True, age: int = 0, **message_updates: object
) -> tuple[
    MemoryUnitOfWorkFactory,
    EmailSemanticFormationService,
    EmailSemanticSource,
    EmailSemanticFact,
    HybridMemoryRetriever,
]:
    clock, factory, _baseline, retriever = await formation_stack()
    sid = UUID(int=410)
    sent_at = (NOW - timedelta(days=age)).replace(microsecond=0)
    body = "The Atlas board vote moved to Friday. The diligence package is due Monday."
    message = {
        "id": "m1",
        "thread_id": "t1",
        "from": "Alex <alex@example.test>",
        "body": body,
        "body_complete": True,
        "headers_complete": True,
        "internal_date": str(int(sent_at.timestamp() * 1000)),
        **message_updates,
    }
    async with factory() as uow:
        await uow.sessions.create(
            session().model_copy(
                update={
                    "id": sid,
                    "metadata": {
                        "email_operational": True,
                        "email_account_servers": {"work": {"read": "gmail_work_read"}},
                    },
                }
            )
        )
        event = await uow.events.append(
            NewEvent(
                session_id=sid,
                run_id=None,
                event_type="tool.call.completed",
                actor_type="runtime",
                payload={
                    "name": "mcp.gmail_work_read.get_thread_page",
                    "result_item": ToolResultItem(
                        call_id="c1",
                        trust=TrustLevel.EXTERNAL_UNTRUSTED,
                        content=[
                            TextPart(text=json.dumps({"thread_id": "t1", "messages": [message]}))
                        ],
                    ).model_dump(mode="json"),
                },
            )
        )
    evidence = (
        EmailSemanticEvaluationEvidence(
            provider="test",
            model="bounded-test",
            build_ref="fixture-not-production",
            corpus_sha256="a" * 64,
            implementation_sha256=semantic_implementation_sha256(),
            sample_count=20,
            supported_count=20,
            labeled_useful_count=20,
            comparison_case_count=1,
            improvement_count=1,
            existing_memory_benchmarks_passed=True,
            attribution_case_count=1,
            cross_project_case_count=1,
            historical_case_count=1,
            evaluated_at=NOW,
        )
        if enabled
        else None
    )
    service = EmailSemanticFormationService(
        factory,
        clock,
        SequenceIdFactory(UUID(int=n) for n in range(5000, 6000)),
        principal(),
        provider="test",
        model="bounded-test",
        evidence=evidence,
    )
    source = EmailSemanticSource(
        account_id="work",
        provider_thread_id="t1",
        message_id="m1",
        session_id=sid,
        source_event_sequence=event.sequence,
        tool_name="mcp.gmail_work_read.get_thread_page",
        sender="Alex <alex@example.test>",
        body=body,
        sent_at=sent_at,
    )
    fact = EmailSemanticFact(
        message_id="m1",
        quote="The Atlas board vote moved to Friday.",
        belief_type=BeliefType.FACT,
        subject="Atlas board vote",
        predicate="scheduled date",
        value="Friday",
        confidence=0.9,
    )
    return factory, service, source, fact, retriever


async def test_semantic_formation_preserves_attribution_historical_dates_and_shared_scope() -> None:
    _factory, service, source, fact, _ = await semantic_stack(age=400)
    rows = await service.form(source, [fact])
    assert len(rows) == 1
    row = rows[0]
    assert row.authority is MemoryAuthority.INFERRED
    assert row.derivation is MemoryDerivation.HYPOTHESIS
    assert row.longevity is MemoryLongevity.TENTATIVE
    assert row.portability is Portability.CONTEXTUAL
    assert row.sensitivity is Sensitivity.SENSITIVE
    assert row.confidence <= 0.4
    assert row.valid_from == source.sent_at
    assert row.last_evidence_at == source.sent_at
    assert row.expires_at == source.sent_at + timedelta(days=30)
    assert row.source_session_id == source.session_id
    assert row.source_event_ids == [source.source_event_sequence]
    assert "Alex" in row.statement and "Friday" in row.statement
    assert row.consolidation_policy_version == "email-semantic@1"
    assert (await service.form(source, [fact]))[0] == row


async def test_semantic_formation_is_disabled_without_versioned_evidence() -> None:
    _, service, source, fact, _ = await semantic_stack(enabled=False)
    assert await service.form(source, [fact]) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "personal"),
        ("message_id", "other"),
        ("body", "Fabricated body"),
        ("sender", "Owner"),
        ("sent_at", NOW),
        ("source_event_sequence", 99),
        ("tool_name", "mcp.attacker_read.get_thread_page"),
    ],
)
async def test_semantic_source_must_exactly_match_the_original_owned_tool_event(
    field: str, value: object
) -> None:
    _, service, source, fact, _ = await semantic_stack()
    with pytest.raises(ToolTrustRejectedError):
        await service.form(source.model_copy(update={field: value}), [fact])


@pytest.mark.parametrize(
    "updates", [{"quote": "Invented quote"}, {"value": "Tuesday"}, {"message_id": "m2"}]
)
async def test_semantic_proposal_must_be_grounded_in_the_selected_message(
    updates: dict[str, object],
) -> None:
    _, service, source, fact, _ = await semantic_stack()
    with pytest.raises(ToolValidationError):
        await service.form(source, [fact.model_copy(update=updates)])


async def test_incomplete_mail_is_not_a_semantic_source() -> None:
    _, service, source, fact, _ = await semantic_stack(body_complete=False)
    with pytest.raises(ToolTrustRejectedError):
        await service.form(source, [fact])


async def test_source_exclusion_prevents_reimport_and_preserves_owner_correction() -> None:
    from agent_core.domain.errors import ConflictError
    from agent_core.domain.memory import MemoryEdit

    factory, service, source, fact, _ = await semantic_stack()
    memory = (await service.form(source, [fact]))[0]
    corrected = await service._governed.edit(
        memory.id, MemoryEdit(statement="I confirmed the Atlas board vote is Tuesday.")
    )
    assert await service.exclude_source("work", "t1", "m1") == 0
    async with factory() as uow:
        assert await uow.memories.get(memory.id, principal()) == corrected
    with pytest.raises(ConflictError):
        await service.form(source, [fact])


async def test_source_exclusion_deletes_inferred_memory_and_replay_is_idempotent() -> None:
    from agent_core.domain.errors import NotFoundError

    factory, service, source, fact, _ = await semantic_stack()
    memory = (await service.form(source, [fact]))[0]
    assert await service.exclude_source("work", "t1", "m1") == 1
    assert await service.exclude_source("work", "t1", "m1") == 0
    async with factory() as uow:
        with pytest.raises(NotFoundError):
            await uow.memories.get(memory.id, principal())


async def test_historical_fact_is_absent_from_current_recall_but_visible_as_of_source_date() -> (
    None
):
    from agent_core.domain.memory import RecallQuery

    factory, service, source, fact, _ = await semantic_stack(age=400)
    memory = (await service.form(source, [fact]))[0]
    query = RecallQuery(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        current_scope="Atlas-project",
        subjects=[fact.subject],
        budget_tokens=2048,
        max_items=10,
        min_score=0,
    )
    async with factory() as uow:
        assert memory not in await uow.memories.query(query)
        assert memory in await uow.memories.query(
            query.model_copy(update={"as_of": source.sent_at + timedelta(days=1)})
        )


async def test_source_content_erasure_preserves_owner_messages_and_event_identity() -> None:
    factory, _service, source, _fact, _ = await semantic_stack()
    async with factory() as uow:
        owner_event = await uow.events.append(
            NewEvent(
                session_id=source.session_id,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=principal().principal_id,
                payload={"content": "Keep this independently authored owner note."},
            )
        )
        before = (await uow.events.list_after(source.session_id, 0, principal()))[0]
        erase = getattr(uow.session_deletions, "erase_email_source", None)
        assert callable(erase), "source content must be erased without deleting its mixed session"
        counts = await erase(principal(), "work", "t1", frozenset({"m1"}), NOW)
        assert counts["events"] == 1
        after = (await uow.events.list_after(source.session_id, 0, principal()))[0]
        assert after.id == before.id and after.sequence == before.sequence
        assert after.created_at == before.created_at and after.actor_type == before.actor_type
        assert "The Atlas board vote" not in json.dumps(after.payload)
        assert "source_erased" in json.dumps(after.payload)
        assert owner_event in await uow.events.list_after(source.session_id, 0, principal())


async def test_rereading_the_same_message_in_a_new_event_never_reinforces_it() -> None:
    factory, service, source, fact, _ = await semantic_stack()
    memory = (await service.form(source, [fact]))[0]
    async with factory() as uow:
        original = (await uow.events.list_after(source.session_id, 0, principal()))[0]
        replay_event = await uow.events.append(
            NewEvent(
                session_id=source.session_id,
                run_id=None,
                event_type=original.event_type,
                actor_type=original.actor_type,
                payload=original.payload,
            )
        )
    replay = await service.form(
        source.model_copy(update={"source_event_sequence": replay_event.sequence}), [fact]
    )
    assert replay == [memory]


async def test_erasure_removes_derived_memory_event_copies() -> None:
    factory, service, source, fact, _ = await semantic_stack()
    await service.form(source, [fact])
    await service.exclude_source("work", "t1", "m1")
    async with factory() as uow:
        await uow.session_deletions.erase_email_source(
            principal(), "work", "t1", frozenset({"m1"}), NOW
        )
        events = await uow.events.list_after(source.session_id, 0, principal())
        assert all("Friday" not in json.dumps(event.payload) for event in events)


def test_semantic_evidence_has_an_operator_path_setting(tmp_path: Path) -> None:
    from agent_core.config import load_settings

    path = tmp_path / "email-evidence.json"
    settings = load_settings(
        {
            "DATABASE_URL": "postgresql+asyncpg://127.0.0.1:1/unused",
            "DEPLOYMENT_MODE": "development",
            "AUTH_MODE": "dev",
            "AGENT_SANDBOX": "fake",
            "AGENT_EMAIL_SEMANTIC_EVIDENCE": str(path),
        }
    )
    assert getattr(settings, "email_semantic_evidence", None) == path


def test_erasure_never_interprets_another_messages_body_as_source_identity() -> None:
    from agent_core.adapters.persistence.email_erasure import erase_result, source_present

    message = {
        "id": "m2",
        "thread_id": "t1",
        "body": json.dumps({"id": "m1", "thread_id": "t1", "body": "unrelated quoted JSON"}),
    }
    assert erase_result(message, "t1", frozenset({"m1"})) == message
    assert not source_present(message, "t1", frozenset({"m1"}))


async def test_two_facts_about_one_subject_remain_distinct() -> None:
    _, service, source, fact, _ = await semantic_stack()
    second = fact.model_copy(
        update={
            "predicate": "diligence deadline",
            "quote": "The diligence package is due Monday.",
            "value": "Monday",
        }
    )
    rows = await service.form(source, [fact, second])
    assert len({row.id for row in rows}) == 2
    assert any("Friday" in row.statement for row in rows)
    assert any("Monday" in row.statement for row in rows)


async def test_late_historical_import_does_not_supersede_current_evidence() -> None:
    factory, service, source, fact, _ = await semantic_stack()
    current = (await service.form(source, [fact]))[0]
    old_at = source.sent_at - timedelta(days=400)
    old_body = "The Atlas board vote moved to Thursday."
    old_result = ToolResultItem(
        call_id="old",
        content=[
            TextPart(
                text=json.dumps(
                    {
                        "thread_id": "t1",
                        "messages": [
                            {
                                "id": "m0",
                                "from": source.sender,
                                "body": old_body,
                                "body_complete": True,
                                "headers_complete": True,
                                "internal_date": str(int(old_at.timestamp() * 1000)),
                            }
                        ],
                    }
                )
            )
        ],
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )
    async with factory() as uow:
        event = await uow.events.append(
            NewEvent(
                session_id=source.session_id,
                run_id=None,
                event_type="tool.call.completed",
                actor_type="runtime",
                payload={
                    "name": source.tool_name,
                    "result_item": old_result.model_dump(mode="json"),
                },
            )
        )
    old_source = source.model_copy(
        update={
            "message_id": "m0",
            "body": old_body,
            "sent_at": old_at,
            "source_event_sequence": event.sequence,
        }
    )
    await service.form(
        old_source,
        [fact.model_copy(update={"message_id": "m0", "quote": old_body, "value": "Thursday"})],
    )
    async with factory() as uow:
        fresh = await uow.memories.get(current.id, principal())
        assert fresh.valid_to is None
        assert fresh.status == current.status


async def test_complete_bounded_passage_of_long_message_can_form_grounded_facts() -> None:
    _, service, source, fact, _ = await semantic_stack(
        body_complete=False,
        body_available=True,
        next_body_offset=len(
            b"The Atlas board vote moved to Friday. The diligence package is due Monday."
        ),
        history_id="42",
    )
    assert len(await service.form(source, [fact])) == 1


async def test_body_continuation_uses_its_exact_event_and_original_header_date() -> None:
    factory, service, source, fact, _ = await semantic_stack(history_id="42")
    body = "The Atlas board vote moved to Thursday."
    offset = len(source.body.encode())
    result = ToolResultItem(
        call_id="body",
        content=[
            TextPart(
                text=json.dumps(
                    {
                        "message_id": "m1",
                        "history_id": "42",
                        "offset": offset,
                        "body": body,
                        "body_available": True,
                        "source_changed": False,
                        "complete": True,
                        "next_offset": None,
                    }
                )
            )
        ],
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
    )
    async with factory() as uow:
        event = await uow.events.append(
            NewEvent(
                session_id=source.session_id,
                run_id=None,
                event_type="tool.call.completed",
                actor_type="runtime",
                payload={
                    "name": "mcp.gmail_work_read.get_message_body",
                    "result_item": result.model_dump(mode="json"),
                },
            )
        )
    passage = source.model_copy(
        update={
            "source_event_sequence": event.sequence,
            "header_event_sequence": source.source_event_sequence,
            "tool_name": "mcp.gmail_work_read.get_message_body",
            "body_offset": offset,
            "body": body,
        }
    )
    formed = await service.form(
        passage, [fact.model_copy(update={"quote": body, "value": "Thursday"})]
    )
    assert len(formed) == 1
    assert formed[0].valid_from == source.sent_at
    assert formed[0].source_event_ids == [event.sequence]
    with pytest.raises(ToolTrustRejectedError):
        await service.form(passage.model_copy(update={"body_offset": offset + 1}), [fact])


def test_erasure_matches_account_bound_message_body_continuations_without_thread_field() -> None:
    from agent_core.adapters.persistence.email_erasure import erase_result, source_present

    body = {
        "message_id": "m1",
        "history_id": "42",
        "offset": 65536,
        "body": "A private continuation",
        "complete": True,
    }
    assert source_present(body, "t1", frozenset({"m1"}))
    assert "private continuation" not in json.dumps(erase_result(body, "t1", frozenset({"m1"})))


@pytest.mark.parametrize("initially_active", [False, True])
async def test_source_erasure_removes_shared_trace_and_terminal_checkpoint_copies(
    initially_active: bool,
) -> None:
    from agent_core.domain.runs import RunCheckpoint, RunStatus
    from tests.contract.memory_fixtures import recalled, trace
    from tests.contract.support import run

    factory, service, source, fact, _ = await semantic_stack()
    memory = (await service.form(source, [fact]))[0]
    other_sid, other_rid = UUID(int=780), UUID(int=781)
    selected = recalled(
        belief_id=memory.id.int, statement=memory.statement, authority=MemoryAuthority.INFERRED
    )
    independent = recalled(belief_id=790, statement="The owner prefers short answers.")
    snapshot = trace().model_copy(
        update={
            "session_id": other_sid,
            "run_id": other_rid,
            "beliefs": [selected, independent],
            "returned": [memory.id, independent.belief_id],
            "rendered": memory.statement,
        }
    )
    async with factory() as uow:
        await uow.sessions.create(session().model_copy(update={"id": other_sid}))
        await uow.runs.create(
            run(status=RunStatus.RUNNING if initially_active else RunStatus.COMPLETED).model_copy(
                update={"id": other_rid, "session_id": other_sid}
            )
        )
        await uow.traces.record(snapshot)
        await uow.checkpoints.write(
            other_rid,
            RunCheckpoint(
                run_id=other_rid,
                version=1,
                status=RunStatus.COMPLETED,
                working_state={"recalled": str(memory.id), "content": memory.statement},
                created_at=NOW,
            ),
            full=True,
        )
    await service.exclude_source("work", "t1", "m1")
    if initially_active:
        with pytest.raises(ConflictError, match="settle"):
            async with factory() as uow:
                await uow.session_deletions.erase_email_source(
                    principal(), "work", "t1", frozenset({"m1"}), NOW
                )
        async with factory() as uow:
            assert (await uow.traces.get(snapshot.id, principal())).beliefs == snapshot.beliefs
            assert await uow.checkpoints.latest(other_rid) is not None
            await uow.runs.transition(other_rid, RunStatus.RUNNING, RunStatus.COMPLETED)
    async with factory() as uow:
        await uow.session_deletions.erase_email_source(
            principal(), "work", "t1", frozenset({"m1"}), NOW
        )
        erased = await uow.traces.get(snapshot.id, principal())
        assert "Friday" not in erased.model_dump_json()
        assert erased.beliefs == [independent]
        assert await uow.checkpoints.latest(other_rid) is None
        assert await uow.sessions.get(other_sid, principal())


async def test_semantic_source_write_has_a_same_transaction_run_audit() -> None:
    from tests.contract.support import run

    factory, service, source, _, _ = await semantic_stack()
    context_run = run().model_copy(update={"id": UUID(int=850), "session_id": source.session_id})
    async with factory() as uow:
        await uow.runs.create(context_run)
    await service.register_source(source, run=context_run, lease=None)
    async with factory() as uow:
        events = await uow.events.list_after(source.session_id, 0, principal())
        guarded = [event for event in events if event.event_type == "email.semantic.updated"]
        assert len(guarded) == 1
        assert guarded[0].run_id == context_run.id
        assert guarded[0].actor_type == "runtime"
        assert source.body not in json.dumps(guarded[0].payload)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sample_count": 40}, "precision"),
        ({"labeled_useful_count": 24}, "recall"),
        ({"existing_memory_benchmarks_passed": False}, "existing_memory_benchmarks_passed"),
        ({"comparison_case_count": 0}, "comparison_case_count"),
    ],
)
def test_semantic_activation_rejects_below_threshold_precision(
    changes: dict[str, object],
    message: str,
) -> None:
    from pydantic import ValidationError

    valid = EmailSemanticEvaluationEvidence(
        provider="test",
        model="bounded-test",
        build_ref="fixture-not-production",
        corpus_sha256="a" * 64,
        implementation_sha256=semantic_implementation_sha256(),
        sample_count=20,
        supported_count=20,
        labeled_useful_count=20,
        comparison_case_count=1,
        improvement_count=1,
        existing_memory_benchmarks_passed=True,
        attribution_case_count=1,
        cross_project_case_count=1,
        historical_case_count=1,
        evaluated_at=NOW,
    )
    with pytest.raises(ValidationError, match=message):
        EmailSemanticEvaluationEvidence.model_validate({**valid.model_dump(), **changes})


async def test_thread_exclusion_blocks_new_semantic_receipts_before_memory_cleanup() -> None:
    factory, service, source, fact, _ = await semantic_stack()
    key = hashlib.sha256(b"work:t1").hexdigest()
    async with factory() as uow:
        await uow.email.put(
            EmailRecord(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                kind="excluded_source",
                key=key,
                revision=1,
                payload={
                    "account_id": "work",
                    "provider_thread_id": "t1",
                    "status": "cleanup_pending",
                },
                created_at=NOW,
                updated_at=NOW,
            ),
            expected_revision=0,
        )
    with pytest.raises(ConflictError, match="excluded"):
        await service.register_source(source)
    with pytest.raises(ConflictError, match="excluded"):
        await service.form(source, [fact])


@pytest.mark.parametrize(
    "relative_path", ["domain/email.py", "context/rendering.py", "runtime/email_state.py"]
)
def test_semantic_evidence_digest_covers_extracted_assessment_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
) -> None:
    original_digest = semantic_implementation_sha256()
    original_read = Path.read_bytes

    def changed_read(path: Path) -> bytes:
        value = original_read(path)
        return (
            value + b"\n# changed policy dependency\n"
            if path.as_posix().endswith("/" + relative_path)
            else value
        )

    monkeypatch.setattr(Path, "read_bytes", changed_read)
    assert semantic_implementation_sha256() != original_digest
