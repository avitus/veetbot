"""Real PostgreSQL contracts for People revisions and source visibility."""

from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.people import PostgresPeopleStore
from agent_core.adapters.persistence.sqlalchemy_models import PeopleHeadRow
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import Person
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import NOW, principal
from tests.contract.test_people_store_contract import (
    independent_people_support_contract,
    people_directory_contract,
    people_duplicate_source_erasure_contract,
    people_email_erasure_contract,
    people_history_paging_contract,
    people_interaction_redirect_contract,
    people_recent_directory_contract,
    people_source_visibility_contract,
    people_store_contract,
    people_transitive_visibility_contract,
)
from tests.integration.m2_support import database_settings


async def test_postgres_import_priority_checks_due_work_for_the_same_owner() -> None:
    from agent_core.bootstrap import build
    from tests.contract.test_run_repository_contract import higher_priority_work_contract

    clock = FixedClock(NOW)
    async with build(settings=database_settings(), storage="postgres", clock=clock) as app:
        await higher_priority_work_contract(app.uow_factory, clock)


async def test_postgres_configured_async_worker_defers_and_resumes_people_import() -> None:
    from dataclasses import replace
    from datetime import datetime, timedelta

    from agent_core.adapters.determinism import SystemClock
    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.application.people import PublicPeopleService
    from agent_core.bootstrap import build
    from agent_core.domain.events import NewEvent
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from agent_core.domain.people_imports import PeopleImportRequest
    from agent_core.domain.runs import RunStatus
    from agent_core.runtime.worker import DurableWorker
    from tests.contract.support import run

    class OffsetClock(SystemClock):
        offset = timedelta(0)

        def now(self) -> datetime:
            return super().now() + self.offset

    clock = OffsetClock()
    owner = principal().model_copy(
        update={"scopes": {"people.read", "people.write", "session.read", "session.write"}}
    )
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="{}") for _ in range(3)]), SystemClock()
    )
    async with build(
        settings=replace(database_settings(), people_enabled=True),
        storage="postgres",
        clock=clock,
        principal=owner,
        model_provider_overrides={"fake": provider},
    ) as app:
        source = await app.services.sessions.create(owner, "general", {})
        chat = await app.services.sessions.create(owner, "general", {})
        active = run(status=RunStatus.RUNNING).model_copy(
            update={"session_id": chat.id, "priority": 0}
        )
        async with app.uow_factory() as uow:
            event = await uow.events.append(
                NewEvent(
                    session_id=source.id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=owner.principal_id,
                    payload={"content": "I prefer jasmine tea."},
                )
            )
            await uow.runs.create(active)
        service = app.services.people
        assert isinstance(service, PublicPeopleService)
        # Synthetic queue contract only; no runtime activation evidence is minted.
        service.imports.capture_available = True
        request = PeopleImportRequest.model_validate(
            {
                "phase": "preview",
                "session_id": str(source.id),
                "scope": {
                    "session_ids": [str(source.id)],
                    "since": event.created_at,
                    "until": app.clock.now(),
                    "max_records": 10,
                    "max_cost_usd": "1",
                },
            }
        )
        preview = await service.create_import(
            owner, request, key="queue-preview", ceiling=Sensitivity.SENSITIVE
        )
        applied = await service.create_import(
            owner,
            request.model_copy(
                update={
                    "phase": "apply",
                    "operation_id": preview.id,
                    "expected_revision": preview.revision,
                }
            ),
            key="queue-apply",
            ceiling=Sensitivity.SENSITIVE,
        )
        worker = app.async_worker_factory("people-contract")
        assert isinstance(worker, DurableWorker)
        assert await worker.run_once(), "configured async worker must claim the People import"
        deferred = await service.get_import(owner, applied.id, ceiling=Sensitivity.SENSITIVE)
        assert deferred.state == "queued" and deferred.error_code == "waiting_for_chat"
        assert deferred.records_read == deferred.records_processed == 0
        assert provider.requests == []
        assert not await worker.run_once(), "the deferred slice must respect its scheduled time"
        async with app.uow_factory() as uow:
            await uow.runs.transition(active.id, RunStatus.RUNNING, RunStatus.COMPLETED)
        clock.offset = timedelta(seconds=31)
        assert await worker.run_once()
        resumed = await service.get_import(owner, applied.id, ceiling=Sensitivity.SENSITIVE)
        assert resumed.state == "failed" and resumed.error_code == "analysis_incomplete"
        assert resumed.records_read == 1 and len(provider.requests) == 3


