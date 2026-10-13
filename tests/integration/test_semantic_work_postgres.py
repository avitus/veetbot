"""M33 semantic-index contracts on migrated PostgreSQL and real transactions."""

from collections.abc import Awaitable, Callable

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.bootstrap import build
from agent_core.domain.semantic_work import SemanticJob
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.semantic_indexing_cases import SCENARIOS as INDEX_SCENARIOS
from tests.contract.semantic_query_work_cases import SCENARIOS as QUERY_SCENARIOS
from tests.contract.semantic_work_cases import SCENARIOS as WORK_SCENARIOS
from tests.contract.support import NOW, principal, session
from tests.integration.m2_support import database_settings

SCENARIOS = WORK_SCENARIOS + INDEX_SCENARIOS + QUERY_SCENARIOS


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda case: case.__name__)
async def test_semantic_work(scenario: Callable[[UnitOfWorkFactory], Awaitable[None]]) -> None:
    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        clock=FixedClock(NOW),
    ) as app:
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
        await scenario(app.uow_factory)


async def test_concurrent_semantic_claim_has_one_owner() -> None:
    import asyncio

    from tests.contract.semantic_work_cases import BOUNDS, CONFIG

    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        clock=FixedClock(NOW),
    ) as app:

        async def claim() -> SemanticJob | None:
            async with app.uow_factory() as uow:
                return await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW)

        jobs = await asyncio.gather(claim(), claim())
        assert sum(j is not None for j in jobs) == 1


async def test_semantic_work_and_spend_force_tenant_rls() -> None:
    from decimal import Decimal

    from sqlalchemy import text

    from agent_core.adapters.persistence.semantic_work import PostgresSemanticWorkStore
    from tests.contract.semantic_work_cases import BOUNDS, CONFIG
    from tests.contract.support import TENANT

    async with (
        build(
            settings=database_settings(),
            storage="postgres",
            principal=principal(),
            clock=FixedClock(NOW),
        ) as app,
        app.uow_factory() as uow,
    ):
        job = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW)
        assert job is not None
        assert await uow.semantic_work.reserve(
            principal(), job.lease_token, CONFIG, "a" * 64, Decimal(".1"), 16, 16, NOW
        )
        assert isinstance(uow.semantic_work, PostgresSemanticWorkStore)
        connection = uow.semantic_work._session
        async with connection.begin_nested() as nested:
            try:
                await connection.execute(
                    text("CREATE ROLE m33_work_reader NOSUPERUSER NOBYPASSRLS")
                )
                await connection.execute(text("GRANT USAGE ON SCHEMA public TO m33_work_reader"))
                await connection.execute(
                    text(
                        "GRANT SELECT ON semantic_index_jobs, semantic_index_spend "
                        "TO m33_work_reader"
                    )
                )
                await connection.execute(text("SET LOCAL ROLE m33_work_reader"))
                for tenant, count in ((TENANT, 1), ("foreign", 0)):
                    await connection.execute(
                        text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                        {"tenant": tenant},
                    )
                    for table in ("semantic_index_jobs", "semantic_index_spend"):
                        assert (
                            await connection.scalar(text(f"SELECT count(*) FROM {table}")) == count
                        )
            finally:
                await nested.rollback()


async def test_downgrade_cannot_erase_work_hidden_by_rls(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import Connection, text
    from sqlalchemy.exc import DBAPIError

    from agent_core.adapters.persistence.semantic_work import PostgresSemanticWorkStore
    from tests.contract.semantic_work_cases import BOUNDS, CONFIG

    migration = import_module("migrations.versions.ea3310a1b002_semantic_index_work")
    async with (
        build(
            settings=database_settings(),
            storage="postgres",
            principal=principal(),
            clock=FixedClock(NOW),
        ) as app,
        app.uow_factory() as uow,
    ):
        assert await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW) is not None
        assert isinstance(uow.semantic_work, PostgresSemanticWorkStore)
        connection = uow.semantic_work._session
        async with connection.begin_nested() as nested:
            try:
                await connection.execute(
                    text("CREATE ROLE m33_work_migrator NOSUPERUSER NOBYPASSRLS")
                )
                await connection.execute(
                    text("GRANT USAGE, CREATE ON SCHEMA public TO m33_work_migrator")
                )
                # The guard must fail closed even when its owner role cannot see another tenant.
                for table in ("semantic_index_jobs", "semantic_index_spend"):
                    await connection.execute(
                        text(f"ALTER TABLE {table} OWNER TO m33_work_migrator")
                    )
                await connection.execute(
                    text("GRANT ALL ON reconsolidation_owners TO m33_work_migrator")
                )
                await connection.execute(text("SET LOCAL ROLE m33_work_migrator"))
                await connection.execute(
                    text("SELECT set_config('agent_core.tenant_id', 'foreign', true)")
                )

                def downgrade(sync: Connection) -> None:
                    monkeypatch.setattr(
                        migration, "op", Operations(MigrationContext.configure(sync))
                    )
                    migration.downgrade()

                with pytest.raises(DBAPIError, match="row-level security"):
                    await (await connection.connection()).run_sync(downgrade)
            finally:
                await nested.rollback()


