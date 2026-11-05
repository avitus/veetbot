"""Saved-job persistence and publication namespaces on the real PostgreSQL adapter."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agent_core.bootstrap import build
from agent_core.domain.cockpit import CommitOutboxEntry, OutboxOperation
from tests.contract.test_job_store_contract import SCENARIOS, record
from tests.integration.m2_support import database_settings
from tests.integration.test_cockpit_store_postgres_m35 import _alembic


async def test_job_store_contract_and_outbox_namespace_isolation(tmp_path: Path) -> None:
    async with build(
        settings=replace(database_settings(), artifact_root=tmp_path), storage="postgres"
    ) as composition:
        owner = composition.principal
        for check in SCENARIOS:
            scoped = owner.model_copy(update={"principal_id": f"jobs-{uuid4()}"})
            async with composition.uow_factory() as uow:
                await check(uow.jobs, scoped)
        now = composition.clock.now()
        entry = CommitOutboxEntry(
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            revision_id=uuid4(),
            slug="count-items",
            operation=OutboxOperation.WRITE,
            next_attempt_at=now,
            created_at=now,
        )
        async with composition.uow_factory() as uow:
            await uow.job_outbox.enqueue(entry)
            assert await uow.cockpit_outbox.due(owner.tenant_id, now=now, limit=10) == []
            assert await uow.job_outbox.due(owner.tenant_id, now=now, limit=10) == [entry]
            await uow.cockpit_outbox.erase_principal(owner)
            assert await uow.job_outbox.due(owner.tenant_id, now=now, limit=10) == [entry]
            await uow.job_outbox.complete(owner, entry.revision_id)


async def test_job_migration_refuses_data_loss_and_round_trips(tmp_path: Path) -> None:
    async with build(
        settings=replace(database_settings(), artifact_root=tmp_path), storage="postgres"
    ) as composition:
        owner = composition.principal
        async with composition.uow_factory() as uow:
            await uow.jobs.put(record(owner), expected_version=None)
        with pytest.raises(RuntimeError, match="pinned revisions must not be lost"):
            _alembic("downgrade", "c4e8a2d6f055")
        async with composition.uow_factory() as uow:
            await uow.jobs.erase_principal(owner)
    _alembic("downgrade", "c4e8a2d6f055")
    _alembic("upgrade", "head")
