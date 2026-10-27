"""Fabricated owner overview incidents through the composed production run loop."""

import asyncio
import html
from typing import Any
from uuid import UUID, uuid4

from agent_core.bootstrap import Composition
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import BeliefType, MemoryAuthority, MemoryRecord, MemoryStatus
from agent_core.domain.messages import TextPart, ToolResultItem
from agent_core.domain.people import (
    PeopleEndpoint,
    PeopleQuery,
    Person,
    RelationshipAssertion,
    RelationshipPredicate,
)
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.tool_output import content_bytes
from tests.contract.memory_fixtures import memory
from tests.gates.test_runtime_tool_budget import _requests


async def seed(
    app: Composition, session_id: UUID, *, family: bool, relatives: int = 2
) -> list[str]:
    statements = ["User has a daughter named Mira.", "Nora is User's mother."]
    statements.extend(f"User has a sibling named Relative{index}." for index in range(2, relatives))
    source_session_id = await app.sessions.create()
    async with app.uow_factory() as uow:
        for index in range(180 if family else 20):
            statement = (
                statements[index]
                if family and index < relatives
                else (
                    f"Briefing {index}: "
                    + 'Inspect "launches" and carefully compare the résumé evidence 👪. ' * 5
                )
            )
            source = await uow.events.append(
                NewEvent(
                    session_id=source_session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=app.principal.principal_id,
                    payload={"content": statement},
                )
            )
            record = memory(belief_id=80000 + index, statement=statement).model_copy(
                update={
                    "tenant_id": app.principal.tenant_id,
                    "principal_id": app.principal.principal_id,
                    "source_session_id": source_session_id,
                    "source_event_ids": [source.sequence],
                    "scope": "general",
                    "subject": f"topic-{index}",
                    "status": MemoryStatus.PROVISIONAL,
                    "authority": MemoryAuthority.INFERRED,
                    "confidence": 0.65,
                    "belief_type": BeliefType.RELATIONSHIP
                    if family and index < relatives
                    else BeliefType.FACT,
                    "valid_from": app.clock.now(),
                    "created_at": app.clock.now(),
                    "updated_at": app.clock.now(),
                    "last_evidence_at": app.clock.now(),
                    "store_position": await uow.memories.next_position(),
                }
            )
            await uow.memories.upsert_belief(MemoryRecord.model_validate(record.model_dump()))
            if family and index < relatives:
                common: dict[str, Any] = {
                    "tenant_id": app.principal.tenant_id,
                    "principal_id": app.principal.principal_id,
                    "created_at": app.clock.now(),
                    "updated_at": app.clock.now(),
                }
                person = Person(
                    id=UUID(int=90000 + index),
                    display_name=["Mira", "Nora"][index] if index < 2 else f"Relative{index}",
                    **common,
                )
                await uow.people.put(person, expected_revision=0)
                owner = PeopleEndpoint(kind="owner")
                endpoint = PeopleEndpoint(kind="person", id=person.id)
                await uow.people.put(
                    RelationshipAssertion(
                        id=uuid4(),
                        subject=owner if index == 0 else endpoint,
                        object=endpoint if index == 0 else owner,
                        predicate="parent" if index < 2 else "sibling",
                        belief_id=record.id,
                        **common,
                    ),
                    expected_revision=0,
                )
    return statements


async def overview_run(app: Composition, *, family: bool, byte_limit: int = 4096) -> None:
    session_id = await app.sessions.create()
    facts = await seed(app, session_id, family=family)
    run_id = await app.runs.submit(
        "Tell me everything you know about me. Structure it into Personal, Family, Work sections"
        if family
        else "Recall the briefing details.",
        session_id=session_id,
    )
    finished = await finish(app, run_id)
    assert finished.status is RunStatus.COMPLETED, finished.failure
    request = _requests(app)[-1]
    if family:
        rendered = html.unescape(request.model_dump_json())
        assert all(fact in rendered for fact in facts), (
            "The production request lost linked family facts"
        )
    else:
        results = [item for item in request.conversation if isinstance(item, ToolResultItem)]
        assert len(results) == 1 and not results[0].is_error
        result = results[0]
        assert result.memory_recall is not None, "The production pipeline withheld its own search"
        assert any(
            isinstance(part, TextPart) and "Briefing" in part.text for part in result.content
        )
        assert len(content_bytes(result.content)) <= byte_limit
        assert result.memory_recall.tokens <= 2000 and result.memory_recall.items <= 20