async def test_concurrent_query_reservations_have_one_owner() -> None:
    import asyncio
    from decimal import Decimal

    from agent_core.domain.semantic_work import SemanticSpend
    from tests.contract.semantic_work_cases import CONFIG

    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        clock=FixedClock(NOW),
    ) as app:

        async def reserve() -> SemanticSpend | None:
            async with app.uow_factory() as uow:
                return await uow.semantic_work.reserve_query(
                    principal(), CONFIG, "a" * 64, Decimal(".01"), NOW
                )

        receipts = await asyncio.gather(reserve(), reserve())
        assert sum(r is not None for r in receipts) == 1


async def test_query_downgrade_requires_settlement(monkeypatch: pytest.MonkeyPatch) -> None:
    from decimal import Decimal
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import Connection
    from sqlalchemy.exc import DBAPIError

    from agent_core.adapters.persistence.semantic_work import PostgresSemanticWorkStore
    from tests.contract.semantic_work_cases import CONFIG

    migration = import_module("migrations.versions.ea3310a1b003_semantic_query_admission")
    async with (
        build(
            settings=database_settings(),
            storage="postgres",
            principal=principal(),
            clock=FixedClock(NOW),
        ) as app,
        app.uow_factory() as uow,
    ):
        receipt = await uow.semantic_work.reserve_query(
            principal(), CONFIG, "a" * 64, Decimal(".01"), NOW
        )
        assert receipt is not None
        assert isinstance(uow.semantic_work, PostgresSemanticWorkStore)
        connection = uow.semantic_work._session

        def downgrade(sync: Connection) -> None:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sync)))
            migration.downgrade()

        async with connection.begin_nested() as nested:
            try:
                with pytest.raises(DBAPIError, match="requires settlement"):
                    await (await connection.connection()).run_sync(downgrade)
            finally:
                await nested.rollback()
        await uow.semantic_work.settle_query(principal(), receipt.id, None, NOW)
        async with connection.begin_nested() as nested:
            try:
                await (await connection.connection()).run_sync(downgrade)
                assert await uow.semantic_work.spent(principal(), NOW.date()) == Decimal(".01")
            finally:
                await nested.rollback()


async def test_index_receipts_keep_legacy_payload_for_application_rollback() -> None:
    from decimal import Decimal

    from sqlalchemy import select

    from agent_core.adapters.persistence.semantic_work import PostgresSemanticWorkStore
    from agent_core.adapters.persistence.sqlalchemy_models import SemanticSpendRow
    from tests.contract.semantic_work_cases import BOUNDS, CONFIG

    async with (
        build(
            settings=database_settings(),
            storage="postgres",
            principal=principal(),
            clock=FixedClock(NOW),
        ) as app,
        app.uow_factory() as uow,
    ):
        job = await uow.semantic_work.claim(principal(), CONFIG, BOUNDS, NOW)
        assert job is not None
        receipt = await uow.semantic_work.reserve(
            principal(), job.lease_token, CONFIG, "a" * 64, Decimal(".01"), 16, 16, NOW
        )
        assert receipt is not None
        assert isinstance(uow.semantic_work, PostgresSemanticWorkStore)
        connection = uow.semantic_work._session
        for settle in (False, True):
            if settle:
                await uow.semantic_work.settle(
                    principal(), job.lease_token, receipt.id, Decimal(".001"), NOW
                )
            payload = await connection.scalar(
                select(SemanticSpendRow.payload).where(SemanticSpendRow.id == receipt.id)
            )
            assert payload is not None
            assert not {"kind", "deadline"} & payload.keys(), (
                "legacy index receipts must remain readable after application rollback"
            )
