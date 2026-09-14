"""Real PostgreSQL parity and cross-transaction admission for calling."""

import asyncio
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from agent_core.adapters.persistence.calls import PostgresCallStore
from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.adapters.persistence.sqlalchemy_models import CallRecordRow
from agent_core.application.calling import CallService
from agent_core.bootstrap import build
from agent_core.domain.calls import CallRecord
from agent_core.domain.mcp import MCPCallResult
from tests.contract.support import NOW, principal, tool_context
from tests.contract.test_bland_client_contract import CALL_ID
from tests.contract.test_call_source_erasure_contract import assert_call_source_erasure
from tests.contract.test_call_store_contract import call_store_contract
from tests.gates.test_call_lifecycle import SIGNING_FIXTURE, arguments, normalized_call, signed_body
from tests.gates.test_call_m27 import call_configuration
from tests.integration.m2_support import database_settings


async def test_postgres_call_store_contract() -> None:
    engine = create_engine(database_settings().database_url)
    try:
        async with create_session_factory(engine)() as session:
            await session.execute(
                text("SELECT set_config('agent_core.tenant_id', 'call-tenant', true)")
            )
            await call_store_contract(PostgresCallStore(session))
            await session.commit()
    finally:
        await engine.dispose()


async def test_postgres_call_source_erasure_contract() -> None:
    async with build(
        settings=database_settings(), storage="postgres", principal=principal()
    ) as app:
        await assert_call_source_erasure(app.uow_factory)


async def test_postgres_concurrent_call_dispatches_share_one_reservation() -> None:
    async with build(
        settings=database_settings(), storage="postgres", principal=principal()
    ) as app:
        first = CallService(app.uow_factory, app.clock, app.principal, call_configuration())
        second = CallService(app.uow_factory, app.clock, app.principal, call_configuration())
        dispatched = 0

        async def accepted(args: dict[str, Any]) -> MCPCallResult:
            nonlocal dispatched
            assert not app.uow_factory.is_open()
            dispatched += 1
            return MCPCallResult(structured={"provider_call_id": CALL_ID, "status": "accepted"})

        results = await asyncio.gather(
            *[
                service.invoke(
                    replace(tool_context(), invocation_id=uuid4()),
                    "bland_call",
                    "start_call",
                    arguments(),
                    accepted,
                )
                for service in (first, second)
            ]
        )
        assert dispatched == 1 and sum(not value.is_error for value in results) == 1


async def test_postgres_signed_receipt_and_notification_are_deduplicated() -> None:
    from agent_core.adapters.determinism import FixedClock

    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        clock=FixedClock(NOW),
    ) as app:

        async def provider(server: str, name: str, args: dict[str, Any]) -> MCPCallResult:
            if name == "provider_list_calls":
                return MCPCallResult(structured={"provider_call_ids": [], "next_offset": None})
            return MCPCallResult(structured=normalized_call())

        service = CallService(
            app.uow_factory,
            app.clock,
            app.principal,
            call_configuration(),
            provider,
            notifications=True,
        )
        assert await service.receive(*signed_body(), SIGNING_FIXTURE)
        assert await service.reconcile() == 1
        assert await service.receive(*signed_body(), SIGNING_FIXTURE)
        assert await service.reconcile() == 0
        async with app.uow_factory() as uow:
            records = await uow.calls.list(app.principal, "call")
            notifications = await uow.notification_outbox.list(app.principal, limit=10)
        assert len(records) == len(notifications) == 1


async def test_call_records_force_rls_and_transaction_rollback() -> None:
    engine = create_engine(database_settings().database_url)
    factory = create_session_factory(engine)
    role = f"call27_probe_{uuid4().hex}"
    row = CallRecord(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        kind="receipt",
        key="probe",
        revision=1,
        payload={"active": True},
        created_at=NOW,
        updated_at=NOW,
    )
    try:
        async with factory() as admin:
            await admin.execute(
                text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                {"tenant": principal().tenant_id},
            )
            await PostgresCallStore(admin).put(row, expected_revision=0)
            await admin.rollback()
            assert await PostgresCallStore(admin).get(principal(), "receipt", "probe") is None
            await PostgresCallStore(admin).put(row, expected_revision=0)
            await admin.execute(text(f"CREATE ROLE {role} NOLOGIN"))
            await admin.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await admin.execute(text(f"GRANT SELECT, INSERT ON call_records TO {role}"))
            await admin.commit()
        try:
            async with factory() as restricted:
                await restricted.execute(
                    text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                    {"tenant": principal().tenant_id},
                )
                await restricted.execute(text(f"SET LOCAL ROLE {role}"))
                assert len((await restricted.execute(select(CallRecordRow))).scalars().all()) == 1
                await restricted.execute(
                    text("SELECT set_config('agent_core.tenant_id', 'foreign', true)")
                )
                assert (await restricted.execute(select(CallRecordRow))).scalars().all() == []
                with pytest.raises(DBAPIError) as denied:
                    async with restricted.begin_nested():
                        await PostgresCallStore(restricted).put(
                            row.model_copy(update={"key": "forbidden"}), expected_revision=0
                        )
                assert getattr(denied.value.orig, "sqlstate", None) == "42501"
                await restricted.rollback()
            async with factory() as admin:
                flags = (
                    await admin.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                            "WHERE relname='call_records'"
                        )
                    )
                ).one()
                assert flags == (True, True)
        finally:
            async with factory() as admin:
                await admin.execute(text(f"DROP OWNED BY {role}"))
                await admin.execute(text(f"DROP ROLE {role}"))
                await admin.commit()
    finally:
        await engine.dispose()