async def restricted_overview_run(app: Composition, restriction: str) -> None:
    from datetime import timedelta

    from agent_core.domain.memory import Portability, Sensitivity

    session_id = await app.sessions.create()
    facts = await seed(app, session_id, family=True)
    async with app.uow_factory() as uow:
        target = await uow.memories.get(UUID(int=80000), app.principal)
        if restriction == "erased":
            await uow.memories.fence_for_erasure(app.principal, [target.id])
        else:
            options: dict[str, dict[str, Any]] = {
                "local": {"scope": "unrelated-project", "portability": Portability.LOCAL},
                "expired": {"expires_at": app.clock.now() - timedelta(seconds=1)},
                "retracted": {"status": MemoryStatus.RETIRED},
                "corrected": {"statement": "Mira is User's niece."},
            }
            await uow.memories.reinforce(target.model_copy(update=options[restriction]))
            if restriction == "corrected":
                edges = await uow.people.query(
                    PeopleQuery(
                        tenant_id=app.principal.tenant_id,
                        principal_id=app.principal.principal_id,
                        kinds=["relationship"],
                        belief_id=target.id,
                        sensitivity_ceiling=Sensitivity.RESTRICTED,
                    )
                )
                for edge in edges:
                    await uow.people.put(
                        edge.model_copy(
                            update={
                                "predicate": "relative",
                                "revision": edge.revision + 1,
                            }
                        ),
                        expected_revision=edge.revision,
                    )
    run_id = await app.runs.submit("Tell me everything you know about me.", session_id=session_id)
    finished = await finish(app, run_id)
    assert finished.status is RunStatus.COMPLETED, finished.failure
    rendered = html.unescape(_requests(app)[-1].model_dump_json())
    assert facts[0] not in rendered
    assert facts[1] in rendered
    if restriction == "corrected":
        assert "Mira is User's niece." in rendered
        assert "owner --parent--> Mira" not in rendered


async def paged_overview_run(app: Composition, *, relatives: int = 5) -> None:
    # An already-open chat must gain current People context without replacing its prefix.
    session_id = await app.sessions.create()
    first = await app.runs.submit("Hello", session_id=session_id)
    assert (await finish(app, first)).status is RunStatus.COMPLETED
    facts = await seed(app, session_id, family=True, relatives=relatives)
    run_id = await app.runs.submit("Tell me everything you know about me.", session_id=session_id)
    assert (await finish(app, run_id)).status is RunStatus.COMPLETED
    requests = _requests(app)
    initial = html.unescape(requests[-2].model_dump_json())
    assert all(fact in initial for fact in facts[:2])
    assert ("Continue memory.search" in initial) is (relatives > 3)
    if relatives > 3:
        assert str(UUID(int=90002)) in initial and "people_after" in initial
    final = requests[-1]
    result = next(item for item in reversed(final.conversation) if isinstance(item, ToolResultItem))
    assert not result.is_error and result.memory_recall is not None
    rendered = html.unescape(
        "".join(part.text for part in result.content if isinstance(part, TextPart))
    )
    assert all(fact in rendered for fact in facts[3:])
    assert "Continue memory.search" not in rendered
    assert len(content_bytes(result.content)) <= 4096
    assert "owner --parent--> Mira" in initial
    assert "Nora --parent--> owner" in initial
    assert result.memory_recall.trace_id is not None
    async with app.uow_factory() as uow:
        trace = await uow.traces.get(result.memory_recall.trace_id, app.principal)
    assert len({person for row in trace.people for person in row.person_ids}) <= 3
    assert trace.query.people_after == UUID(int=90002)
    assert await app.memory_retriever.validate_retained(
        result.memory_recall,
        session_id=session_id,
        run_id=run_id,
    )
    if relatives == 3:
        return  # The second page is empty, so it has no relationship evidence to erase.
    async with app.uow_factory() as uow:
        await uow.memories.fence_for_erasure(app.principal, [UUID(int=80003)])
    assert not await app.memory_retriever.validate_retained(
        result.memory_recall,
        session_id=session_id,
        run_id=run_id,
    ), "An erased backing belief must invalidate retained People evidence"


