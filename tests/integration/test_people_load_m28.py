"""Opt-in, synthetic representative PostgreSQL load; never points at a live owner.

Run against a disposable migrated database with RUN_PEOPLE_LOAD_TESTS=1. The
normal integration fixture empties that database before this case, as it does
for every integration test. This suite measures server/application reads, not
provider latency or production activation.
"""

import json
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Select, event, insert, text
from sqlalchemy.sql import ClauseElement

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.memory_repositories import _memory_values
from agent_core.adapters.persistence.people import PostgresPeopleStore, _name
from agent_core.adapters.persistence.sqlalchemy_models import (
    MemoryRevisionRow,
    MemoryRow,
    PeopleHeadRow,
    PeopleLinkRow,
    PeopleRevisionRow,
)
from agent_core.application.people_context import PeopleContextService
from agent_core.bootstrap import build
from agent_core.domain.memory import SENSITIVITY_ORDER, RecallQuery, Sensitivity
from agent_core.domain.people import (
    InteractionParticipant,
    PeopleInteraction,
    PeopleQuery,
    PeopleRecord,
    PeopleSource,
    Person,
    PersonMemoryLink,
    event_time,
    referenced_assignments,
    referenced_organizations,
    referenced_people,
)
from tests.contract.memory_fixtures import memory
from tests.contract.support import NOW, principal
from tests.contract.support import session as source_session
from tests.integration.m2_support import database_settings


