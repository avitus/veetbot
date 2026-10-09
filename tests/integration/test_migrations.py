"""Migration acceptance checks against a disposable or CI PostgreSQL database."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.revision import EXPECTED_REVISION
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleMergeSuggestion, Person, PersonIdentifier
from agent_core.domain.people_views import ResolveMergeSuggestion
from tests.contract.support import NOW, principal, session
from tests.integration.m2_support import database_settings

ROOT = Path(__file__).resolve().parents[2]
# ADR-0125's migration. Later migrations follow it, so its downgrade tests
# name the revision before it rather than stepping back one from head.
BEFORE_MERGE_SUGGESTIONS = "524f16dfc8f9-1"


def _alembic(*arguments: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def test_migrations_upgrade_cleanly_and_match_metadata() -> None:
    try:
        _alembic("downgrade", "base")
        _alembic("upgrade", "head")
        assert "No new upgrade operations detected" in _alembic("check")
        assert EXPECTED_REVISION in _alembic("current")
    finally:
        _alembic("upgrade", "head")


def test_migrations_round_trip_each_step_from_its_predecessor() -> None:
    _alembic("upgrade", "head")
    assert EXPECTED_REVISION in _alembic("current")
    _alembic("downgrade", "-1")
    try:
        _alembic("upgrade", "+1")
        _alembic("downgrade", "-1")
    finally:
        _alembic("upgrade", "head")
    assert EXPECTED_REVISION in _alembic("current")


async def test_lease_expiration_backfill_preserves_continuations_and_crash_history() -> None:
    settings = database_settings()
    async with build(settings=settings, storage="postgres") as composition:
        run_ids = [await composition.runs.submit("synthetic migration case") for _ in range(3)]
        checkpoints = []
        for index, run_id in enumerate(run_ids):
            async with composition.uow_factory() as uow:
                run = await uow.runs.get(run_id, composition.principal)
                checkpoints.append(await uow.checkpoints.latest(run_id))
                for epoch in range(index):
                    await uow.events.append(
                        NewEvent(
                            session_id=run.session_id,
                            run_id=run_id,
                            event_type="run.requeued",
                            actor_type="maintenance",
                            payload={"reclaimed_epoch": epoch + 1, "attempts": epoch + 1},
                        )
                    )
                if index == 2:
                    await uow.events.append(
                        NewEvent(
                            session_id=run.session_id,
                            run_id=run_id,
                            event_type="run.failed",
                            actor_type="maintenance",
                            payload={
                                "reclaimed_epoch": 3,
                                "failure": {"reason": "max_attempts_exceeded"},
                            },
                        )
                    )
        _alembic("downgrade", "a4f7c1e9d2b3")
        engine = create_engine(settings.database_url)
        try:
            async with create_session_factory(engine)() as session:
                # Every legacy row has three total claims, but only the last
                # two have any crash history. Terminal rows stay terminal.
                await session.execute(text("UPDATE runs SET attempts = 3"))
                await session.execute(
                    text("UPDATE runs SET status = 'FAILED' WHERE id = :id"), {"id": run_ids[2]}
                )
                await session.commit()
            _alembic("upgrade", "head")
        finally:
            await engine.dispose()
            _alembic("upgrade", "head")
        for index, run_id in enumerate(run_ids):
            async with composition.uow_factory() as uow:
                run = await uow.runs.get(run_id, composition.principal)
                assert run.attempts == 3
                assert run.lease_expirations == [0, 1, 3][index]
                assert run.status.value == ("FAILED" if index == 2 else "QUEUED")
                assert await uow.checkpoints.latest(run_id) == checkpoints[index]
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            first = await uow.queue.claim("recovered-continuation", [0])
            second = await uow.queue.claim("recovered-crash", [0])
            assert first is not None and first.run.id == run_ids[0]
            assert second is not None and second.run.id == run_ids[1]
            assert await uow.queue.claim("no-terminal-reopen", [0]) is None


def _people_owner() -> Principal:
    return principal().model_copy(
        update={
            "principal_id": f"downgrade-{uuid4().hex[:12]}",
            "scopes": {"people.read", "people.write"},
        }
    )


async def _two_sabinas(app: Any, owner: Principal) -> UUID:
    """Seed two correspondents the duplicate pass asks about; return an audit session."""
    audit = uuid4()
    fields: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW - timedelta(days=10),
        "updated_at": NOW - timedelta(days=10),
    }
    async with app.uow_factory() as uow:
        await uow.sessions.create(
            session().model_copy(update={"id": audit, "principal_id": owner.principal_id})
        )
        for address in ("sabina@home.test", "sabina@work.test"):
            person = Person(id=uuid4(), display_name="Sabina Smith", **fields)
            await uow.people.put(person, expected_revision=0)
            await uow.people.put(
                PersonIdentifier(
                    id=uuid4(),
                    person_id=person.id,
                    identifier_kind="email",
                    namespace="owner",
                    value=address,
                    context="owner",
                    verification="channel_observed",
                    valid_from=NOW - timedelta(days=10),
                    **fields,
                ),
                expected_revision=0,
            )
    return audit


async def test_merge_suggestion_downgrade_never_erases_an_owner_answer() -> None:
    """ADR-0125: a downgrade drops derived suggestions but refuses to lose an answer."""
    owner = _people_owner()
    settings = replace(database_settings(), people_enabled=True)
    try:
        async with build(settings=settings, storage="postgres", principal=owner) as app:
            service = app.services.people
            assert service is not None
            audit = await _two_sabinas(app, owner)

            async def listed() -> list[Any]:
                return list(
                    (await service.merge_suggestions(owner, ceiling=Sensitivity.SENSITIVE)).items
                )

            assert len((await service.dedupe(owner, apply=True)).suggestions) == 1
            # An open suggestion is derived: it goes, and the next pass asks again.
            _alembic("downgrade", BEFORE_MERGE_SUGGESTIONS)
            _alembic("upgrade", "head")
            assert await listed() == []
            assert len((await service.dedupe(owner, apply=True)).suggestions) == 1
            [asked] = await listed()
            await service.resolve_merge_suggestion(
                owner,
                asked.id,
                ResolveMergeSuggestion(
                    session_id=audit, expected_revision=asked.revision, decision="separate"
                ),
                key="keep-apart",
                ceiling=Sensitivity.SENSITIVE,
            )
            # The owner's answer is final, so the downgrade refuses to erase it.
            with pytest.raises(subprocess.CalledProcessError) as refused:
                _alembic("downgrade", BEFORE_MERGE_SUGGESTIONS)
            assert "answered" in refused.value.stderr
            assert EXPECTED_REVISION in _alembic("current")
            assert "No new upgrade operations detected" in _alembic("check")
            assert (await service.dedupe(owner, apply=True)).suggestions == []
    finally:
        _alembic("upgrade", "head")


async def _until_the_downgrade_waits_for_people_write(engine: Any, downgrade: Any) -> None:
    # M32's head-column downgrade takes the memory-owner table first, matching
    # People writers' owner-before-People ordering. Either lock serializes the
    # same in-flight answer; the test still requires it to survive downgrade.
    for _ in range(300):
        async with engine.connect() as connection:
            waiting = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE relation IN ('people_heads'::regclass, "
                        "'reconsolidation_owners'::regclass) "
                        "AND mode = 'AccessExclusiveLock' AND NOT granted"
                    )
                )
            ).scalar_one()
        if waiting:
            return
        if downgrade.returncode is not None:
            _, errors = await downgrade.communicate()
            raise AssertionError("the downgrade ended before it waited: " + errors.decode())
        await asyncio.sleep(0.1)
    raise AssertionError("the downgrade never waited for the answer in flight")


async def test_merge_suggestion_downgrade_waits_for_an_answer_in_flight() -> None:
    """An answer that commits while the downgrade runs is never lost (ADR-0125)."""
    owner = _people_owner()
    settings = replace(database_settings(), people_enabled=True)
    engine = create_engine(settings.database_url)
    try:
        async with build(settings=settings, storage="postgres", principal=owner) as app:
            service = app.services.people
            assert service is not None
            await _two_sabinas(app, owner)
            assert len((await service.dedupe(owner, apply=True)).suggestions) == 1
            [asked] = (await service.merge_suggestions(owner, ceiling=Sensitivity.SENSITIVE)).items
            async with app.uow_factory() as uow:
                current = await uow.people.get(owner, asked.id, ceiling=Sensitivity.RESTRICTED)
                assert isinstance(current, PeopleMergeSuggestion)
                await uow.people.put(
                    current.model_copy(
                        update={
                            "state": "separated",
                            "revision": current.revision + 1,
                            "updated_at": current.updated_at + timedelta(seconds=1),
                        }
                    ),
                    expected_revision=current.revision,
                )
                downgrade = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "alembic",
                    "downgrade",
                    BEFORE_MERGE_SUGGESTIONS,
                    cwd=ROOT,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await _until_the_downgrade_waits_for_people_write(engine, downgrade)
            # The answer commits here, while the downgrade waits on it.
            _output, errors = await downgrade.communicate()
            assert downgrade.returncode != 0, "the downgrade erased an answer in flight"
            assert b"answered" in errors
            async with app.uow_factory() as uow:
                kept = await uow.people.get(owner, asked.id, ceiling=Sensitivity.RESTRICTED)
            assert isinstance(kept, PeopleMergeSuggestion) and kept.state == "separated"
    finally:
        _alembic("upgrade", "head")
        await engine.dispose()


async def test_activity_backfill_removes_only_automatic_memory_timestamps() -> None:
    """Repair old audit bumps while preserving later conversation and close times."""
    from agent_core.adapters.determinism import FixedClock
    from agent_core.domain.sessions import SessionStatus

    clock = FixedClock(NOW)
    settings = database_settings()
    ids = [UUID(int=810 + index) for index in range(5)]
    async with build(
        settings=settings, storage="postgres", principal=principal(), clock=clock
    ) as app:
        async with app.uow_factory() as uow:
            for index, sid in enumerate(ids):
                await uow.sessions.create(session().model_copy(update={"id": sid}))
                if index != 4:
                    await uow.events.append(
                        NewEvent(
                            session_id=sid,
                            run_id=None,
                            event_type="user.message.created",
                            actor_type="principal",
                            payload={"content": "Original conversation"},
                        )
                    )
            clock.advance(timedelta(days=30))
            for sid in ids:
                await uow.events.append(
                    NewEvent(
                        session_id=sid,
                        run_id=None,
                        event_type="memory.decayed",
                        actor_type="memory",
                    )
                )
            clock.advance(timedelta(days=1))
            await uow.events.append(
                NewEvent(
                    session_id=ids[1],
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    payload={"content": "Later conversation"},
                )
            )
            await uow.sessions.close(ids[2], principal(), clock.now())
        _alembic("downgrade", "e6b3d1a9c470")
        engine = create_engine(settings.database_url)
        try:
            async with create_session_factory(engine)() as db:
                # Reproduce the former adapter, including a newer non-event write.
                for index in (0, 3, 4):
                    await db.execute(
                        text("UPDATE sessions SET updated_at = :at WHERE id = :id"),
                        {"id": ids[index], "at": NOW + timedelta(days=30 if index != 3 else 32)},
                    )
                await db.commit()
            _alembic("upgrade", "head")
        finally:
            await engine.dispose()
            _alembic("upgrade", "head")
        async with app.uow_factory() as uow:
            records = [await uow.sessions.get(sid, principal()) for sid in ids]
            assert [record.updated_at for record in records] == [
                NOW,
                NOW + timedelta(days=31),
                NOW + timedelta(days=31),
                NOW + timedelta(days=32),
                NOW,
            ]
            assert records[2].status is SessionStatus.CLOSED
            assert len(await uow.events.list_after(ids[0], 0, principal())) == 2


async def test_reconsolidation_backfill_preserves_originals_and_history() -> None:
    from agent_core.adapters.determinism import FixedClock
    from agent_core.adapters.persistence.memory_repositories import PostgresMemoryStore
    from agent_core.adapters.persistence.repositories import PostgresSessionRepository
    from tests.contract.memory_fixtures import memory

    engine = create_engine(database_settings().database_url)
    sessions = create_session_factory(engine)
    originals = [
        memory(belief_id=700 + i).model_copy(
            update={"created_at": NOW - timedelta(days=i), "store_position": i + 1}
        )
        for i in range(3)
    ]
    try:
        async with sessions() as connection, connection.begin():
            await connection.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": principal().tenant_id},
            )
            await PostgresSessionRepository(connection).create(session())
            store = PostgresMemoryStore(connection, FixedClock(NOW))
            for record in originals:
                await store.upsert_belief(record)
            before = (
                (await connection.execute(text("SELECT payload FROM memory_revisions ORDER BY id")))
                .scalars()
                .all()
            )
        _alembic("downgrade", "f7c4a2d9e681")
        _alembic("upgrade", "head")
        async with sessions() as connection:
            await connection.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": principal().tenant_id},
            )
            store = PostgresMemoryStore(connection, FixedClock(NOW))
            assert [await store.get(record.id, principal()) for record in originals] == originals
            after = (
                (await connection.execute(text("SELECT payload FROM memory_revisions ORDER BY id")))
                .scalars()
                .all()
            )
            assert before == after
            ordered = (
                await connection.execute(
                    text(
                        "SELECT id, creation_sequence, content_revision FROM memories "
                        "ORDER BY creation_sequence"
                    )
                )
            ).all()
            assert [tuple(row) for row in ordered] == [
                (originals[2].id, 1, 1),
                (originals[1].id, 2, 1),
                (originals[0].id, 3, 1),
            ]
            assert (
                await connection.execute(text("SELECT count(*) FROM reconsolidation_changes"))
            ).scalar_one() == 3
        assert "No new upgrade operations detected" in _alembic("check")
    finally:
        _alembic("upgrade", "head")
        await engine.dispose()


async def test_attribution_backfill_preserves_evidence_and_unknown_purged_heads() -> None:
    from agent_core.adapters.determinism import FixedClock
    from agent_core.adapters.persistence.memory_repositories import PostgresMemoryStore
    from agent_core.adapters.persistence.people import PostgresPeopleStore
    from agent_core.adapters.persistence.repositories import PostgresSessionRepository
    from tests.contract.memory_fixtures import memory
    from tests.contract.reconsolidation_attribution_cases import link, person, source

    engine = create_engine(database_settings().database_url)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as connection, connection.begin():
            await connection.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": principal().tenant_id},
            )
            await PostgresSessionRepository(connection).create(session())
            await PostgresMemoryStore(connection, FixedClock(NOW)).upsert_belief(memory())
            people = PostgresPeopleStore(connection, FixedClock(NOW))
            for record in (source(), person(), link(), person(899)):
                await people.put(record, expected_revision=0)
            await people.erase(principal(), [person(899).id])
            before = (
                await connection.execute(
                    text(
                        "SELECT payload, content_revision, creation_sequence "
                        "FROM memory_revisions ORDER BY id"
                    )
                )
            ).all()
        _alembic("downgrade", "ea3210a1b004")
        _alembic("upgrade", "head")
        async with sessions() as connection:
            await connection.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": principal().tenant_id},
            )
            rows = dict(
                (await connection.execute(text("SELECT id, memory_attribution FROM people_heads")))
                .tuples()
                .all()
            )
            assert rows == {
                source().id: {
                    "source_id": str(source().id),
                    "session_id": str(source().session_id),
                    "event_sequence": 1,
                },
                person().id: {},
                link().id: {"belief_id": str(memory().id)},
                person(899).id: None,
            }, "backfill must retain only opaque keys and must not guess purged attribution"
            after = (
                await connection.execute(
                    text(
                        "SELECT payload, content_revision, creation_sequence "
                        "FROM memory_revisions ORDER BY id"
                    )
                )
            ).all()
            assert after == before
            people = PostgresPeopleStore(connection, FixedClock(NOW))
            assert (
                await people.memory_attribution_records(principal(), memory().id, (source().id,))
                is None
            )
            other = principal().model_copy(update={"principal_id": "unaffected-owner"})
            assert await people.memory_attribution_records(other, memory().id, (source().id,)) == ()
            assert (
                await PostgresMemoryStore(connection, FixedClock(NOW)).get(memory().id, principal())
                == memory()
            )
    finally:
        _alembic("upgrade", "head")
        await engine.dispose()


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_attribution_migration_refuses_an_rls_filtered_role(
    direction: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import DBAPIError

    from agent_core.adapters.determinism import FixedClock, RandomIdFactory
    from agent_core.adapters.persistence.people import PostgresPeopleStore
    from agent_core.adapters.persistence.reconsolidation import PostgresReconsolidationStore
    from agent_core.adapters.persistence.sqlalchemy_models import Base
    from tests.contract.reconsolidation_attribution_cases import person

    migration = import_module("migrations.versions.ea3210a1b005_retain_people_attribution_keys")
    engine = create_engine(database_settings().database_url)
    try:
        async with create_session_factory(engine)() as database_session:
            await database_session.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": principal().tenant_id},
            )
            # All role, ownership and DDL changes roll back on connection exit.
            await PostgresPeopleStore(database_session, FixedClock(NOW)).put(
                person(), expected_revision=0
            )
            await PostgresReconsolidationStore(database_session, RandomIdFactory()).claim_due(
                principal(), NOW, "hidden-lease"
            )
            connection = await database_session.connection()
            if direction == "upgrade":
                await connection.execute(
                    text("ALTER TABLE people_heads DROP COLUMN memory_attribution")
                )
            await connection.execute(
                text("CREATE ROLE m32_attribution_migrator NOSUPERUSER NOBYPASSRLS")
            )
            await connection.execute(
                text("GRANT USAGE, CREATE ON SCHEMA public TO m32_attribution_migrator")
            )
            await connection.execute(
                text(
                    "GRANT ALL ON people_heads, people_revisions, "
                    "reconsolidation_owners, reconsolidation_jobs "
                    "TO m32_attribution_migrator"
                )
            )
            await connection.execute(
                text("ALTER TABLE people_heads OWNER TO m32_attribution_migrator")
            )
            await connection.execute(text("SET LOCAL ROLE m32_attribution_migrator"))

            def apply(sync: Any) -> None:
                context = MigrationContext.configure(sync, opts={"target_metadata": Base.metadata})
                monkeypatch.setattr(migration, "op", Operations(context))
                getattr(migration, direction)()

            with pytest.raises(DBAPIError, match="row-level security"):
                await connection.run_sync(apply)
    finally:
        await engine.dispose()


_ORIGINAL_RECONSOLIDATION_DOWNGRADE = """
import asyncio
from importlib import import_module
from alembic.migration import MigrationContext
from alembic.operations import Operations
from agent_core.adapters.persistence.database import create_engine
from tests.integration.m2_support import database_settings

