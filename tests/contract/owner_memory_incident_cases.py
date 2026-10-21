"""Fabricated incident shape shared by real persistence and memory compositions."""

import html
from uuid import UUID

from agent_core.bootstrap import Composition
from agent_core.domain.memory import BeliefType, MemoryAuthority, MemoryStatus, Portability
from agent_core.memory.retrieval import DeterministicQueryFormer
from tests.contract.memory_fixtures import memory
from tests.integration.test_memory_postgres_m9 import _remember, _user_event


async def owner_question_recall(app: Composition) -> None:
    session_id = await app.sessions.create()
    facts = [
        ("residence", "User lives at 12 Juniper Lane in Alder Bay.", BeliefType.FACT),
        ("child", "User's daughter is Mira.", BeliefType.RELATIONSHIP),
        ("vehicle", "User drives a 2027 Aster S8.", BeliefType.USER_MODEL_ATTR),
    ]
    async with app.uow_factory() as uow:
        for index, (subject, statement, kind) in enumerate(facts):
            await uow.memories.upsert_belief(
                memory(belief_id=9000 + index, statement=statement).model_copy(
                    update={
                        "tenant_id": app.principal.tenant_id,
                        "principal_id": app.principal.principal_id,
                        "source_session_id": session_id,
                        "scope": "old-project",
                        "origin_scopes": ["old-project"],
                        "subject": subject,
                        "belief_type": kind,
                        "portability": Portability.CONTEXTUAL,
                        "created_at": app.clock.now(),
                        "updated_at": app.clock.now(),
                        "valid_from": app.clock.now(),
                        "last_evidence_at": app.clock.now(),
                        "last_reinforced_at": app.clock.now(),
                        "store_position": await uow.memories.next_position(),
                    }
                )
            )
        for index in range(40):
            await uow.memories.upsert_belief(
                memory(belief_id=10000 + index, statement=f"User has a Weekday{index}.").model_copy(
                    update={
                        "tenant_id": app.principal.tenant_id,
                        "principal_id": app.principal.principal_id,
                        "source_session_id": session_id,
                        "scope": "general",
                        "subject": f"noise-{index}",
                        "belief_type": BeliefType.USER_MODEL_ATTR,
                        "authority": MemoryAuthority.INFERRED,
                        "confidence": 0.55,
                        "store_position": await uow.memories.next_position(),
                    }
                )
            )
        for index in range(4):
            await uow.memories.upsert_belief(
                memory(
                    belief_id=11000 + index,
                    statement=f"Briefing procedure {index}: "
                    + "Inspect the dashboard and report status. " * 80,
                ).model_copy(
                    update={
                        "tenant_id": app.principal.tenant_id,
                        "principal_id": app.principal.principal_id,
                        "source_session_id": session_id,
                        "scope": "user",
                        "subject": f"briefing-{index}",
                        "belief_type": BeliefType.PROCEDURE_POINTER,
                        "confidence": 1.0,
                        "corroboration_count": 10,
                        "store_position": await uow.memories.next_position(),
                    }
                )
            )
    snapshot = await app.memory_retriever.snapshot(session_id=session_id, current_scope="general")
    # The broad overview uses the bounded session core, not a new query-less task scan.
    for _, statement, _ in facts:
        assert html.escape(statement) in snapshot.rendered, (
            "The owner overview lost an eligible identity fact"
        )
    assert snapshot.tokens <= 1500
    for question, expected in [
        ("Nothing about my family?", facts[1][1]),
        ("What car do I drive?", facts[2][1]),
        ("Where do I live?", facts[0][1]),
    ]:
        query = DeterministicQueryFormer(app.principal).from_text(question)[0]
        result = await app.memory_retriever.recall(query, session_id=session_id)
        assert html.escape(expected) in result.rendered, question
        hidden = query.model_copy(
            update={"exclude_ids": (UUID(int=9000), UUID(int=9001), UUID(int=9002))}
        )
        excluded = await app.memory_retriever.recall(hidden, session_id=session_id)
        assert html.escape(expected) not in excluded.rendered


async def owner_formation_lifecycle(app: Composition) -> None:
    session_id = await app.sessions.create()
    records = []
    for statement, subject in [
        ("User has a new 2027 Aster S8.", "new 2027 Aster S8"),
        ("User owns a 2027 Aster S8.", "2027 Aster S8"),
    ]:
        source = await _user_event(app, session_id, statement)
        records.append(
            await app.memory.remember(
                session_id=session_id,
                run_id=None,
                statement=statement,
                subject=subject,
                scope="general",
                belief_type=BeliefType.USER_MODEL_ATTR,
                source_event_ids=[source],
                explicit=False,
            )
        )
    assert records[0].id == records[1].id
    assert records[1].status is MemoryStatus.ACTIVE
    assert records[1].corroboration_count == 2
    child = await _remember(
        app,
        session_id,
        "User's daughter is Mira.",
        subject="daughter",
        belief_type=BeliefType.RELATIONSHIP,
    )
    await _remember(
        app,
        session_id,
        "User has at least one daughter.",
        subject="daughter",
        belief_type=BeliefType.RELATIONSHIP,
    )
    async with app.uow_factory() as uow:
        assert (await uow.memories.get(child.id, app.principal)).status is MemoryStatus.ACTIVE


async def deleted_correction_does_not_restore_old_claim(
    app: Composition, *, session_delete: bool
) -> None:
    first_session = await app.sessions.create()
    second_session = await app.sessions.create()
    old = await _remember(app, first_session, "User lives in Alder Bay.", subject="residence")
    new = await _remember(app, second_session, "User lives in Cedar Cove.", subject="residence")
    if session_delete:
        await app.services.sessions.delete(
            app.principal.model_copy(update={"scopes": {*app.principal.scopes, "session.write"}}),
            second_session,
        )
    else:
        await app.memory.delete(new.id)
    async with app.uow_factory() as uow:
        retained = await uow.memories.get(old.id, app.principal)
    assert retained.status is MemoryStatus.SUPERSEDED
    assert retained.valid_to is not None