@pytest.mark.skipif(os.getenv("RUN_PEOPLE_LOAD_TESTS") != "1", reason="explicit synthetic load run")
async def test_people_representative_first_pages(record_property: Any) -> None:
    owner = principal().model_copy(
        update={"scopes": {"people.read", "memory.read", "session.read"}}
    )
    clock = FixedClock(NOW)
    engine = create_engine(database_settings().database_url)
    factory = create_session_factory(engine)
    person_count, belief_count, interaction_count = 10_000, 100_000, 100_000
    started = time.perf_counter()
    fields: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }

    def person_id(index: int) -> UUID:
        return UUID(int=1_000_000 + index % person_count)

    async def seed(rows: list[PeopleRecord]) -> None:
        heads, revisions, links = [], [], []
        for row in rows:
            heads.append(
                {
                    "tenant_id": row.tenant_id,
                    "principal_id": row.principal_id,
                    "id": row.id,
                    "kind": row.kind,
                    "revision": 1,
                    "sensitivity": SENSITIVITY_ORDER[row.sensitivity],
                    "erased": False,
                    "excluded": False,
                }
            )
            revisions.append(
                {
                    "tenant_id": row.tenant_id,
                    "principal_id": row.principal_id,
                    "entity_id": row.id,
                    "kind": row.kind,
                    "revision": 1,
                    "recorded_at": NOW,
                    "event_at": event_time(row),
                    "sensitivity": SENSITIVITY_ORDER[row.sensitivity],
                    "search_text": _name(row),
                    "source_session_id": row.session_id if isinstance(row, PeopleSource) else None,
                    "payload": row.model_dump(mode="json"),
                }
            )
            roles = {
                "source": row.support_ids,
                "person": referenced_people(row),
                "organization": referenced_organizations(row),
                "assignment": referenced_assignments(row),
            }
            for role, targets in roles.items():
                for target in set(targets):
                    links.append(
                        {
                            "tenant_id": row.tenant_id,
                            "principal_id": row.principal_id,
                            "entity_id": row.id,
                            "revision": 1,
                            "target_id": target,
                            "role": role,
                        }
                    )
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": owner.tenant_id},
            )
            await session.execute(insert(PeopleHeadRow), heads)
            await session.execute(insert(PeopleRevisionRow), revisions)
            if links:
                await session.execute(insert(PeopleLinkRow), links)
            await session.commit()

    try:
        async with (
            build(settings=database_settings(), storage="postgres", principal=owner) as app,
            app.uow_factory() as uow,
        ):
            await uow.sessions.create(source_session())
        for start in range(0, person_count, 1000):
            await seed(
                [
                    Person(
                        id=person_id(i),
                        display_name=f"Synthetic person {i:05d}",
                        state="active",
                        **fields,
                    )
                    for i in range(start, start + 1000)
                ]
            )
        for start in range(0, belief_count, 1000):
            memories = [
                memory(
                    belief_id=2_000_000 + i,
                    statement=f"Synthetic person {i % person_count:05d} enjoys chess.",
                ).model_copy(update={"store_position": i + 1})
                for i in range(start, start + 1000)
            ]
            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                    {"tenant": owner.tenant_id},
                )
                await session.execute(insert(MemoryRow), [_memory_values(row) for row in memories])
                await session.execute(
                    insert(MemoryRevisionRow),
                    [
                        {
                            "tenant_id": owner.tenant_id,
                            "principal_id": owner.principal_id,
                            "belief_id": row.id,
                            "recorded_at": NOW,
                            "payload": row.model_dump(mode="json"),
                        }
                        for row in memories
                    ],
                )
                await session.commit()
            await seed(
                [
                    PersonMemoryLink(
                        id=UUID(int=3_000_000 + i),
                        person_id=person_id(i),
                        belief_id=UUID(int=2_000_000 + i),
                        **fields,
                    )
                    for i in range(start, start + 1000)
                ]
            )
        for start in range(0, interaction_count, 1000):
            sources: list[PeopleRecord] = [
                PeopleSource(
                    id=UUID(int=4_000_000 + i),
                    session_id=source_session().id,
                    event_sequence=i + 1,
                    source_kind="owner",
                    evidence_at=NOW - timedelta(days=i % 365),
                    source_revision="synthetic-load-v1",
                    **fields,
                )
                for i in range(start, start + 1000)
            ]
            await seed(sources)
            await seed(
                [
                    PeopleInteraction(
                        id=UUID(int=5_000_000 + i),
                        support_ids=[UUID(int=4_000_000 + i)],
                        channel="chat",
                        interaction_kind="meeting",
                        attribution="owner_reported",
                        direction="reported",
                        summary="Synthetic group meeting.",
                        occurred_at=NOW - timedelta(days=i % 365),
                        precision="day",
                        participants=[
                            InteractionParticipant(
                                person_id=person_id(10 * i + j), role="participant"
                            )
                            for j in range(10)
                        ],
                        **fields,
                    )
                    for i in range(start, start + 1000)
                ]
            )
        seed_seconds = time.perf_counter() - started
        async with engine.begin() as connection:
            # Bulk fixtures assign positions directly; leave the sequence valid
            # for concurrent formation and restore-write rehearsals.
            await connection.execute(
                text(
                    "SELECT setval('memory_store_position_seq', "
                    "(SELECT max(store_position) FROM memories))"
                )
            )
            for table in (
                "people_heads",
                "people_revisions",
                "people_links",
                "memories",
                "memory_revisions",
            ):
                await connection.execute(text(f"ANALYZE {table}"))
        reports: dict[str, object] = {
            "people": person_count,
            "beliefs": belief_count,
            "participant_links": interaction_count * 10,
            "seed_seconds": seed_seconds,
            "cache": "first application read after bulk load; OS/database caches not evicted",
        }
        query = PeopleQuery(
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            kinds=["person"],
            sensitivity_ceiling=Sensitivity.SENSITIVE,
            limit=50,
        )
        statements: list[ClauseElement] = []

        def capture(_connection: Any, clause: ClauseElement, *_args: Any) -> None:
            if isinstance(clause, Select):
                statements.append(clause)

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": owner.tenant_id},
            )
            await session.execute(text("SET LOCAL statement_timeout = '10s'"))
            store = PostgresPeopleStore(session, clock)
            event.listen(engine.sync_engine, "before_execute", capture)
            try:
                operations: tuple[tuple[str, Callable[[], Awaitable[list[PeopleRecord]]]], ...] = (
                    ("directory", lambda: store.query(query)),
                    (
                        "history",
                        lambda: store.query(
                            query.model_copy(
                                update={
                                    "kinds": ["interaction"],
                                    "person_id": person_id(42),
                                    "sort": "history",
                                }
                            )
                        ),
                    ),
                )
                for name, operation in operations:
                    latencies = []
                    for _ in range(21):
                        before = time.perf_counter()
                        page = await operation()
                        latencies.append(1000 * (time.perf_counter() - before))
                        assert len(page) == 51
                    warm = sorted(latencies[1:])
                    reports[name] = {
                        "first_ms": latencies[0],
                        "warm_p50_ms": warm[9],
                        "warm_p95_ms": warm[math.ceil(0.95 * len(warm)) - 1],
                    }
            finally:
                event.remove(engine.sync_engine, "before_execute", capture)
            for name, clause in zip(
                ("directory_plan", "history_plan"), (statements[0], statements[-1]), strict=True
            ):
                sql = str(
                    clause.compile(engine.sync_engine, compile_kwargs={"literal_binds": True})
                )
                reports[name] = (
                    await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}"))
                ).scalar_one()
            reports["storage"] = [
                dict(row)
                for row in (
                    await session.execute(
                        text(
                            "SELECT relname, pg_relation_size(oid) AS bytes, "
                            "pg_indexes_size(oid) AS index_bytes FROM pg_class "
                            "WHERE relname IN ('people_heads','people_revisions',"
                            "'people_links','memories','memory_revisions')"
                        )
                    )
                ).mappings()
            ]
        async with build(
            settings=replace(database_settings(), people_enabled=True),
            storage="postgres",
            principal=owner,
            clock=clock,
        ) as app:
            context = PeopleContextService(app.uow_factory, app.memory_retriever)
            recall_query = RecallQuery(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                current_scope="project-a",
                text="What does Synthetic person 00042 enjoy?",
                min_score=0.1,
                budget_tokens=2000,
                max_items=20,
                sensitivity_ceiling=Sensitivity.SENSITIVE,
            )
            overhead = []
            for _ in range(21):
                before = time.perf_counter()
                await app.memory_retriever.recall(recall_query, session_id=source_session().id)
                baseline = time.perf_counter() - before
                before = time.perf_counter()
                recalled = await context.automatic_recall(
                    owner, recall_query, session_id=source_session().id
                )
                elapsed = time.perf_counter() - before
                assert any(item.record_id == person_id(42) for item in recalled.people)
                overhead.append(1000 * (elapsed - baseline))
            reports["context_additional_p95_ms"] = sorted(overhead[1:])[18]
        reports["chat_import_contention"] = await interactive_import_contention()
        record_property("people_load", json.dumps(reports, default=str))
        print(json.dumps(reports, default=str))
        contention = reports["chat_import_contention"]
        assert isinstance(contention, dict)
        assert contention["p95_change_percent"] < 10, contention
        context_p95 = reports["context_additional_p95_ms"]
        assert isinstance(context_p95, float) and context_p95 < 200
        for name in ("directory", "history"):
            result = reports[name]
            assert isinstance(result, dict)
            assert result["warm_p95_ms"] < 500, (name, result)
    finally:
        await engine.dispose()