async def main():
    engine = create_engine(database_settings().database_url)
    try:
        async with engine.begin() as connection:
            def apply(sync):
                migration = import_module(
                    "migrations.versions.ea3210a1b001_add_reconsolidation_maintenance"
                )
                migration.op = Operations(MigrationContext.configure(sync))
                migration.downgrade()
            await connection.run_sync(apply)
    finally:
        await engine.dispose()

asyncio.run(main())
"""


@pytest.mark.parametrize(
    ("original_guard", "refusal"),
    [
        (True, "export its history"),
        (False, "preserve hypothesis/conflict history"),
    ],
)
async def test_reconsolidation_downgrade_preserves_a_job_admitted_in_flight(
    original_guard: bool, refusal: str
) -> None:
    from agent_core.adapters.determinism import RandomIdFactory
    from agent_core.adapters.persistence.reconsolidation import PostgresReconsolidationStore

    engine = create_engine(database_settings().database_url)
    sessions = create_session_factory(engine)
    downgrade = None
    try:
        async with asyncio.timeout(20):
            async with sessions() as connection, connection.begin():
                await connection.execute(
                    text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                    {"tenant": principal().tenant_id},
                )
                job = await PostgresReconsolidationStore(connection, RandomIdFactory()).claim_due(
                    principal(), NOW, "in-flight"
                )
                assert job is not None
                arguments = ["-m", "alembic", "downgrade", "f7c4a2d9e681"]
                if original_guard:
                    # Later guards intercept the original migration in a full
                    # downgrade. Exercise its locking/refusal directly against
                    # the current schema, where today's writer can admit a job.
                    arguments = ["-c", _ORIGINAL_RECONSOLIDATION_DOWNGRADE]
                downgrade = await asyncio.create_subprocess_exec(
                    sys.executable,
                    *arguments,
                    cwd=ROOT,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                for _ in range(200):
                    async with engine.connect() as observer:
                        waiting = (
                            await observer.execute(
                                text(
                                    "SELECT count(*) FROM pg_locks WHERE NOT granted "
                                    "AND relation IN ('reconsolidation_jobs'::regclass, "
                                    "'reconsolidation_owners'::regclass)"
                                )
                            )
                        ).scalar_one()
                    if waiting:
                        break
                    assert downgrade.returncode is None
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("downgrade never serialized against admission")
            _, errors = await downgrade.communicate()
            assert downgrade.returncode != 0, "downgrade lost a newly committed job"
            assert refusal in errors.decode()
        assert EXPECTED_REVISION in _alembic("current")
        async with sessions() as connection:
            await connection.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": principal().tenant_id},
            )
            assert (
                await connection.execute(text("SELECT id FROM reconsolidation_jobs"))
            ).scalar_one() == job.id
        assert "No new upgrade operations detected" in _alembic("check")
    finally:
        if downgrade is not None and downgrade.returncode is None:
            downgrade.kill()
            await downgrade.communicate()
        _alembic("upgrade", "head")
        await engine.dispose()