CLOSENESS: tuple[tuple[int, str, RelationshipPredicate, bool], ...] = (
    # (id, name, predicate, owner holds the subject end); ids sort far from closeness.
    (90010, "Sib", "sibling", True),
    (90011, "Cousin", "relative", True),
    (90012, "Riv", "parent", True),
    (90013, "Mom", "parent", False),
    (90014, "Kyrri", "parent", True),
    (90015, "Erin", "spouse", True),
)


async def seed_closeness(app: Composition) -> None:
    """The owner's family in `CLOSENESS`: a relationship memory and edge each."""

    source_session_id = await app.sessions.create()
    async with app.uow_factory() as uow:
        for number, name, predicate, owner_first in CLOSENESS:
            source = await uow.events.append(
                NewEvent(
                    session_id=source_session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=app.principal.principal_id,
                    payload={"content": f"{name} is family."},
                )
            )
            record = memory(belief_id=number - 10000, statement=f"{name} is family.").model_copy(
                update={
                    "tenant_id": app.principal.tenant_id,
                    "principal_id": app.principal.principal_id,
                    "source_session_id": source_session_id,
                    "source_event_ids": [source.sequence],
                    "scope": "general",
                    "subject": f"family-{name}",
                    "belief_type": BeliefType.RELATIONSHIP,
                    "valid_from": app.clock.now(),
                    "created_at": app.clock.now(),
                    "updated_at": app.clock.now(),
                    "last_evidence_at": app.clock.now(),
                    "store_position": await uow.memories.next_position(),
                }
            )
            await uow.memories.upsert_belief(MemoryRecord.model_validate(record.model_dump()))
            common: dict[str, Any] = {
                "tenant_id": app.principal.tenant_id,
                "principal_id": app.principal.principal_id,
                "created_at": app.clock.now(),
                "updated_at": app.clock.now(),
            }
            person = Person(id=UUID(int=number), display_name=name, state="active", **common)
            await uow.people.put(person, expected_revision=0)
            owner = PeopleEndpoint(kind="owner")
            endpoint = PeopleEndpoint(kind="person", id=person.id)
            await uow.people.put(
                RelationshipAssertion(
                    id=uuid4(),
                    subject=owner if owner_first else endpoint,
                    object=endpoint if owner_first else owner,
                    predicate=predicate,
                    belief_id=record.id,
                    **common,
                ),
                expected_revision=0,
            )


async def closeness_overview_run(app: Composition) -> None:
    """ADR-0259: the first overview page holds the closest family, not the lowest ids.

    Production: Riv was on page 2 because pages held three people in id order,
    and the automatic path only ever renders page 1.
    """

    session_id = await app.sessions.create()
    await seed_closeness(app)
    run_id = await app.runs.submit("Tell me everything you know about me.", session_id=session_id)
    assert (await finish(app, run_id)).status is RunStatus.COMPLETED
    initial = html.unescape(_requests(app)[-1].model_dump_json())
    for edge in ("owner --spouse--> Erin", "owner --parent--> Riv", "owner --parent--> Kyrri"):
        assert edge in initial, edge
    for name in ("Sib", "Cousin", "Mom"):
        assert f"--> {name}" not in initial and f"{name} --" not in initial, name
    # The continuation names the last person shown, in closeness order.
    assert str(UUID(int=90014)) in initial and "people_after" in initial


async def finish(app: Composition, run_id: UUID) -> Run:
    from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWorkFactory
    from agent_core.runtime.worker import DurableWorker

    if isinstance(app.uow_factory, PostgresUnitOfWorkFactory):
        worker = app.worker_factory("owner-overview-regression")
        assert isinstance(worker, DurableWorker)
        assert await asyncio.wait_for(worker.run_once(), 60)
    return await asyncio.wait_for(app.runs.wait_terminal(run_id), 60)
