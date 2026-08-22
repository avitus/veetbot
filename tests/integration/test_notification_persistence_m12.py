"""PostgreSQL contracts for Milestone 12 — Notifications and device identity."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import text

from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.notifications import DeliveryOutcome, NotificationDelivery
from tests.contract.support import NOW, TENANT, principal
from tests.contract.test_device_registry_contract import (
    DEVICE_ID,
    assert_device_listing_is_stable,
    assert_device_registration_is_idempotent_and_principal_scoped,
    assert_device_tokens_move_and_lifecycle_removes_targets,
    device,
)
from tests.contract.test_notification_outbox_contract import (
    assert_notification_claim_is_partitioned_by_provider,
    assert_notification_claim_settle_and_pagination,
    assert_notification_delivery_attempt_is_unique,
    assert_notification_enqueue_deduplicates_and_lists_by_principal,
    new_notification,
)
from tests.integration.m2_support import database_settings


class _RollbackContractError(Exception):
    pass


async def test_postgres_device_registration_is_idempotent_and_principal_scoped() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                assert isinstance(uow, PostgresUnitOfWork)
                await uow.session.execute(
                    text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                    {"tenant": TENANT},
                )
                await assert_device_registration_is_idempotent_and_principal_scoped(uow.devices)
                raise _RollbackContractError


async def test_postgres_live_push_token_moves_to_new_installation() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                assert isinstance(uow, PostgresUnitOfWork)
                await uow.session.execute(
                    text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                    {"tenant": TENANT},
                )
                await assert_device_tokens_move_and_lifecycle_removes_targets(uow.devices)
                raise _RollbackContractError


async def test_postgres_device_listing_uses_stable_cursor() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                assert isinstance(uow, PostgresUnitOfWork)
                await uow.session.execute(
                    text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                    {"tenant": TENANT},
                )
                await assert_device_listing_is_stable(uow.devices)
                raise _RollbackContractError


async def test_postgres_notification_outbox_satisfies_shared_contracts() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        for contract in (
            assert_notification_enqueue_deduplicates_and_lists_by_principal,
            assert_notification_claim_settle_and_pagination,
        ):
            with pytest.raises(_RollbackContractError):
                async with composition.uow_factory() as uow:
                    assert isinstance(uow, PostgresUnitOfWork)
                    await uow.session.execute(
                        text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                        {"tenant": TENANT},
                    )
                    await contract(uow.notification_outbox)
                    raise _RollbackContractError

        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                assert isinstance(uow, PostgresUnitOfWork)
                await uow.session.execute(
                    text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                    {"tenant": TENANT},
                )
                await assert_notification_delivery_attempt_is_unique(
                    uow.notification_outbox, uow.devices
                )
                raise _RollbackContractError

        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                assert isinstance(uow, PostgresUnitOfWork)
                await uow.session.execute(
                    text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
                    {"tenant": TENANT},
                )
                await assert_notification_claim_is_partitioned_by_provider(
                    uow.notification_outbox, uow.devices
                )
                raise _RollbackContractError


async def test_device_and_outbox_writes_share_the_repository_unit_of_work() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        owner = composition.principal
        owned_device = device(token=None).model_copy(
            update={"tenant_id": owner.tenant_id, "principal_id": owner.principal_id}
        )
        owned_notification = new_notification().model_copy(
            update={"tenant_id": owner.tenant_id, "principal_id": owner.principal_id}
        )

        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                await uow.devices.upsert(owned_device, owner)
                assert await uow.notification_outbox.enqueue(owned_notification) is not None
                raise _RollbackContractError

        async with composition.uow_factory() as uow:
            assert await uow.devices.list(owner, limit=10) == []
            assert await uow.notification_outbox.list(owner, limit=10) == []
            await uow.devices.upsert(owned_device, owner)
            assert await uow.notification_outbox.enqueue(owned_notification) is not None

        async with composition.uow_factory() as uow:
            assert await uow.devices.list(owner, limit=10) == [owned_device]
            notifications = await uow.notification_outbox.list(owner, limit=10)
            assert len(notifications) == 1
            assert notifications[0].id == owned_notification.id


async def test_postgres_repeated_notification_triggers_create_one_durable_row() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        owner = composition.principal
        expected_keys: set[str] = set()
        async with composition.uow_factory() as uow:
            for offset in range(32):
                notification_id = UUID(int=owned_id_int(offset))
                dedupe_key = f"generated-repeat:{offset}"
                expected_keys.add(dedupe_key)
                first = new_notification(
                    notification_id=notification_id,
                    dedupe_key=dedupe_key,
                ).model_copy(
                    update={
                        "tenant_id": owner.tenant_id,
                        "principal_id": owner.principal_id,
                    }
                )
                assert await uow.notification_outbox.enqueue(first) is not None
                for repeat in range(1, 4):
                    repeated = new_notification(
                        notification_id=UUID(int=notification_id.int + repeat),
                        dedupe_key=dedupe_key,
                    ).model_copy(
                        update={
                            "tenant_id": owner.tenant_id,
                            "principal_id": owner.principal_id,
                        }
                    )
                    assert await uow.notification_outbox.enqueue(repeated) is None

        async with composition.uow_factory() as uow:
            rows = await uow.notification_outbox.list(owner, limit=100)
            assert len(rows) == len(expected_keys)
            assert {row.dedupe_key for row in rows} == expected_keys


async def test_notification_rows_are_principal_isolated_and_rls_forced() -> None:
    async with (
        build(settings=database_settings(), storage="postgres") as composition,
        composition.uow_factory() as uow,
    ):
        assert isinstance(uow, PostgresUnitOfWork)
        await uow.session.execute(
            text("SELECT set_config('agent_core.tenant_id', :tenant, true)"),
            {"tenant": TENANT},
        )
        owned_device = device(token=None)
        owned_notification = new_notification()
        await uow.devices.upsert(owned_device, principal())
        notification = await uow.notification_outbox.enqueue(owned_notification)
        assert notification is not None
        await uow.notification_outbox.record_delivery(
            NotificationDelivery(
                id=owned_notification.id,
                notification_id=owned_notification.id,
                device_id=DEVICE_ID,
                attempt=1,
                outcome=DeliveryOutcome.DELIVERED,
                attempted_at=NOW,
            )
        )

        stranger = Principal(
            tenant_id=TENANT,
            principal_id="principal-b",
            roles={"user"},
            scopes=set(),
        )
        with pytest.raises(NotFoundError):
            await uow.devices.get(owned_device.id, stranger)
        with pytest.raises(NotFoundError):
            await uow.devices.revoke(owned_device.id, stranger, NOW)
        assert await uow.devices.list(stranger, limit=10) == []
        assert await uow.notification_outbox.list(stranger, limit=10) == []

        stranger_device = device(
            device_id=UUID(int=DEVICE_ID.int + 50),
            client_device_id="principal-b-device",
            principal_id=stranger.principal_id,
            token=None,
        )
        await uow.devices.upsert(stranger_device, stranger)
        with pytest.raises(NotFoundError):
            await uow.notification_outbox.record_delivery(
                NotificationDelivery(
                    id=UUID(int=owned_notification.id.int + 50),
                    notification_id=owned_notification.id,
                    device_id=stranger_device.id,
                    attempt=1,
                    outcome=DeliveryOutcome.DELIVERED,
                    attempted_at=NOW,
                )
            )

        is_superuser = bool(
            await uow.session.scalar(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            )
        )
        if is_superuser:
            await uow.session.execute(
                text("CREATE ROLE veetbot_notification_rls_probe NOLOGIN NOSUPERUSER")
            )
            await uow.session.execute(
                text(
                    "GRANT SELECT ON devices, notification_outbox, "
                    "notification_deliveries TO veetbot_notification_rls_probe"
                )
            )
            await uow.session.execute(text("SET LOCAL ROLE veetbot_notification_rls_probe"))
        await uow.session.execute(
            text("SELECT set_config('agent_core.tenant_id', 'another-tenant', true)")
        )
        counts = [
            await uow.session.scalar(text(f"SELECT count(*) FROM {table}"))
            for table in ("devices", "notification_outbox", "notification_deliveries")
        ]
        assert counts == [0, 0, 0]
        rows = (
            await uow.session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:tables)"
                ),
                {
                    "tables": [
                        "devices",
                        "notification_outbox",
                        "notification_deliveries",
                    ]
                },
            )
        ).all()
        assert {row.relname for row in rows} == {
            "devices",
            "notification_outbox",
            "notification_deliveries",
        }
        assert all(row.relrowsecurity and row.relforcerowsecurity for row in rows)
        if is_superuser:
            await uow.session.execute(text("RESET ROLE"))
            await uow.session.execute(
                text(
                    "REVOKE SELECT ON devices, notification_outbox, "
                    "notification_deliveries FROM veetbot_notification_rls_probe"
                )
            )
            await uow.session.execute(text("DROP ROLE veetbot_notification_rls_probe"))


def owned_id_int(offset: int) -> int:
    return int("40000000000000000000000000000000", 16) + offset * 10