async def interactive_import_contention() -> dict[str, object]:
    """Exercise the actual configured workers with continuous foreground demand."""
    import asyncio

    from agent_core.adapters.determinism import SystemClock
    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.application.people import PublicPeopleService
    from agent_core.domain.events import NewEvent
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from agent_core.domain.people_imports import PeopleImportRequest
    from agent_core.domain.runs import RunStatus
    from agent_core.runtime.worker import DurableWorker

    owner = principal().model_copy(
        update={
            "scopes": {
                "people.read",
                "people.write",
                "memory.read",
                "session.read",
                "session.write",
            }
        }
    )
    script = FakeModelScript(
        turns=[ScriptedTurn(text="The recorded synthetic preference is ceramics.")],
        on_exhausted="repeat_last",
    )
    extraction = FakeModelProvider(script, SystemClock())
    async with build(
        settings=replace(database_settings(), people_enabled=True),
        storage="postgres",
        principal=owner,
        script=script,
        model_provider_overrides={"fake": extraction},
    ) as app:
        service = app.services.people
        assert isinstance(service, PublicPeopleService)
        # This isolated fixture tests scheduling, and never mints activation evidence.
        service.imports.capture_available = True
        source = await app.services.sessions.create(owner, "general", {})
        async with app.uow_factory() as uow:
            evidence = await uow.events.append(
                NewEvent(
                    session_id=source.id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=owner.principal_id,
                    payload={"content": "Load Contact enjoys ceramics."},
                )
            )
        request = PeopleImportRequest.model_validate(
            {
                "phase": "preview",
                "session_id": source.id,
                "scope": {
                    "session_ids": [source.id],
                    "since": evidence.created_at,
                    "until": app.clock.now(),
                    "max_records": 10,
                    "max_cost_usd": "1",
                },
            }
        )
        preview = await service.create_import(
            owner,
            request,
            key="load-preview",
            ceiling=Sensitivity.SENSITIVE,
        )
        foreground = app.worker_factory("load-chat")
        assert isinstance(foreground, DurableWorker)

        async def measure(count: int, *, with_import: bool) -> dict[str, float | int]:
            run_ids = [
                await app.runs.submit("What does Synthetic person 00042 enjoy?")
                for _ in range(count)
            ]
            applied = None
            if with_import:
                applied = await service.create_import(
                    owner,
                    request.model_copy(
                        update={
                            "phase": "apply",
                            "operation_id": preview.id,
                            "expected_revision": preview.revision,
                        }
                    ),
                    key="load-apply",
                    ceiling=Sensitivity.SENSITIVE,
                )
            # Production polls the async queue whether an import exists or not.
            # Hold its configured scheduling loop constant in both arms.
            background = app.async_worker_factory("load-import")
            assert isinstance(background, DurableWorker)
            task = asyncio.create_task(background.run_forever())
            samples = []
            try:
                for run_id in run_ids:
                    started = time.perf_counter()
                    assert await foreground.run_once()
                    samples.append(1000 * (time.perf_counter() - started))
                    async with app.uow_factory() as uow:
                        assert (await uow.runs.get(run_id, owner)).status == RunStatus.COMPLETED
            finally:
                background.stop()
                await task
            claims = 0
            if applied is not None:
                assert applied.run_id is not None
                async with app.uow_factory() as uow:
                    import_run = await uow.runs.get(applied.run_id, owner)
                    claims = sum(
                        event.event_type == "run.claimed" and event.actor_id == "load-import"
                        for event in await uow.events.list_after(import_run.session_id, 0, owner)
                    )
                deferred = await service.get_import(
                    owner, applied.id, ceiling=Sensitivity.SENSITIVE
                )
                assert deferred.state == "queued" and deferred.error_code == "waiting_for_chat"
                assert deferred.records_read == deferred.records_processed == 0
                assert deferred.spent_usd == deferred.reserved_usd == 0
                assert claims > 0
            return {
                "p50_ms": sorted(samples)[len(samples) // 2],
                "p95_ms": sorted(samples)[math.ceil(0.95 * len(samples)) - 1],
                "samples": len(samples),
                "import_claims": claims,
            }

        await measure(3, with_import=False)
        baseline = await measure(40, with_import=False)
        busy = await measure(40, with_import=True)
        assert extraction.requests == [], "busy imports must not reach the provider"
        change = 100 * (busy["p95_ms"] / baseline["p95_ms"] - 1)
        result = {
            "baseline": baseline,
            "queued_import": busy,
            "p95_change_percent": change,
            "scope": "full durable Chat runtime; fixed local provider; continuous foreground queue",
            "background_workers": "configured async polling in both arms",
            "provider_network_calls": 0,
        }
        return result
