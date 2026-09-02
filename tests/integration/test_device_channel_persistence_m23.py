"""PostgreSQL contracts for Milestone 23 device-channel persistence."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, text

from agent_core.adapters.persistence.sqlalchemy_models import DeviceIngestReceiptRow
from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
from agent_core.bootstrap import build
from agent_core.domain.devices import DeviceTriageMapping
from agent_core.domain.errors import NotFoundError
from tests.contract import test_device_ingest_store_contract as ingest_contract
from tests.contract import test_device_invocation_store_contract as invocation_contract
from tests.contract.support import (
    NOW,
    RUN_ID,
    SESSION_ID,
    TENANT,
    agent,
    principal,
    run,
    session,
)
from tests.contract.test_device_registry_contract import (
    assert_declared_capabilities_survive_the_round_trip,
    device,
)
from tests.integration.m2_support import database_settings

_DEVICE_IDS = (
    invocation_contract.DEVICE_ID,
    invocation_contract.OTHER_DEVICE_ID,
    ingest_contract.DEVICE_ID,
    ingest_contract.OTHER_DEVICE_ID,
)


class _RollbackContractError(Exception):
    pass


async def _seed(uow: Any) -> None:
    """Create the tenant-a agent, sessions, run, and devices the contracts name."""

    await uow.session.execute(text("SELECT set_config('agent_core.tenant_id', 'tenant-a', true)"))
    await uow.agents.put(agent())
    await uow.sessions.create(session())
    await uow.sessions.create(
        session().model_copy(update={"id": ingest_contract.ROTATED_SESSION_ID})
    )
    await uow.runs.create(run())
    for index, device_id in enumerate(_DEVICE_IDS):
        await uow.devices.upsert(
            device(
                device_id=device_id,
                client_device_id=f"m23-contract-device-{index}",
                token=None,
            ),
            principal(),
        )


async def test_postgres_device_invocation_store_satisfies_shared_contracts() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        for contract in (
            invocation_contract.assert_invocation_creation_is_idempotent_and_device_scoped,
            invocation_contract.assert_first_result_wins_and_expired_rows_refuse_results,
            invocation_contract.assert_expiry_sweeps_only_overdue_pending_rows,
        ):
            with pytest.raises(_RollbackContractError):
                async with composition.uow_factory() as uow:
                    assert isinstance(uow, PostgresUnitOfWork)
                    await _seed(uow)
                    await contract(uow.device_invocations)
                    raise _RollbackContractError


async def test_postgres_device_ingest_store_satisfies_shared_contracts() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        for contract in (
            ingest_contract.assert_receipts_are_stored_once_and_routed_after_the_fact,
            ingest_contract.assert_receipt_counting_is_per_device_channel_and_utc_day,
            ingest_contract.assert_triage_mapping_is_created_then_replaced,
        ):
            with pytest.raises(_RollbackContractError):
                async with composition.uow_factory() as uow:
                    assert isinstance(uow, PostgresUnitOfWork)
                    await _seed(uow)
                    await contract(uow.device_ingest)
                    raise _RollbackContractError


async def test_postgres_attach_routing_pins_the_session_and_run_on_the_receipt() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                assert isinstance(uow, PostgresUnitOfWork)
                await _seed(uow)
                await uow.device_ingest.record(ingest_contract.receipt())
                await uow.device_ingest.attach_routing(
                    device_id=ingest_contract.DEVICE_ID,
                    channel=ingest_contract.CHANNEL,
                    digest=ingest_contract.DIGEST_A,
                    session_id=SESSION_ID,
                    run_id=RUN_ID,
                )
                row = (
                    await uow.session.scalars(
                        select(DeviceIngestReceiptRow).where(
                            DeviceIngestReceiptRow.device_id == ingest_contract.DEVICE_ID,
                            DeviceIngestReceiptRow.channel == ingest_contract.CHANNEL,
                            DeviceIngestReceiptRow.digest == ingest_contract.DIGEST_A,
                        )
                    )
                ).one()
                assert row.session_id == SESSION_ID
                assert row.run_id == RUN_ID
                assert row.received_at == NOW
                raise _RollbackContractError


async def test_postgres_device_row_round_trips_declared_capabilities() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                assert isinstance(uow, PostgresUnitOfWork)
                await uow.session.execute(
                    text("SELECT set_config('agent_core.tenant_id', 'tenant-a', true)")
                )
                await assert_declared_capabilities_survive_the_round_trip(uow.devices)
                raise _RollbackContractError


async def test_postgres_device_deletion_purges_its_device_channel_rows() -> None:
    """Deleting a device succeeds and leaves no device-channel row behind.

    The registry's delete is a bare ``DELETE FROM devices``; it enumerates no
    dependent table. ON DELETE CASCADE is therefore what keeps deletion from
    either failing permanently or orphaning rows.
    """

    tables = ("device_invocations", "device_ingest_receipts", "device_triage_sessions")
    async with build(settings=database_settings(), storage="postgres") as composition:
        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                assert isinstance(uow, PostgresUnitOfWork)
                await _seed(uow)
                await uow.device_invocations.create(invocation_contract.invocation())
                await uow.device_ingest.record(ingest_contract.receipt())
                await uow.device_ingest.set_triage_mapping(
                    DeviceTriageMapping(
                        device_id=ingest_contract.DEVICE_ID,
                        tenant_id=TENANT,
                        channel=ingest_contract.CHANNEL,
                        session_id=SESSION_ID,
                    )
                )
                assert [
                    await uow.session.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
                    for table in tables
                ] == [1, 1, 1]

                await uow.devices.delete(invocation_contract.DEVICE_ID, principal())
                await uow.devices.delete(ingest_contract.DEVICE_ID, principal())

                with pytest.raises(NotFoundError):
                    await uow.devices.get(invocation_contract.DEVICE_ID, principal())
                assert [
                    await uow.session.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
                    for table in tables
                ] == [0, 0, 0]
                raise _RollbackContractError


async def test_postgres_device_channel_rows_are_tenant_isolated_and_rls_forced() -> None:
    tables = ["device_invocations", "device_ingest_receipts", "device_triage_sessions"]
    async with build(settings=database_settings(), storage="postgres") as composition:
        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                assert isinstance(uow, PostgresUnitOfWork)
                await _seed(uow)
                await uow.device_invocations.create(invocation_contract.invocation())
                await uow.device_ingest.record(ingest_contract.receipt())
                await uow.device_ingest.set_triage_mapping(
                    DeviceTriageMapping(
                        device_id=ingest_contract.DEVICE_ID,
                        tenant_id=TENANT,
                        channel=ingest_contract.CHANNEL,
                        session_id=SESSION_ID,
                    )
                )

                is_superuser = bool(
                    await uow.session.scalar(
                        text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                    )
                )
                if is_superuser:
                    await uow.session.execute(
                        text("CREATE ROLE veetbot_device_channel_rls_probe NOLOGIN NOSUPERUSER")
                    )
                    await uow.session.execute(
                        text(
                            "GRANT SELECT ON device_invocations, device_ingest_receipts, "
                            "device_triage_sessions TO veetbot_device_channel_rls_probe"
                        )
                    )
                    await uow.session.execute(
                        text("SET LOCAL ROLE veetbot_device_channel_rls_probe")
                    )
                await uow.session.execute(
                    text("SELECT set_config('agent_core.tenant_id', 'another-tenant', true)")
                )
                counts = [
                    await uow.session.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
                    for table in tables
                ]
                assert counts == [0, 0, 0]

                rows = (
                    await uow.session.execute(
                        text(
                            "SELECT relname, relrowsecurity, relforcerowsecurity "
                            "FROM pg_class WHERE relname = ANY(:tables)"
                        ),
                        {"tables": tables},
                    )
                ).all()
                assert {row.relname for row in rows} == set(tables)
                assert all(row.relrowsecurity and row.relforcerowsecurity for row in rows)
                if is_superuser:
                    await uow.session.execute(text("RESET ROLE"))
                    await uow.session.execute(
                        text(
                            "REVOKE SELECT ON device_invocations, device_ingest_receipts, "
                            "device_triage_sessions FROM veetbot_device_channel_rls_probe"
                        )
                    )
                    await uow.session.execute(text("DROP ROLE veetbot_device_channel_rls_probe"))
                raise _RollbackContractError
