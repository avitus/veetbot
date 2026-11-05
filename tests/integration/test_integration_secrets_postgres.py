"""ADR-0275 persistence, real transactions, RLS and independent worker reads."""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import sys
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
from agent_core.bootstrap import build
from agent_core.config import Settings
from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import ConflictError, NotFoundError
from tests.contract.test_integration_secret_store_contract import store_contract
from tests.integration.m2_support import database_settings
from tests.integration.test_cockpit_store_postgres_m35 import _alembic

KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
CANARY = "synthetic-postgres-integration-key"


def settings() -> Settings:
    return replace(
        database_settings(),
        integration_secrets_enabled=True,
        integration_secrets_key=SecretStr(KEY),
    )


async def test_postgres_store_contract_and_rls() -> None:
    async with build(settings=settings(), storage="postgres") as app, app.uow_factory() as uow:
        await store_contract(uow.integration_secrets, app.principal)
        assert isinstance(uow, PostgresUnitOfWork)
        await uow.session.execute(text("CREATE ROLE integration_key_rls_probe NOLOGIN NOSUPERUSER"))
        await uow.session.execute(
            text("GRANT SELECT, INSERT, UPDATE ON integration_secrets TO integration_key_rls_probe")
        )
        await uow.session.execute(text("SET LOCAL ROLE integration_key_rls_probe"))
        await uow.session.execute(text("SELECT set_config('agent_core.tenant_id', 'other', true)"))
        assert await uow.session.scalar(text("SELECT count(*) FROM integration_secrets")) == 0
        flags = (
            await uow.session.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname='integration_secrets'"
                )
            )
        ).one()
        assert tuple(flags) == (True, True)
        await uow.session.execute(text("RESET ROLE"))
        await uow.session.execute(
            text("REVOKE ALL ON integration_secrets FROM integration_key_rls_probe")
        )
        await uow.session.execute(text("DROP ROLE integration_key_rls_probe"))


def worker_read(key_id: UUID) -> str:
    """Fresh process and connection, with only the separately supplied synthetic key."""
    script = """
import asyncio, os
from dataclasses import replace
from pydantic import SecretStr
from agent_core.bootstrap import build
from agent_core.config import Settings
from tests.integration.m2_support import database_settings
async def main():
    settings = replace(database_settings(), integration_secrets_enabled=True,
                       integration_secrets_key=SecretStr(os.environ["SYNTHETIC_MASTER"]))
    async with build(settings=settings, storage="postgres") as app:
        try:
            _, value = await app.services.integration_secrets.resolve(
                app.principal, os.environ["SYNTHETIC_REFERENCE"], "api.example.com")
            print(value.reveal())
        except (PermissionError, LookupError):
            print("unavailable")
        except Exception as error:
            from agent_core.domain.errors import NotFoundError
            if isinstance(error, NotFoundError): print("unavailable")
            else: raise
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "SYNTHETIC_MASTER": KEY, "SYNTHETIC_REFERENCE": f"stored:{key_id}"},
    )
    return result.stdout.strip().splitlines()[-1]


async def test_restart_rotation_restore_and_revocation_across_worker_processes() -> None:
    key_id = uuid4()
    async with build(settings=settings(), storage="postgres") as app:
        service = app.services.integration_secrets
        assert service is not None
        await service.put(
            app.principal,
            key_id,
            label="Example",
            origin="https://api.example.com",
            auth_scheme="bearer",
            value=SecretValue(CANARY),
            expected_revision=0,
        )
        async with app.uow_factory() as uow:
            backup = await uow.integration_secrets.get(app.principal, key_id)
            assert backup is not None and backup.sealed_value is not None
            assert CANARY not in backup.sealed_value
    assert await asyncio.to_thread(worker_read, key_id) == CANARY
    async with build(settings=settings(), storage="postgres") as app:
        service = app.services.integration_secrets
        assert service is not None
        await service.put(
            app.principal,
            key_id,
            label="Example",
            origin="https://api.example.com",
            auth_scheme="bearer",
            value=SecretValue("replacement"),
            expected_revision=1,
        )
        assert await asyncio.to_thread(worker_read, key_id) == "replacement"
        await service.delete(app.principal, key_id, 2)
        assert await asyncio.to_thread(worker_read, key_id) == "unavailable"
        with pytest.raises(RuntimeError, match="tombstones"):
            _alembic("downgrade", "e3b271c10402")
        # A logical ciphertext backup restored to an empty disposable table.
        async with app.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork)
            await uow.session.execute(text("DELETE FROM integration_secrets"))
            await uow.integration_secrets.put(app.principal, backup)
    assert await asyncio.to_thread(worker_read, key_id) == CANARY
    async with build(
        settings=replace(
            settings(),
            integration_secrets_key=SecretStr(base64.urlsafe_b64encode(bytes(32)).decode()),
        ),
        storage="postgres",
    ) as app:
        assert app.services.integration_secrets is not None
        with pytest.raises(PermissionError):
            await app.services.integration_secrets.resolve(
                app.principal, f"stored:{key_id}", "api.example.com"
            )


async def test_concurrent_retries_cap_owner_isolation_and_audit_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with build(settings=settings(), storage="postgres") as app:
        service = app.services.integration_secrets
        assert service is not None
        owner = app.principal
        key_id = uuid4()

        async def save(value: str, revision: int, target: UUID = key_id) -> dict[str, object]:
            return await service.put(
                owner,
                target,
                label="Example",
                origin="https://api.example.com",
                auth_scheme="bearer",
                value=SecretValue(value),
                expected_revision=revision,
            )

        results = await asyncio.gather(save(CANARY, 0), save(CANARY, 0))
        assert results[0] == results[1]
        results2 = await asyncio.gather(save("first", 1), save("second", 1), return_exceptions=True)
        assert sum(isinstance(result, ConflictError) for result in results2) == 1
        with pytest.raises(ConflictError):
            await service.delete(owner, key_id, 1)
        other = owner.model_copy(update={"principal_id": "other"})
        with pytest.raises(NotFoundError):
            await service.put(
                other,
                key_id,
                label="Example",
                origin="https://api.example.com",
                auth_scheme="bearer",
                value=SecretValue(CANARY),
                expected_revision=0,
            )
        async with app.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork)
            events = (
                (
                    await uow.session.execute(
                        text(
                            "SELECT payload FROM process_events "
                            "WHERE event_type LIKE 'settings.integration_secrets.%'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(events) == 2
            assert CANARY not in str(events) and "sealed_value" not in str(events)
        original = service._audit

        async def failed_audit(*args: object) -> None:
            raise RuntimeError("synthetic audit failure")

        monkeypatch.setattr(service, "_audit", failed_audit)
        failed_id = uuid4()
        with pytest.raises(RuntimeError, match="audit failure"):
            await save(CANARY, 0, failed_id)
        async with app.uow_factory() as uow:
            assert await uow.integration_secrets.get(owner, failed_id) is None
        monkeypatch.setattr(service, "_audit", original)
        for _ in range(98):
            await save(CANARY, 0, uuid4())
        capped = await asyncio.gather(
            save(CANARY, 0, uuid4()), save(CANARY, 0, uuid4()), return_exceptions=True
        )
        assert sum(isinstance(result, ConflictError) for result in capped) == 1
        assert len(await service.list(owner)) == 100