@pytest.mark.parametrize("reference", ["event", "recall"])
@pytest.mark.parametrize("assignments", [0, 300])
async def test_postgres_people_forget_cancels_recalled_runs(
    reference: str, assignments: int
) -> None:
    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.unit.test_people_erasure import active_erasure_contract

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(settings=database_settings(), storage="postgres", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await active_erasure_contract(
            app.uow_factory, app.clock, owner, assignments=assignments, reference=reference
        )


@pytest.mark.parametrize("assignments", [0, 300])
async def test_postgres_reapplies_newer_erasure_to_a_restored_snapshot(assignments: int) -> None:
    from agent_core.bootstrap import build
    from tests.contract.support import memory_uow_factory, session
    from tests.unit.test_people_erasure import restore_erasure_contract

    clock, live = await memory_uow_factory()

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(
        settings=database_settings(), storage="postgres", principal=owner, fixed_clock_at=NOW
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await restore_erasure_contract(live, app.uow_factory, clock, owner, assignments=assignments)


async def test_postgres_late_artifacts_keep_erasure_receipts_exportable() -> None:
    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.unit.test_people_erasure import active_erasure_contract

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(settings=database_settings(), storage="postgres", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await active_erasure_contract(
            app.uow_factory,
            app.clock,
            owner,
            assignments=300,
            reference="recall",
            late_artifact_count=3001,
        )


@pytest.mark.parametrize("operation", ["search", "history"])
async def test_postgres_people_tools_register_influence_before_return(operation: str) -> None:
    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.unit.test_people_tools import tool_read_erasure_contract

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(settings=database_settings(), storage="postgres", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await tool_read_erasure_contract(app.uow_factory, app.clock, owner, operation=operation)


async def test_postgres_people_store_contract() -> None:
    engine = create_engine(database_settings().database_url)
    try:
        async with create_session_factory(engine)() as session:
            await session.execute(
                text("SELECT set_config('agent_core.tenant_id', 'tenant-a', true)")
            )
            await people_store_contract(PostgresPeopleStore(session, FixedClock(NOW)))
            await people_source_visibility_contract(PostgresPeopleStore(session, FixedClock(NOW)))
            await people_transitive_visibility_contract(
                PostgresPeopleStore(session, FixedClock(NOW))
            )
            await people_history_paging_contract(PostgresPeopleStore(session, FixedClock(NOW)))
            await independent_people_support_contract(PostgresPeopleStore(session, FixedClock(NOW)))
            await people_email_erasure_contract(PostgresPeopleStore(session, FixedClock(NOW)))
            await people_directory_contract(PostgresPeopleStore(session, FixedClock(NOW)))
            await people_recent_directory_contract(PostgresPeopleStore(session, FixedClock(NOW)))
            await people_duplicate_source_erasure_contract(
                PostgresPeopleStore(session, FixedClock(NOW))
            )
            await people_interaction_redirect_contract(
                PostgresPeopleStore(session, FixedClock(NOW))
            )
            await session.commit()
    finally:
        await engine.dispose()


async def test_people_forced_rls_and_rollback() -> None:
    engine = create_engine(database_settings().database_url)
    factory = create_session_factory(engine)
    owner = principal()
    role = f"people28_probe_{uuid4().hex}"
    person = Person(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        display_name="Fixture",
        created_at=NOW,
        updated_at=NOW,
    )
    try:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('agent_core.tenant_id', 'tenant-a', true)")
            )
            await PostgresPeopleStore(session, FixedClock(NOW)).put(person, expected_revision=0)
            await session.rollback()
            assert (
                await PostgresPeopleStore(session, FixedClock(NOW)).get(
                    owner, person.id, ceiling=Sensitivity.RESTRICTED
                )
                is None
            )
            await PostgresPeopleStore(session, FixedClock(NOW)).put(person, expected_revision=0)
            await session.execute(text(f"CREATE ROLE {role} NOLOGIN"))
            await session.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await session.execute(
                text(
                    "GRANT SELECT, INSERT, UPDATE ON people_heads, people_revisions, "
                    f"people_links TO {role}"
                )
            )
            await session.commit()
        try:
            async with factory() as session:
                flags = (
                    await session.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                            "WHERE relname IN ('people_heads','people_revisions','people_links')"
                        )
                    )
                ).all()
                assert [tuple(row) for row in flags] == [(True, True)] * 3
                await session.execute(
                    text("SELECT set_config('agent_core.tenant_id', 'tenant-a', true)")
                )
                await session.execute(text(f"SET LOCAL ROLE {role}"))
                assert len((await session.execute(select(PeopleHeadRow))).scalars().all()) == 1
                await session.execute(
                    text("SELECT set_config('agent_core.tenant_id', 'foreign', true)")
                )
                assert (await session.execute(select(PeopleHeadRow))).scalars().all() == []
                with pytest.raises(DBAPIError) as denied:
                    async with session.begin_nested():
                        await PostgresPeopleStore(session, FixedClock(NOW)).put(
                            person.model_copy(update={"id": uuid4()}), expected_revision=0
                        )
                assert getattr(denied.value.orig, "sqlstate", None) == "42501"
        finally:
            async with factory() as session:
                await session.execute(text(f"DROP OWNED BY {role}"))
                await session.execute(text(f"DROP ROLE {role}"))
                await session.commit()
    finally:
        await engine.dispose()


async def test_postgres_people_management_and_identity_repair() -> None:
    from agent_core.application.people import PublicPeopleService
    from agent_core.application.people_identity import PeopleIdentityService
    from agent_core.bootstrap import build
    from agent_core.domain.people_views import CreatePerson, UpdatePerson
    from tests.contract.support import session

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(settings=database_settings(), storage="postgres", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        service = PublicPeopleService(app.uow_factory, app.clock)
        first = await service.create(
            owner,
            CreatePerson(session_id=session().id, display_name="Alex"),
            key="first",
            ceiling=Sensitivity.SENSITIVE,
        )
        second = await service.create(
            owner,
            CreatePerson(session_id=session().id, display_name="Al"),
            key="second",
            ceiling=Sensitivity.SENSITIVE,
        )
        changed = await service.update(
            owner,
            first.id,
            UpdatePerson(session_id=session().id, expected_revision=1, pinned=True),
            key="pin",
            ceiling=Sensitivity.SENSITIVE,
        )
        assert changed.pinned and changed.revision == 2
        replay = await service.create(
            owner,
            CreatePerson(session_id=session().id, display_name="Alex"),
            key="first",
            ceiling=Sensitivity.SENSITIVE,
        )
        assert replay == first
        identity = PeopleIdentityService(app.uow_factory, app.clock, app.ids)
        preview = await identity.preview_merge(
            owner,
            first.id,
            second.id,
            expected_revisions={first.id: 2, second.id: 1},
            ceiling=Sensitivity.SENSITIVE,
        )
        await identity.apply(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
        undo = await identity.preview_undo(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
        await identity.apply(owner, undo.id, ceiling=Sensitivity.SENSITIVE)
        async with app.uow_factory() as uow:
            await uow.session_deletions.delete(session().id, owner, app.clock.now())
            assert await uow.people.get(owner, first.id, ceiling=Sensitivity.RESTRICTED) is None


async def test_postgres_historical_beliefs_and_erasure() -> None:
    from agent_core.adapters.determinism import FixedClock
    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.contract.test_memory_store_contract import historical_memory_contract

    clock = FixedClock(NOW)
    async with (
        build(
            settings=database_settings(), storage="postgres", principal=principal(), clock=clock
        ) as app,
        app.uow_factory() as uow,
    ):
        await uow.sessions.create(session())
        await historical_memory_contract(uow.memories, clock)


async def test_postgres_alias_search_filters_before_paging() -> None:
    from agent_core.domain.people import PeopleQuery, PersonIdentifier

    engine = create_engine(database_settings().database_url)
    owner = principal()
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Maya", **common)
    alias = PersonIdentifier(
        id=uuid4(),
        person_id=person.id,
        identifier_kind="email",
        namespace="owner",
        value="maya@example.test",
        context="owner",
        verification="owner_confirmed",
        valid_from=NOW,
        **common,
    )
    try:
        async with create_session_factory(engine)() as session:
            store = PostgresPeopleStore(session, FixedClock(NOW))
            await store.put(person, expected_revision=0)
            await store.put(alias, expected_revision=0)
            query = PeopleQuery(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                sensitivity_ceiling=Sensitivity.SENSITIVE,
                kinds=["person"],
                search_aliases=True,
                text="maya@example.test",
                as_of=NOW,
                limit=1,
            )
            assert await store.query(query) == [person]
            assert (
                await store.query(
                    query.model_copy(update={"sensitivity_ceiling": Sensitivity.INTERNAL})
                )
                == []
            )
    finally:
        await engine.dispose()


async def test_postgres_bounded_import_source_window_and_protected_decay() -> None:
    from datetime import timedelta

    from agent_core.adapters.determinism import FixedClock
    from agent_core.bootstrap import build
    from agent_core.domain.events import NewEvent
    from tests.contract.support import session as source_session
    from tests.contract.test_memory_store_contract import protected_people_decay_page_contract

    clock = FixedClock(NOW)
    owner = principal()
    async with build(
        settings=database_settings(), storage="postgres", principal=owner, clock=clock
    ) as app:
        first = source_session()
        second = first.model_copy(update={"id": uuid4()})
        async with app.uow_factory() as uow:
            await uow.sessions.create(first)
            await uow.sessions.create(second)
            for source in [second, first, second]:
                await uow.events.append(
                    NewEvent(
                        session_id=source.id,
                        run_id=None,
                        event_type="user.message.created",
                        actor_type="principal",
                        actor_id=owner.principal_id,
                        payload={"content": "A source"},
                    )
                )
                clock.advance(timedelta(seconds=1))
            options: dict[str, Any] = {
                "session_ids": [first.id, second.id],
                "since": NOW,
                "until": clock.now(),
                "limit": 2,
            }
            page = await uow.events.list_window(owner, **options)
            assert [event.session_id for event in page] == [second.id, first.id]
            remaining = await uow.events.list_window(
                owner, after=(page[-1].created_at, page[-1].id), **options
            )
            assert len(remaining) == 1 and remaining[0].session_id == second.id
            await protected_people_decay_page_contract(uow.memories)


async def test_postgres_import_preview_and_queue_survive_service_recreation() -> None:
    from datetime import timedelta

    from agent_core.application.people_imports import PeopleImportService
    from agent_core.bootstrap import build
    from agent_core.domain.people_imports import PeopleImportRequest
    from tests.contract.support import session

    owner = principal().model_copy(
        update={"scopes": {"people.read", "people.write", "session.read"}}
    )
    async with build(settings=database_settings(), storage="postgres", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        service = PeopleImportService(app.uow_factory, app.clock)
        service.implementation_identity = lambda: "a" * 64
        dispatched = []

        async def dispatch(run_id: UUID) -> None:
            dispatched.append(run_id)

        service.dispatch = dispatch
        service.capture_available = True
        request = PeopleImportRequest.model_validate(
            {
                "phase": "preview",
                "session_id": str(session().id),
                "scope": {
                    "session_ids": [str(session().id)],
                    "since": NOW.isoformat(),
                    "until": (NOW + timedelta(days=1)).isoformat(),
                    "max_records": 20,
                    "max_cost_usd": "1",
                },
            }
        )
        preview = await service.submit(owner, request, key="preview", ceiling=Sensitivity.SENSITIVE)
        queued = await service.submit(
            owner,
            request.model_copy(
                update={
                    "phase": "apply",
                    "operation_id": preview.id,
                    "expected_revision": preview.revision,
                }
            ),
            key="apply",
            ceiling=Sensitivity.SENSITIVE,
        )
        restored = PeopleImportService(app.uow_factory, app.clock)
        assert await restored.get(owner, preview.id, ceiling=Sensitivity.SENSITIVE) == queued
        assert queued.state == "queued" and dispatched == [queued.run_id]


async def test_postgres_historical_recall_filters_original_revisions() -> None:
    from agent_core.adapters.determinism import FixedClock
    from agent_core.bootstrap import build
    from tests.contract.support import session as source_session
    from tests.contract.test_memory_store_contract import historical_recall_contract

    clock = FixedClock(NOW)
    async with (
        build(
            settings=database_settings(), storage="postgres", principal=principal(), clock=clock
        ) as app,
        app.uow_factory() as uow,
    ):
        await uow.sessions.create(source_session())
        await historical_recall_contract(uow.memories, clock)


async def test_populated_people_migration_preserves_records_and_guards_downgrade(
    tmp_path: Path,
) -> None:
    import json
    from datetime import UTC, datetime

    from agent_core.adapters.determinism import FixedClock
    from agent_core.adapters.persistence.memory_repositories import PostgresMemoryStore
    from agent_core.adapters.persistence.sqlalchemy_models import MemoryRevisionRow
    from agent_core.bootstrap import build
    from agent_core.domain.errors import NotFoundError
    from tests.contract.memory_fixtures import memory
    from tests.contract.support import session as source_session
    from tests.integration.test_memory_distillation_postgres_m21 import _alembic

    settings = database_settings()
    async with (
        build(settings=settings, storage="postgres", principal=principal()) as app,
        app.uow_factory() as uow,
    ):
        await uow.sessions.create(source_session())
        await uow.memories.upsert_belief(memory())
    with pytest.raises(RuntimeError, match="exported and erased"):
        _alembic("downgrade", "b27c41d9e602")
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine)() as session:
            rows = (await session.scalars(select(MemoryRevisionRow))).all()
            exported = tmp_path / "synthetic-memory-revisions.json"
            exported.write_text(json.dumps([row.payload for row in rows]))
            assert json.loads(exported.read_text())[0]["statement"] == memory().statement
            await session.execute(text("DELETE FROM memory_revisions"))
            await session.commit()
        _alembic("downgrade", "b27c41d9e602")
        before = datetime.now(UTC)
        _alembic("upgrade", "head")
        async with create_session_factory(engine)() as session:
            row = (await session.scalars(select(MemoryRevisionRow))).one()
            assert row.recorded_at >= before
            store = PostgresMemoryStore(session, FixedClock(NOW))
            assert (await store.get(memory().id, principal())).statement == memory().statement
            with pytest.raises(NotFoundError):
                await store.get_at(memory().id, principal(), known_at=before)
            assert (
                await store.get_at(memory().id, principal(), known_at=datetime.now(UTC))
            ).statement == memory().statement
    finally:
        _alembic("upgrade", "head")
        await engine.dispose()


async def test_memory_revisions_enforce_tenant_rls_and_cross_owner_foreign_keys() -> None:
    from agent_core.adapters.persistence.sqlalchemy_models import MemoryRevisionRow
    from agent_core.bootstrap import build
    from tests.contract.memory_fixtures import memory
    from tests.contract.support import session as source_session

    owner = principal()
    foreign = owner.model_copy(update={"tenant_id": "foreign"})
    foreign_belief = memory(belief_id=9919).model_copy(
        update={"tenant_id": "foreign", "source_session_id": uuid4(), "store_position": 2}
    )
    async with (
        build(settings=database_settings(), storage="postgres", principal=owner) as app,
        app.uow_factory() as uow,
    ):
        await uow.sessions.create(source_session())
        await uow.sessions.create(
            source_session().model_copy(
                update={"id": foreign_belief.source_session_id, "tenant_id": foreign.tenant_id}
            )
        )
        await uow.memories.upsert_belief(memory())
        await uow.memories.upsert_belief(foreign_belief)
    engine = create_engine(database_settings().database_url)
    role = f"memory28_probe_{uuid4().hex}"
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(text(f"CREATE ROLE {role} NOLOGIN"))
            await session.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await session.execute(text(f"GRANT SELECT, INSERT ON memory_revisions TO {role}"))
            await session.execute(
                text(f"GRANT USAGE ON SEQUENCE memory_revisions_id_seq TO {role}")
            )
            await session.commit()
        async with factory() as session:
            flags = (
                await session.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname='memory_revisions'"
                    )
                )
            ).one()
            assert tuple(flags) == (True, True)
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            await session.execute(
                text("SELECT set_config('agent_core.tenant_id', 'tenant-a', true)")
            )
            assert [
                row.belief_id for row in (await session.scalars(select(MemoryRevisionRow))).all()
            ] == [memory().id]
            with pytest.raises(DBAPIError) as rejected:
                async with session.begin_nested():
                    await session.execute(
                        insert(MemoryRevisionRow).values(
                            tenant_id=owner.tenant_id,
                            principal_id=owner.principal_id,
                            belief_id=foreign_belief.id,
                            recorded_at=NOW,
                            payload=memory().model_dump(mode="json"),
                        )
                    )
            assert getattr(rejected.value.orig, "sqlstate", None) == "23503"
    finally:
        async with factory() as session:
            await session.execute(text(f"DROP OWNED BY {role}"))
            await session.execute(text(f"DROP ROLE {role}"))
            await session.commit()
        await engine.dispose()


async def test_postgres_relationship_directory_applies_belief_privacy_before_pagination() -> None:
    from tests.gates.test_people_api_m28 import people_relationship_directory_api_contract

    await people_relationship_directory_api_contract("postgres", database_settings())


@pytest.mark.parametrize(
    "failure",
    [None, "collision", "unrelated", "suppressed", "expired_alias", "external", "repaired"],
)
async def test_postgres_legacy_links_preserve_provenance_and_owner_repairs(
    failure: str | None,
) -> None:
    from dataclasses import replace

    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.unit.test_people_legacy import legacy_link_contract

    async with build(
        settings=replace(database_settings(), people_enabled=True),
        storage="postgres",
        principal=principal(),
        clock=FixedClock(NOW),
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await legacy_link_contract(app.uow_factory, app.clock, failure)


async def test_postgres_long_erasure_retains_all_suppression_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.unit.test_people_erasure import long_erasure_contract

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(
        settings=database_settings(), storage="postgres", principal=owner, clock=FixedClock(NOW)
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())

        enter = PostgresUnitOfWork.__aenter__

        async def timed_enter(self: PostgresUnitOfWork) -> PostgresUnitOfWork:
            opened = await enter(self)
            await opened.session.execute(text("SET LOCAL statement_timeout = '500ms'"))
            return opened

        monkeypatch.setattr(PostgresUnitOfWork, "__aenter__", timed_enter)
        await long_erasure_contract(app.uow_factory, app.clock, owner, count=513)


async def test_large_erasure_does_not_exceed_the_postgres_parameter_limit() -> None:
    from agent_core.adapters.persistence.sqlalchemy_models import PeopleRevisionRow

    owner = principal()
    engine = create_engine(database_settings().database_url)
    target = Person(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        created_at=NOW,
        updated_at=NOW,
        display_name="Synthetic large history",
    )
    survivor = target.model_copy(update={"id": uuid4(), "display_name": "Independent person"})
    try:
        async with create_session_factory(engine)() as session:
            await session.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": owner.tenant_id},
            )
            store = PostgresPeopleStore(session, FixedClock(NOW))
            await store.put(target, expected_revision=0)
            await store.put(survivor, expected_revision=0)
            parameters = {
                "tenant": owner.tenant_id,
                "owner": owner.principal_id,
                "target": target.id,
                "now": NOW,
            }
            # Bound setup as well as erasure so an accidental fixture scan
            # cannot consume the hosted job's entire no-output allowance.
            await session.execute(text("SET LOCAL statement_timeout = '30s'"))
            await session.execute(
                text("""
                INSERT INTO people_heads
                    (tenant_id, principal_id, id, kind, revision, sensitivity, erased, excluded)
                SELECT :tenant, :owner,
                       ('10000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid,
                       'identifier', 1, 0, false, false
                FROM generate_series(1, 33000) AS g(i)
            """),
                parameters,
            )
            # This uncommitted bulk load cannot receive autovacuum statistics.
            # Refresh each parent before the next stage validates its FKs.
            await session.execute(text("ANALYZE people_heads"))
            await session.execute(
                text("""
                INSERT INTO people_revisions (tenant_id, principal_id, entity_id, revision, kind,
                    recorded_at, event_at, sensitivity, search_text, payload)
                SELECT tenant_id, principal_id, id, 1, kind, :now, :now, 0, 'synthetic',
                    jsonb_build_object('id', id, 'kind', kind, 'tenant_id', tenant_id,
                        'principal_id', principal_id, 'revision', 1, 'sensitivity', 'public',
                        'created_at', CAST(:now AS timestamptz),
                        'updated_at', CAST(:now AS timestamptz),
                        'support_ids', '[]'::jsonb, 'person_id', CAST(:target AS uuid),
                        'identifier_kind', 'name', 'namespace', 'owner', 'value', 'Synthetic',
                        'context', '', 'verification', 'contextual',
                        'valid_from', CAST(:now AS timestamptz))
                FROM people_heads
                WHERE tenant_id=:tenant AND principal_id=:owner AND kind='identifier'
            """),
                parameters,
            )
            await session.execute(text("ANALYZE people_revisions"))
            await session.execute(
                text("""
                INSERT INTO people_links
                    (tenant_id, principal_id, entity_id, revision, target_id, role)
                SELECT tenant_id, principal_id, id, 1, :target, 'person'
                FROM people_heads
                WHERE tenant_id=:tenant AND principal_id=:owner AND kind='identifier'
            """),
                parameters,
            )
            await session.execute(text("ANALYZE people_links"))
            ids = [target.id, *(UUID(f"10000000-0000-0000-0000-{i:012d}") for i in range(1, 33001))]
            assert await store.erase(owner, ids) == 33001
            assert await store.get(owner, target.id, ceiling=Sensitivity.RESTRICTED) is None
            assert await store.get(owner, survivor.id, ceiling=Sensitivity.RESTRICTED) == survivor
            remaining = list((await session.scalars(select(PeopleRevisionRow.entity_id))).all())
            assert remaining == [survivor.id]
    finally:
        await engine.dispose()


async def test_postgres_memory_erasure_fence_precedes_physical_cleanup() -> None:
    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.contract.test_memory_store_contract import memory_erasure_fence_contract

    async with (
        build(
            settings=database_settings(),
            storage="postgres",
            principal=principal(),
            fixed_clock_at=NOW,
        ) as app,
        app.uow_factory() as uow,
    ):
        await uow.sessions.create(session())
        await memory_erasure_fence_contract(uow.memories)


async def test_postgres_people_fence_precedes_bounded_revision_cleanup() -> None:
    from agent_core.bootstrap import build
    from tests.contract.test_people_store_contract import people_erasure_fence_contract

    async with (
        build(
            settings=database_settings(),
            storage="postgres",
            principal=principal(),
            fixed_clock_at=NOW,
        ) as app,
        app.uow_factory() as uow,
    ):
        await people_erasure_fence_contract(uow.people)


async def test_postgres_forget_resumes_fenced_memory_and_people_batches() -> None:
    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.unit.test_people_erasure import staged_erasure_contract

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(
        settings=database_settings(), storage="postgres", principal=owner, fixed_clock_at=NOW
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await staged_erasure_contract(app.uow_factory, app.clock, owner)


async def test_postgres_erasure_discovers_references_without_loading_unrelated_payloads() -> None:
    from sqlalchemy import event

    from agent_core.adapters.persistence.mappers import event_values
    from agent_core.adapters.persistence.sqlalchemy_models import EventRow
    from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
    from agent_core.bootstrap import build
    from agent_core.domain.events import NewEvent
    from agent_core.domain.runs import RunStatus
    from tests.contract.support import run, session

    owner = principal()
    target = uuid4()
    loaded_payloads = 0

    def loaded(row: EventRow, context: Any) -> None:
        nonlocal loaded_payloads
        loaded_payloads += 1

    async with build(settings=database_settings(), storage="postgres", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
            await uow.runs.create(run(status=RunStatus.COMPLETED))
            assert isinstance(uow, PostgresUnitOfWork)
            await uow.session.execute(
                insert(EventRow),
                [
                    event_values(
                        NewEvent(
                            session_id=session().id,
                            run_id=None,
                            event_type="memory.formed",
                            actor_type="memory",
                            payload={
                                "record_id": str(target) if index == 1000 else str(uuid4()),
                                "statement": "Synthetic generated statement " * 100,
                            },
                        ),
                        sequence=index + 1,
                        created_at=NOW,
                    )
                    for index in range(1001)
                ],
            )
        event.listen(EventRow, "load", loaded)
        try:
            async with app.uow_factory() as uow:
                result = await uow.session_deletions.erase_people_copies(owner, [target], NOW)
        finally:
            event.remove(EventRow, "load", loaded)
        assert result.counts["events"] == 1
        assert loaded_payloads <= 10, (
            "discovery must use indexed opaque references before payload reads"
        )
        async with app.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork)
            indexes = set(
                (await uow.session.scalars(text("SELECT indexname FROM pg_indexes"))).all()
            )
            assert {
                "ix_events_people_references",
                "ix_invocations_people_references",
                "ix_traces_people_references",
            } <= indexes


@pytest.mark.parametrize("run_linked", [False, True])
async def test_postgres_forget_resumes_generated_payload_batches(run_linked: bool) -> None:
    from agent_core.adapters.persistence.sqlalchemy_models import EventRow
    from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
    from agent_core.application.people import PublicPeopleService
    from agent_core.application.people_erasure import PeopleErasureService
    from agent_core.bootstrap import build
    from agent_core.domain.events import NewEvent
    from agent_core.domain.memory import Sensitivity
    from agent_core.domain.messages import AssistantMessage, TextPart
    from agent_core.domain.people_views import CreatePerson, PeopleForgetRequest
    from agent_core.domain.runs import RunStatus
    from tests.contract.support import run, session

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(settings=database_settings(), storage="postgres", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
            await uow.runs.create(run(status=RunStatus.COMPLETED))
        person = await PublicPeopleService(app.uow_factory, app.clock).create(
            owner,
            CreatePerson(session_id=session().id, display_name="Sam"),
            key="copy-batches-person",
            ceiling=Sensitivity.SENSITIVE,
        )
        async with app.uow_factory() as uow:
            for _ in range(600):
                await uow.events.append(
                    NewEvent(
                        session_id=session().id,
                        run_id=run().id if run_linked else None,
                        event_type="assistant.message.completed",
                        actor_type="agent",
                        payload={
                            "person_id": str(person.id),
                            "message": AssistantMessage(
                                content=[TextPart(text="Sam likes chess")]
                            ).model_dump(mode="json"),
                        },
                    )
                )
            await uow.history.catch_up(session().id)
        service = PeopleErasureService(app.uow_factory, app.clock)
        preview = await service.forget(
            owner,
            person.id,
            PeopleForgetRequest(session_id=session().id, phase="preview", expected_revision=1),
            key="copy-batches-preview",
            ceiling=Sensitivity.SENSITIVE,
        )
        receipt = await service.forget(
            owner,
            person.id,
            PeopleForgetRequest(
                session_id=session().id,
                phase="apply",
                expected_revision=preview.revision,
                operation_id=preview.id,
            ),
            key="copy-batches-apply",
            ceiling=Sensitivity.SENSITIVE,
        )
        async with app.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork)
            raw = list(
                (
                    await uow.session.scalars(
                        select(EventRow.payload).where(
                            EventRow.event_type == "assistant.message.completed"
                        )
                    )
                ).all()
            )
            remaining = sum("Sam likes chess" in str(item) for item in raw)
            assert remaining >= 344, "the apply transaction must redact at most 256 event payloads"
            assert receipt.state == "cleanup_pending"
            visible = await uow.events.list_after(session().id, 0, owner)
            assert all("Sam likes chess" not in str(item.payload) for item in visible)
            history = await uow.history.read(session().id)
            assert "Sam likes chess" not in history.model_dump_json()
        restarted = PeopleErasureService(app.uow_factory, app.clock)
        finished = await restarted.get(owner, receipt.id, ceiling=Sensitivity.SENSITIVE)
        assert finished.state == "completed"
        async with app.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork)
            raw = list(
                (
                    await uow.session.scalars(
                        select(EventRow.payload).where(
                            EventRow.event_type == "assistant.message.completed"
                        )
                    )
                ).all()
            )
            assert all("[People memory erased]" in str(item) for item in raw)


async def test_postgres_forget_batches_knowledge_and_trace_payloads() -> None:
    from sqlalchemy import func

    from agent_core.adapters.persistence.sqlalchemy_models import KnowledgeChunkRow, RecallTraceRow
    from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
    from agent_core.bootstrap import build
    from agent_core.domain.errors import NotFoundError
    from agent_core.domain.knowledge import KnowledgeQuery
    from agent_core.domain.runs import RunStatus
    from tests.contract.memory_fixtures import prepared_knowledge, trace
    from tests.contract.support import run, session

    owner = principal()
    target = uuid4()
    prepared = prepared_knowledge()
    chunks = [
        prepared.chunks[0].model_copy(
            update={
                "chunk_id": f"kc_{i:016x}",
                "ordinal": i,
            }
        )
        for i in range(600)
    ]
    prepared = prepared.model_copy(update={"chunks": chunks})
    references = [
        trace().model_copy(update={"id": uuid4(), "run_id": run().id}) for _ in range(600)
    ]
    references = [
        value.model_copy(
            update={
                "query": value.query.model_copy(update={"people_scope": (target,)}),
            }
        )
        for value in references
    ]
    async with build(settings=database_settings(), storage="postgres", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
            await uow.runs.create(run(status=RunStatus.COMPLETED))
            await uow.artifacts.create(prepared.document.source_ref)
            await uow.knowledge.ingest(prepared)
            for value in references:
                await uow.traces.record(value)
        async with app.uow_factory() as uow:
            result = await uow.session_deletions.erase_people_copies(owner, [target], NOW)
        async with app.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork)
            assert (
                (await uow.session.scalar(select(func.count()).select_from(KnowledgeChunkRow))) or 0
            ) >= 344
            assert (
                (await uow.session.scalar(select(func.count()).select_from(RecallTraceRow))) or 0
            ) >= 344
            assert result.pending_generated
            assert (
                await uow.knowledge.latest(owner.tenant_id, prepared.document.document_id) is None
            )
            assert await uow.knowledge.get_chunk(chunks[-1].chunk_id) is None
            assert not await uow.knowledge.search(
                KnowledgeQuery(
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    current_scope=None,
                    text="restart service",
                    budget_tokens=500,
                    max_passages=5,
                    min_score=0.1,
                    as_of=NOW,
                )
            )
            with pytest.raises(NotFoundError):
                await uow.traces.get(references[-1].id, owner)
            assert references[0].turn_id is not None
            assert not await uow.traces.for_turn(references[0].turn_id)
            # Byte cleanup waits until its dependent knowledge rows are gone.
            assert not await uow.artifacts.list_expired(NOW, limit=100)
        for _ in range(5):
            async with app.uow_factory() as uow:
                result = await uow.session_deletions.erase_people_copies(owner, [], NOW)
            if not result.pending_generated:
                break
        assert not result.pending_generated
        async with app.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork)
            assert not await uow.session.scalar(select(func.count()).select_from(KnowledgeChunkRow))
            assert not await uow.session.scalar(select(func.count()).select_from(RecallTraceRow))
            assert await uow.artifacts.delete_expired(prepared.document.source_ref.id, now=NOW)


async def test_postgres_erasure_follows_generated_document_context() -> None:
    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.unit.test_people_erasure import derived_knowledge_erasure_contract

    owner = principal()
    async with build(settings=database_settings(), storage="postgres", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await derived_knowledge_erasure_contract(app.uow_factory, app.clock, owner)


async def test_postgres_forget_batches_run_messages_and_checkpoints() -> None:
    from sqlalchemy import func

    from agent_core.adapters.persistence.sqlalchemy_models import CheckpointRow, RunRow
    from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
    from agent_core.bootstrap import build
    from agent_core.domain.runs import RunCheckpoint, RunStatus
    from tests.contract.support import run, session

    owner = principal()
    completed = [
        run(status=RunStatus.COMPLETED).model_copy(
            update={
                "id": uuid4(),
                "final_message": "Sam likes chess",
            }
        )
        for _ in range(257)
    ]
    async with build(settings=database_settings(), storage="postgres", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
            for value in completed:
                await uow.runs.create(value)
                await uow.checkpoints.write(
                    value.id,
                    RunCheckpoint(
                        run_id=value.id,
                        version=1,
                        status=RunStatus.COMPLETED,
                        compacted_summary="Sam likes chess",
                        created_at=NOW,
                    ),
                    full=True,
                )
        async with app.uow_factory() as uow:
            result = await uow.session_deletions.erase_people_copies(
                owner,
                [],
                NOW,
                run_ids=[value.id for value in completed],
            )
        async with app.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork)
            assert (
                await uow.session.scalar(
                    select(func.count())
                    .select_from(RunRow)
                    .where(RunRow.final_message == "Sam likes chess")
                )
                == 1
            )
            assert await uow.session.scalar(select(func.count()).select_from(CheckpointRow)) == 1
            assert result.pending_generated
            for value in completed:
                assert (await uow.runs.get(value.id, owner)).final_message is None
                assert await uow.checkpoints.latest(value.id) is None
        async with app.uow_factory() as uow:
            result = await uow.session_deletions.erase_people_copies(owner, [], NOW)
            assert not result.pending_generated


async def test_postgres_forget_batches_email_and_episode_copies() -> None:
    from sqlalchemy import func

    from agent_core.adapters.persistence.sqlalchemy_models import (
        EmailRecordRow,
        IntegratedEpisodeRow,
    )
    from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
    from agent_core.application.people import PublicPeopleService
    from agent_core.application.people_erasure import PeopleErasureService
    from agent_core.bootstrap import build
    from agent_core.domain.email import EmailRecord
    from agent_core.domain.memory import Sensitivity
    from agent_core.domain.people import PersonMemoryLink
    from agent_core.domain.people_views import CreatePerson, PeopleForgetRequest
    from tests.contract.memory_fixtures import memory
    from tests.contract.support import session
    from tests.contract.test_integrated_episode_store_contract import integrated_episode

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    belief = memory()
    async with build(settings=database_settings(), storage="postgres", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        person = await PublicPeopleService(app.uow_factory, app.clock).create(
            owner,
            CreatePerson(session_id=session().id, display_name="Sam"),
            key="email-batches-person",
            ceiling=Sensitivity.SENSITIVE,
        )
        async with app.uow_factory() as uow:
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
            for index in range(257):
                await uow.episodes.put(
                    integrated_episode(episode_id=20000 + index).model_copy(
                        update={
                            "tenant_id": owner.tenant_id,
                            "principal_id": owner.principal_id,
                            "derivation_key": f"{index:064x}",
                        }
                    )
                )
                key = f"thread-{index:04}"
                for kind in ("thread", "semantic_source", "assessment"):
                    await uow.email.put(
                        EmailRecord(
                            tenant_id=owner.tenant_id,
                            principal_id=owner.principal_id,
                            kind=kind,
                            key=key,
                            revision=1,
                            created_at=NOW,
                            updated_at=NOW,
                            payload={
                                "account_id": "work",
                                "provider_thread_id": key,
                                "memory_ids": [str(belief.id)],
                                "facts": {"chess": "Sam likes chess"},
                                "summary": "Sam likes chess",
                                "messages": [{"body": "Original mail"}],
                            },
                        ),
                        expected_revision=0,
                    )
        service = PeopleErasureService(app.uow_factory, app.clock)
        preview = await service.forget(
            owner,
            person.id,
            PeopleForgetRequest(
                session_id=session().id,
                phase="preview",
                expected_revision=1,
            ),
            key="email-batches-preview",
            ceiling=Sensitivity.SENSITIVE,
        )
        receipt = await service.forget(
            owner,
            person.id,
            PeopleForgetRequest(
                session_id=session().id,
                phase="apply",
                expected_revision=preview.revision,
                operation_id=preview.id,
            ),
            key="email-batches-apply",
            ceiling=Sensitivity.SENSITIVE,
        )
        async with app.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork)
            remaining = await uow.session.scalar(
                select(func.count())
                .select_from(EmailRecordRow)
                .where(
                    EmailRecordRow.kind == "semantic_source",
                    EmailRecordRow.payload["facts"] != {},
                )
            )
            assert remaining, "email payload deletion must leave a resumable bounded page"
            assert (
                await uow.session.scalar(select(func.count()).select_from(IntegratedEpisodeRow))
                == 1
            )
            assert not await uow.episodes.for_session(session().id, owner)
            assert receipt.state == "cleanup_pending"
            source = await uow.email.get(owner, "semantic_source", "thread-0256")
            assert (
                source is not None and source.payload["facts"] == {} and source.payload["excluded"]
            )
            thread = await uow.email.get(owner, "thread", "thread-0256")
            assert (
                thread is not None
                and thread.payload["summary"] == "Review the original conversation."
            )
            assert thread.payload["messages"] == [{"body": "Original mail"}]
            assert await uow.email.get(owner, "assessment", "thread-0256") is None
            assert not await uow.email.list(owner, "assessment")
        restarted = PeopleErasureService(app.uow_factory, app.clock)
        assert (
            await restarted.get(owner, receipt.id, ceiling=Sensitivity.SENSITIVE)
        ).state == "completed"
        async with app.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork)
            assert not await uow.session.scalar(
                select(func.count())
                .select_from(EmailRecordRow)
                .where(
                    EmailRecordRow.kind == "semantic_source",
                    EmailRecordRow.payload["facts"] != {},
                )
            )


async def test_postgres_forget_invalidates_frozen_memory_snapshots() -> None:
    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.unit.test_people_erasure import frozen_snapshot_erasure_contract

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(settings=database_settings(), storage="postgres", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await frozen_snapshot_erasure_contract(app.uow_factory, app.clock, owner)


@pytest.mark.parametrize("source_kind", ["email", "session"])
async def test_postgres_source_erasure_removes_cross_session_copies(source_kind: str) -> None:
    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.unit.test_people_erasure import source_copy_erasure_contract

    owner = principal()
    async with build(settings=database_settings(), storage="postgres", principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await source_copy_erasure_contract(app.uow_factory, app.clock, owner, source_kind)


@pytest.mark.parametrize("operation", ["people-remove", "memory-delete"])
async def test_postgres_remove_one_fact_erases_its_copied_influence(operation: str) -> None:
    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.unit.test_people_erasure import frozen_snapshot_erasure_contract

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    async with build(
        settings=database_settings(), storage="postgres", principal=owner, fixed_clock_at=NOW
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await frozen_snapshot_erasure_contract(
            app.uow_factory, app.clock, owner, operation=operation
        )


@pytest.mark.parametrize("operation", ["correct", "changed"])
@pytest.mark.parametrize("dated", [False, True])
async def test_postgres_temporal_owner_correction_matrix(
    operation: Literal["correct", "changed"], dated: bool
) -> None:
    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.unit.test_people_service import people_correction_temporal_contract

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    clock = FixedClock(NOW)
    async with build(
        settings=database_settings(), storage="postgres", principal=owner, clock=clock
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        assert operation in {"correct", "changed"}
        await people_correction_temporal_contract(app.uow_factory, clock, owner, operation, dated)


@pytest.mark.parametrize(
    "kind,operation,dated",
    [
        (kind, operation, dated)
        for kind in ("relationship", "commitment")
        for operation, dated in (("correct", False), ("changed", False), ("changed", True))
    ]
    + [("fact", "changed", True)],
)
async def test_postgres_temporal_profile_and_projection_matrix(
    kind: str,
    operation: Literal["correct", "changed"],
    dated: bool,
) -> None:
    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.unit.test_people_service import (
        people_history_temporal_contract,
        people_projection_temporal_contract,
    )

    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    clock = FixedClock(NOW)
    async with build(
        settings=database_settings(), storage="postgres", principal=owner, clock=clock
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        if kind == "fact":
            await people_history_temporal_contract(app.uow_factory, clock, owner)
        else:
            await people_projection_temporal_contract(
                app.uow_factory, clock, owner, kind, operation, dated
            )


async def test_postgres_recall_waits_for_initial_erasure_without_blocking_another_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.bootstrap import build
    from tests.contract.support import session
    from tests.unit.test_people_erasure import initial_erasure_contention_contract

    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        fixed_clock_at=NOW,
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await initial_erasure_contention_contract(app.uow_factory, app.clock, monkeypatch)
