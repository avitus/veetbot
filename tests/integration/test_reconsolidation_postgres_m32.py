"""The M32 shared contracts cross real transactions and migrations."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

import pytest
from sqlalchemy import text

from agent_core.adapters.determinism import FixedClock, RandomIdFactory
from agent_core.adapters.memory.reconsolidation_evidence import ReconsolidationEvidence
from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.dreaming import PostgresDreamingSchedule
from agent_core.adapters.persistence.memory_repositories import (
    PostgresMemoryStore,
    PostgresTraceStore,
)
from agent_core.adapters.persistence.people import PostgresPeopleStore
from agent_core.adapters.persistence.reconsolidation import PostgresReconsolidationStore
from agent_core.adapters.persistence.repositories import (
    PostgresEventRepository,
    PostgresRunRepository,
    PostgresSessionRepository,
)
from agent_core.adapters.persistence.upcasters import EventUpcasterRegistry
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.reconsolidation import ReconsolidationJob
from agent_core.ports.memory import TraceStore
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.derived_memory_cases import DERIVED_SCENARIOS
from tests.contract.dreaming_cases import SCENARIOS as DREAMING_SCENARIOS
from tests.contract.memory_fixtures import memory
from tests.contract.reconsolidation_admission_cases import ADMISSION_SCENARIOS
from tests.contract.reconsolidation_apply_cases import APPLY_SCENARIOS
from tests.contract.reconsolidation_attribution_cases import ATTRIBUTION_SCENARIOS
from tests.contract.reconsolidation_audit_cases import AUDIT_SCENARIOS
from tests.contract.reconsolidation_cases import SCENARIOS, Factory, Stores
from tests.contract.reconsolidation_execution_cases import EXECUTION_SCENARIOS
from tests.contract.reconsolidation_history_cases import HISTORY_SCENARIOS
from tests.contract.reconsolidation_input_cases import INPUT_SCENARIOS
from tests.contract.reconsolidation_operation_cases import OPERATION_SCENARIOS
from tests.contract.reconsolidation_recall_cases import RECALL_SCENARIOS
from tests.contract.reconsolidation_source_cases import MERGE_SCENARIOS
from tests.contract.reconsolidation_summary_cases import SUMMARY_SCENARIOS
from tests.contract.reconsolidation_summary_history_cases import SUMMARY_HISTORY_SCENARIOS
from tests.contract.reconsolidation_summary_recall_cases import SUMMARY_RECALL_SCENARIOS
from tests.contract.reconsolidation_surface_cases import SURFACE_SCENARIOS
from tests.contract.support import NOW, TENANT, principal, run, session
from tests.integration.m2_support import database_settings


@dataclass
class RecallStores(Stores):
    traces: TraceStore


async def test_summary_snapshot_erasure() -> None:
    from agent_core.bootstrap import build
    from tests.contract.reconsolidation_summary_context_cases import summary_snapshot_erasure

    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        clock=FixedClock(NOW),
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await summary_snapshot_erasure(app.uow_factory)


@pytest.mark.parametrize("active", [False, True], ids=["batch_history", "leased_work"])
async def test_group_batch_downgrade_preserves_history(
    postgres_stores: Factory, monkeypatch: pytest.MonkeyPatch, active: bool
) -> None:
    from importlib import import_module
    from typing import Any

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from tests.contract.reconsolidation_apply_cases import reviewed_subsets_commit_together

    if active:
        async with postgres_stores() as uow:
            assert await uow.reconsolidation.claim_due(principal(), NOW, "active") is not None
    else:
        await reviewed_subsets_commit_together(postgres_stores)
    async with postgres_stores() as uow:
        assert isinstance(uow.reconsolidation, PostgresReconsolidationStore)
        connection = uow.reconsolidation._session
        migration = import_module("migrations.versions.ea3210a1b009_allow_group_operation_batches")

        def downgrade(sync: Any) -> None:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sync)))
            migration.downgrade()

        with pytest.raises(RuntimeError, match="group operation history must not be lost"):
            await (await connection.connection()).run_sync(downgrade)
        assert await connection.scalar(text("SELECT count(*) FROM reconsolidation_operations")) == (
            0 if active else 2
        )


@pytest.fixture
def history_clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
async def postgres_stores(history_clock: FixedClock) -> AsyncIterator[Factory]:
    engine = create_engine(database_settings().database_url)
    sessions = create_session_factory(engine)
    clock = history_clock

    @asynccontextmanager
    async def factory() -> AsyncIterator[Stores]:
        async with sessions() as connection, connection.begin():
            await connection.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": TENANT},
            )
            memories = PostgresMemoryStore(connection, clock)
            events = PostgresEventRepository(connection, clock, EventUpcasterRegistry())
            people = PostgresPeopleStore(connection, clock)
            reconsolidation = PostgresReconsolidationStore(
                connection,
                RandomIdFactory(),
                ReconsolidationEvidence(events, people, memories),
                dreaming=PostgresDreamingSchedule(connection, RandomIdFactory()),
            )
            yield RecallStores(
                memories,
                reconsolidation,
                events,
                people,
                PostgresTraceStore(
                    connection, people, reconsolidation=reconsolidation, clock=clock
                ),
            )

    try:
        async with sessions() as connection, connection.begin():
            await connection.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": TENANT},
            )
            await PostgresSessionRepository(connection).create(session())
            await PostgresRunRepository(connection, clock).create(
                run().model_copy(update={"id": UUID(int=991)})
            )
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda case: case.__name__)
async def test_shared_contract(
    postgres_stores: Factory, scenario: Callable[[Factory], Awaitable[None]]
) -> None:
    await scenario(postgres_stores)


async def test_concurrent_claim_and_budget_admission(postgres_stores: Factory) -> None:
    async def claim() -> ReconsolidationJob | None:
        async with postgres_stores() as uow:
            return await uow.reconsolidation.claim_due(principal(), NOW, "worker")

    claims = await asyncio.gather(claim(), claim())
    assert sum(lease is not None for lease in claims) == 1
    lease = next(lease for lease in claims if lease is not None)

    async def reserve(digest: str) -> bool:
        try:
            async with postgres_stores() as uow:
                await uow.reconsolidation.reserve(
                    principal(), lease.lease_token, digest, Decimal("0.15"), NOW
                )
            return True
        except ConflictError:
            return False

    assert sorted(await asyncio.gather(reserve("a" * 64), reserve("b" * 64))) == [False, True]


async def test_source_journal_and_lease_roll_back_together(postgres_stores: Factory) -> None:
    with pytest.raises(RuntimeError, match="abort"):
        async with postgres_stores() as uow:
            await uow.memories.upsert_belief(memory())
            lease = await uow.reconsolidation.claim_due(principal(), NOW, "aborted")
            assert lease is not None and lease.full_bound == lease.change_bound == 1
            raise RuntimeError("abort")
    async with postgres_stores() as uow:
        with pytest.raises(NotFoundError):
            await uow.memories.get(memory().id, principal())
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "committed")
        assert lease is not None and lease.full_bound == lease.change_bound == 0


async def test_scan_bound_waits_for_inflight_source_commit(postgres_stores: Factory) -> None:
    inserted, commit = asyncio.Event(), asyncio.Event()

    async def writer() -> None:
        async with postgres_stores() as uow:
            await uow.memories.upsert_belief(memory())
            inserted.set()
            await commit.wait()

    async def reader() -> ReconsolidationJob | None:
        async with postgres_stores() as uow:
            return await uow.reconsolidation.claim_due(principal(), NOW, "scanner")

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        tasks.create_task(writer())
        await inserted.wait()
        scan = tasks.create_task(reader())
        await asyncio.sleep(0.05)
        assert not scan.done(), "scan bounds must serialize behind uncommitted creations"
        commit.set()
    lease = scan.result()
    assert lease is not None and lease.full_bound == lease.change_bound == 1


@pytest.mark.parametrize(
    "scenario",
    [
        *MERGE_SCENARIOS,
        *ATTRIBUTION_SCENARIOS,
        *OPERATION_SCENARIOS,
        *SUMMARY_SCENARIOS,
        *INPUT_SCENARIOS,
        *ADMISSION_SCENARIOS,
        *EXECUTION_SCENARIOS,
        *APPLY_SCENARIOS,
        *DREAMING_SCENARIOS,
        *AUDIT_SCENARIOS,
    ],
    ids=lambda case: case.__name__,
)
async def test_merge_source_contract(
    postgres_stores: Factory, scenario: Callable[[Factory], Awaitable[None]]
) -> None:
    await scenario(postgres_stores)


async def test_people_lock_waits_for_original_memory_writer(postgres_stores: Factory) -> None:
    inserted, commit = asyncio.Event(), asyncio.Event()

    async def writer() -> None:
        async with postgres_stores() as uow:
            await uow.memories.upsert_belief(memory())
            inserted.set()
            await commit.wait()

    async def people_reader() -> None:
        async with postgres_stores() as uow, uow.people.lock(principal()):
            pass

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        tasks.create_task(writer())
        await inserted.wait()
        reader = tasks.create_task(people_reader())
        try:
            await asyncio.sleep(0.15)
            assert not reader.done(), "People must take the memory owner lock first"
        finally:
            commit.set()


async def test_large_attribution_sets_preserve_exact_event_pairs(postgres_stores: Factory) -> None:
    from uuid import UUID

    from agent_core.adapters.persistence.people_attribution import (
        advance_attribution_revisions,
        footprint_targets,
    )
    from agent_core.domain.reconsolidation_attribution import AttributionFootprint
    from tests.contract.support import SESSION_ID

    async with postgres_stores() as uow:
        for i in range(2):
            await uow.memories.upsert_belief(
                memory(belief_id=501 + i).model_copy(
                    update={"store_position": i + 1, "source_event_ids": [i + 1]}
                )
            )
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "large-attribution")
        assert lease is not None
        before = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
        assert isinstance(uow.people, PostgresPeopleStore)
        connection = uow.people._session
        ids = {UUID(int=100_000 + i) for i in range(33_000)}
        assert await footprint_targets(
            connection, principal(), [AttributionFootprint(source_id=key) for key in ids]
        ) == (set(), set())
        events = {(UUID(int=900), i + 2) for i in range(33_000)} | {(SESSION_ID, 1)}
        await advance_attribution_revisions(connection, principal(), ids, events, NOW)
        after = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
        old, new = ({s.belief_id: s for s in page.sources} for page in (before, after))
        assert new[UUID(int=501)].content_revision == old[UUID(int=501)].content_revision + 1
        assert new[UUID(int=502)] == old[UUID(int=502)], "session/event pairs must not cross-match"


async def test_merge_mutation_invalidates_before_any_operation_read(
    postgres_stores: Factory,
) -> None:
    from sqlalchemy import select

    from agent_core.adapters.persistence.sqlalchemy_models import (
        ReconsolidationMemberRow,
        ReconsolidationOperationHistoryRow,
        ReconsolidationOperationRow,
    )
    from tests.contract.reconsolidation_operation_cases import committed

    _, _, value = await committed(postgres_stores)
    async with postgres_stores() as uow:
        await uow.memories.fence_for_erasure(principal(), [memory().id])
        assert isinstance(uow.reconsolidation, PostgresReconsolidationStore)
        connection = uow.reconsolidation._session
        # Read raw persisted state, before a repository read could repair it.
        assert await connection.scalar(select(ReconsolidationOperationRow.state)) == "invalidated"
        assert (await connection.scalars(select(ReconsolidationMemberRow.active))).all() == [
            False,
            False,
        ]
        assert (
            len(
                (
                    await connection.scalars(select(ReconsolidationOperationHistoryRow.revision))
                ).all()
            )
            == 2
        )
        payload = await connection.scalar(select(ReconsolidationOperationRow.payload))
        assert memory().statement not in str(payload)
        assert payload is not None and payload["store_position"] > value.store_position


async def test_merge_constraints_prevent_foreign_and_overlapping_members(
    postgres_stores: Factory,
) -> None:
    from uuid import UUID

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.exc import IntegrityError

    from agent_core.adapters.persistence.reconsolidation_mappers import group_values
    from agent_core.adapters.persistence.reconsolidation_operations import save_operation
    from agent_core.adapters.persistence.sqlalchemy_models import (
        ReconsolidationGroupRow,
        ReconsolidationMemberRow,
    )
    from tests.contract.reconsolidation_operation_cases import committed

    _, group, value = await committed(postgres_stores)
    async with postgres_stores() as uow:
        assert isinstance(uow.reconsolidation, PostgresReconsolidationStore)
        connection = uow.reconsolidation._session
        with pytest.raises(IntegrityError, match="foreign key"):
            async with connection.begin_nested():
                await connection.execute(
                    pg_insert(ReconsolidationMemberRow).values(
                        tenant_id=TENANT,
                        principal_id="foreign-owner",
                        operation_id=value.id,
                        member_id=UUID(int=9999),
                        canonical_id=value.plan.canonical_id,
                        active=True,
                    )
                )
        with pytest.raises(IntegrityError, match="uq_recon_active_member"):
            async with connection.begin_nested():
                duplicate = group.model_copy(
                    update={"id": UUID(int=9901), "input_digest": "b" * 64}
                )
                await connection.execute(
                    pg_insert(ReconsolidationGroupRow).values(**group_values(duplicate))
                )
                await save_operation(
                    connection,
                    value.model_copy(update={"id": UUID(int=9902), "group_id": duplicate.id}),
                    new=True,
                )
        assert (
            len((await connection.scalars(select(ReconsolidationMemberRow.member_id))).all()) == 2
        )


async def test_merge_rls_hides_every_new_table(postgres_stores: Factory) -> None:
    from tests.contract.reconsolidation_operation_cases import committed

    _, _, value = await committed(postgres_stores)
    async with postgres_stores() as uow:
        await uow.reconsolidation.undo_merge(principal(), value.id, 1, "rls", NOW)
        assert isinstance(uow.reconsolidation, PostgresReconsolidationStore)
        connection = uow.reconsolidation._session
        async with connection.begin_nested() as nested:
            try:
                await connection.execute(
                    text("CREATE ROLE m32_merge_reader NOSUPERUSER NOBYPASSRLS")
                )
                await connection.execute(text("GRANT USAGE ON SCHEMA public TO m32_merge_reader"))
                await connection.execute(
                    text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO m32_merge_reader")
                )
                await connection.execute(text("SET LOCAL ROLE m32_merge_reader"))
                for tenant, visible in ((TENANT, True), ("foreign-tenant", False)):
                    await connection.execute(
                        text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                        {"tenant": tenant},
                    )
                    for suffix in (
                        "operations",
                        "dependencies",
                        "members",
                        "blocks",
                        "undo",
                        "operation_history",
                    ):
                        count = await connection.scalar(
                            text(f"SELECT count(*) FROM reconsolidation_{suffix}")
                        )
                        assert bool(count) is visible, suffix
            finally:
                await nested.rollback()


async def test_merge_downgrade_refuses_completed_history(
    postgres_stores: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from importlib import import_module
    from typing import Any

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import select

    from agent_core.adapters.persistence.sqlalchemy_models import Base, ReconsolidationOperationRow
    from tests.contract.reconsolidation_operation_cases import committed

    job, _, value = await committed(postgres_stores)
    async with postgres_stores() as uow:
        await uow.reconsolidation.release(principal(), job.lease_token, NOW)
    async with postgres_stores() as uow:
        assert isinstance(uow.reconsolidation, PostgresReconsolidationStore)
        session = uow.reconsolidation._session
        connection = await session.connection()
        migration = import_module("migrations.versions.ea3210a1b006_persist_reconsolidation_merges")

        def downgrade(sync: Any) -> None:
            context = MigrationContext.configure(sync, opts={"target_metadata": Base.metadata})
            monkeypatch.setattr(migration, "op", Operations(context))
            migration.downgrade()

        with pytest.raises(RuntimeError, match="undo history would be lost"):
            await connection.run_sync(downgrade)
        assert await session.scalar(select(ReconsolidationOperationRow.id)) == value.id
        assert await uow.memories.get(memory().id, principal()) is not None


@pytest.mark.parametrize(
    "scenario", [*RECALL_SCENARIOS, *SUMMARY_RECALL_SCENARIOS], ids=lambda case: case.__name__
)
async def test_merge_recall(
    postgres_stores: Factory, scenario: Callable[[UnitOfWorkFactory], Awaitable[None]]
) -> None:
    # This fixture supplies the actual transactional repositories used by recall.
    async with asyncio.timeout(30):
        await scenario(cast(UnitOfWorkFactory, postgres_stores))


@pytest.mark.parametrize(
    "scenario", [*HISTORY_SCENARIOS, *SUMMARY_HISTORY_SCENARIOS], ids=lambda case: case.__name__
)
async def test_historical_merge_recall(
    postgres_stores: Factory,
    history_clock: FixedClock,
    scenario: Callable[[UnitOfWorkFactory, FixedClock], Awaitable[None]],
) -> None:
    async with asyncio.timeout(30):
        await scenario(cast(UnitOfWorkFactory, postgres_stores), history_clock)


@pytest.mark.parametrize("mutation", ["correction", "fence", "people"])
async def test_summary_text_is_purged_before_read(postgres_stores: Factory, mutation: str) -> None:
    from sqlalchemy import func, select

    from agent_core.adapters.persistence.sqlalchemy_models import (
        ReconsolidationDependencyRow,
        ReconsolidationOperationHistoryRow,
        ReconsolidationOperationRow,
        ReconsolidationSummaryRow,
    )
    from tests.contract.reconsolidation_attribution_cases import source
    from tests.contract.reconsolidation_summary_cases import committed_summary

    value = await committed_summary(postgres_stores, omitted=True)
    async with postgres_stores() as uow:
        original = await uow.memories.get(memory(belief_id=502).id, principal())
        if mutation == "correction":
            await uow.memories.reinforce(original.model_copy(update={"confidence": 0.1}))
        elif mutation == "fence":
            await uow.memories.fence_for_erasure(principal(), [original.id])
        else:
            async with uow.people.lock(principal()):
                await uow.people.put(
                    source().model_copy(update={"excluded": True}), expected_revision=0
                )
    async with postgres_stores() as uow:
        assert isinstance(uow.reconsolidation, PostgresReconsolidationStore)
        connection = uow.reconsolidation._session
        assert (
            await connection.scalar(select(func.count()).select_from(ReconsolidationSummaryRow))
            == 0
        )
        payload = await connection.scalar(select(ReconsolidationOperationRow.payload))
        assert (
            payload is not None and payload["state"] == "invalidated" and payload["revision"] == 2
        )
        history = (
            await connection.scalars(select(ReconsolidationOperationHistoryRow.payload))
        ).all()
        assert len(history) == 2
        assert all("prefer" not in str(row) and "statement" not in str(row) for row in history)
        assert (
            await connection.scalar(select(func.count()).select_from(ReconsolidationDependencyRow))
            == 2
        )
        assert payload["id"] == str(value.id)


async def test_summary_commit_serializes_with_erasure(postgres_stores: Factory) -> None:
    from tests.contract.reconsolidation_summary_cases import summary_inputs

    async with postgres_stores() as uow:
        job, group, originals, clauses = await summary_inputs(uow)
        prepared = await uow.reconsolidation.plan_summary(
            principal(), job.lease_token, group.id, clauses, NOW
        )
        assert prepared is not None
    fenced, finish = asyncio.Event(), asyncio.Event()

    async def eraser() -> None:
        async with postgres_stores() as uow:
            await uow.memories.fence_for_erasure(principal(), [originals[0].id])
            fenced.set()
            await finish.wait()

    async def writer() -> None:
        async with postgres_stores() as uow:
            with pytest.raises(ConflictError):
                await uow.reconsolidation.commit_summary(
                    principal(), job.lease_token, group.id, prepared, NOW
                )

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        tasks.create_task(eraser())
        await fenced.wait()
        commit = tasks.create_task(writer())
        try:
            await asyncio.sleep(0.1)
            assert not commit.done()
        finally:
            finish.set()


async def test_concurrent_summary_commit_is_unique(postgres_stores: Factory) -> None:
    from tests.contract.reconsolidation_summary_cases import summary_inputs

    async with postgres_stores() as uow:
        job, group, _, clauses = await summary_inputs(uow)
        prepared = await uow.reconsolidation.plan_summary(
            principal(), job.lease_token, group.id, clauses, NOW
        )
        assert prepared is not None

    async def commit() -> bool:
        try:
            async with postgres_stores() as uow:
                await uow.reconsolidation.commit_summary(
                    principal(), job.lease_token, group.id, prepared, NOW
                )
                return True
        except ConflictError:
            return False

    assert sorted(await asyncio.gather(commit(), commit())) == [False, True]


async def test_summary_rls_and_downgrade_preserve_content(
    postgres_stores: Factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib import import_module
    from typing import Any

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from agent_core.adapters.persistence.sqlalchemy_models import Base
    from tests.contract.reconsolidation_summary_cases import summary_inputs

    async with postgres_stores() as uow:
        job, group, _, clauses = await summary_inputs(uow)
        prepared = await uow.reconsolidation.plan_summary(
            principal(), job.lease_token, group.id, clauses, NOW
        )
        assert prepared is not None
        await uow.reconsolidation.commit_summary(
            principal(), job.lease_token, group.id, prepared, NOW
        )
        await uow.reconsolidation.release(principal(), job.lease_token, NOW)
    async with postgres_stores() as uow:
        assert isinstance(uow.reconsolidation, PostgresReconsolidationStore)
        connection = uow.reconsolidation._session
        async with connection.begin_nested() as nested:
            try:
                await connection.execute(
                    text("CREATE ROLE m32_summary_reader NOSUPERUSER NOBYPASSRLS")
                )
                await connection.execute(text("GRANT USAGE ON SCHEMA public TO m32_summary_reader"))
                await connection.execute(
                    text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO m32_summary_reader")
                )
                await connection.execute(text("SET LOCAL ROLE m32_summary_reader"))
                for tenant, count in ((TENANT, 1), ("foreign-tenant", 0)):
                    await connection.execute(
                        text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                        {"tenant": tenant},
                    )
                    assert (
                        await connection.scalar(
                            text("SELECT count(*) FROM reconsolidation_summaries")
                        )
                        == count
                    )
            finally:
                await nested.rollback()
        migration = import_module("migrations.versions.ea3210a1b007_persist_extractive_summaries")

        def downgrade(sync: Any) -> None:
            context = MigrationContext.configure(sync, opts={"target_metadata": Base.metadata})
            monkeypatch.setattr(migration, "op", Operations(context))
            migration.downgrade()

        with pytest.raises(RuntimeError, match="summary history and content would be lost"):
            await (await connection.connection()).run_sync(downgrade)
        assert await connection.scalar(text("SELECT count(*) FROM reconsolidation_summaries")) == 1


@pytest.mark.parametrize("scenario", SURFACE_SCENARIOS, ids=lambda case: case.__name__)
async def test_owner_controls(
    postgres_stores: Factory, scenario: Callable[[Factory], Awaitable[None]]
) -> None:
    await scenario(postgres_stores)


@pytest.mark.parametrize("scenario", DERIVED_SCENARIOS, ids=lambda case: case.__name__)
async def test_derived_owner_controls(
    scenario: Callable[[UnitOfWorkFactory], Awaitable[None]],
) -> None:
    from agent_core.bootstrap import build

    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        clock=FixedClock(NOW),
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await scenario(app.uow_factory)


@pytest.mark.parametrize("mode", ["summary", "rejection", "rollback"])
async def test_derived_snapshot_erasure(mode: Literal["summary", "rejection", "rollback"]) -> None:
    from agent_core.bootstrap import build
    from tests.contract.reconsolidation_summary_context_cases import summary_snapshot_erasure

    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        clock=FixedClock(NOW),
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await summary_snapshot_erasure(app.uow_factory, mode)


@pytest.mark.parametrize(
    "mode", ["receipt", "rejection", "review_then_source_change", "unreviewed_projection"]
)
async def test_derived_receipt_rls_and_guarded_downgrade(
    postgres_stores: Factory, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from importlib import import_module
    from typing import Any

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import IntegrityError

    from agent_core.adapters.persistence.sqlalchemy_models import Base
    from agent_core.domain.derived_memory import SummaryWriteReceipt
    from tests.contract.reconsolidation_summary_cases import summary_inputs

    async with postgres_stores() as uow:
        job, group, originals, clauses = await summary_inputs(uow)
        prepared = await uow.reconsolidation.plan_summary(
            principal(), job.lease_token, group.id, clauses, NOW
        )
        assert prepared is not None
        operation = await uow.reconsolidation.commit_summary(
            principal(), job.lease_token, group.id, prepared, NOW
        )
        await uow.reconsolidation.release(principal(), job.lease_token, NOW)
        if mode == "receipt":
            await uow.reconsolidation.record_summary_write(
                principal(),
                "key",
                SummaryWriteReceipt(operation_id=operation.id, request_hash="f" * 64),
            )
        elif mode != "unreviewed_projection":
            await uow.reconsolidation.change_summary(
                principal(), operation.id, "delete" if mode == "rejection" else "dismiss", NOW
            )
            if mode == "review_then_source_change":
                await uow.memories.reinforce(originals[0].model_copy(update={"confidence": 0.1}))
    async with postgres_stores() as uow:
        assert isinstance(uow.reconsolidation, PostgresReconsolidationStore)
        connection = uow.reconsolidation._session
        if mode == "receipt":
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "INSERT INTO reconsolidation_memory_writes "
                            "(tenant_id, principal_id, key_digest, operation_id, payload) "
                            "VALUES (:tenant, 'wrong-owner', 'cross-owner', :operation, '{}')"
                        ),
                        {"tenant": TENANT, "operation": operation.id},
                    )
            async with connection.begin_nested() as nested:
                try:
                    await connection.execute(
                        text("CREATE ROLE m32_receipt_reader NOSUPERUSER NOBYPASSRLS")
                    )
                    await connection.execute(
                        text("GRANT USAGE ON SCHEMA public TO m32_receipt_reader")
                    )
                    await connection.execute(
                        text("GRANT SELECT ON reconsolidation_memory_writes TO m32_receipt_reader")
                    )
                    await connection.execute(text("SET LOCAL ROLE m32_receipt_reader"))
                    for tenant, count in ((TENANT, 1), ("foreign", 0)):
                        await connection.execute(
                            text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                            {"tenant": tenant},
                        )
                        assert (
                            await connection.scalar(
                                text("SELECT count(*) FROM reconsolidation_memory_writes")
                            )
                            == count
                        )
                finally:
                    await nested.rollback()
        migration = import_module("migrations.versions.ea3210a1b008_derived_memory_owner_receipts")

        def downgrade(sync: Any) -> None:
            monkeypatch.setattr(
                migration,
                "op",
                Operations(
                    MigrationContext.configure(sync, opts={"target_metadata": Base.metadata})
                ),
            )
            migration.downgrade()

        with pytest.raises(RuntimeError, match="receipts and suppression must not be lost"):
            await (await connection.connection()).run_sync(downgrade)
        assert (
            await connection.scalar(text("SELECT to_regclass('reconsolidation_memory_writes')"))
            is not None
        )


@pytest.mark.parametrize("active", [False, True], ids=["call_history", "leased_work"])
async def test_call_audit_downgrade_preserves_history(
    postgres_stores: Factory, monkeypatch: pytest.MonkeyPatch, active: bool
) -> None:
    from importlib import import_module
    from typing import Any

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from tests.contract.reconsolidation_audit_cases import audited_reservation

    job, group, _, spend = await audited_reservation(postgres_stores)
    if not active:
        async with postgres_stores() as uow:
            await uow.reconsolidation.settle(principal(), job.lease_token, spend.id, None, NOW)
            await uow.reconsolidation.finish_group(
                principal(), job.lease_token, group.id, "no_change", NOW
            )
            await uow.reconsolidation.release(principal(), job.lease_token, NOW)
    async with postgres_stores() as uow:
        assert isinstance(uow.reconsolidation, PostgresReconsolidationStore)
        connection = uow.reconsolidation._session
        migration = import_module("migrations.versions.ea3210a1b00a_audit_reconsolidation_calls")

        def downgrade(sync: Any) -> None:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sync)))
            migration.downgrade()

        with pytest.raises(RuntimeError, match="provider audit history must not be lost"):
            await (await connection.connection()).run_sync(downgrade)
        assert (await uow.reconsolidation.get_spend(principal(), spend.id)).call_audit is not None


async def test_call_audit_migration_keeps_legacy_reservations(
    postgres_stores: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from importlib import import_module
    from typing import Any

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    async with postgres_stores() as uow:
        job = await uow.reconsolidation.claim_due(principal(), NOW, "legacy")
        assert job is not None
        spend = await uow.reconsolidation.reserve(
            principal(), job.lease_token, "a" * 64, Decimal("0.1"), NOW
        )
        spend = await uow.reconsolidation.settle(principal(), job.lease_token, spend.id, None, NOW)
        await uow.reconsolidation.release(principal(), job.lease_token, NOW)
    async with postgres_stores() as uow:
        assert isinstance(uow.reconsolidation, PostgresReconsolidationStore)
        connection = uow.reconsolidation._session
        migration = import_module("migrations.versions.ea3210a1b00a_audit_reconsolidation_calls")

        def round_trip(sync: Any) -> None:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sync)))
            migration.downgrade()
            migration.upgrade()

        await (await connection.connection()).run_sync(round_trip)
        assert await uow.reconsolidation.get_spend(principal(), spend.id) == spend
        assert spend.call_audit is None


@pytest.mark.parametrize("kind", ["infer_connection", "flag_conflict"])
async def test_grounded_operation_downgrade_preserves_history(
    postgres_stores: Factory, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from importlib import import_module
    from typing import Any, cast

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from agent_core.adapters.determinism import FixedClock
    from agent_core.memory.reconsolidation_apply import apply_review
    from agent_core.ports.persistence import UnitOfWorkFactory
    from tests.contract.reconsolidation_apply_cases import prepared_batch

    def connection(ops: list[dict[str, Any]]) -> None:
        del ops[1:]
        if kind == "infer_connection":
            ops[0]["clauses"][0]["text"] = "User may prefer concise explanations."

    prepared, review = await prepared_batch(
        postgres_stores, kind=kind, independent=True, change=connection
    )
    result = await apply_review(
        cast(UnitOfWorkFactory, postgres_stores),
        FixedClock(NOW),
        principal(),
        prepared,
        review,
        admitted=lambda: True,
    )
    assert result[0].operation_ids
    async with postgres_stores() as uow:
        await uow.reconsolidation.release(principal(), prepared.lease_token, NOW)
    async with postgres_stores() as uow:
        assert isinstance(uow.reconsolidation, PostgresReconsolidationStore)
        migration = import_module("migrations.versions.ea3210a1b00b_grounded_connection_operations")

        def downgrade(sync: Any) -> None:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sync)))
            migration.downgrade()

        with pytest.raises(RuntimeError, match="previous readers cannot decode"):
            await (await uow.reconsolidation._session.connection()).run_sync(downgrade)


@pytest.mark.parametrize("state", ["schedule", "proposal"])
async def test_reviewed_dreaming_downgrade_preserves_owner_work(
    postgres_stores: Factory, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    from importlib import import_module
    from typing import Any

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from tests.contract.dreaming_cases import review_mode_stages_merge_without_suppressing_originals

    if state == "proposal":
        await review_mode_stages_merge_without_suppressing_originals(postgres_stores)
    else:
        async with postgres_stores() as uow:
            await uow.reconsolidation.claim_dreaming_run(principal(), NOW)
    async with postgres_stores() as uow:
        assert isinstance(uow.reconsolidation, PostgresReconsolidationStore)
        connection = uow.reconsolidation._session
        migration = import_module("migrations.versions.ea3210a1b00c_owner_reviewed_dreaming")

        def downgrade(sync: Any) -> None:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sync)))
            migration.downgrade()

        with pytest.raises(RuntimeError, match="Preserve dreaming review and scheduling history"):
            await (await connection.connection()).run_sync(downgrade)
        assert await connection.scalar(text("SELECT to_regclass('dreaming_runs')")) is not None
