"""The follow-up inbox on the real PostgreSQL adapter (ADR-0324)."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text, update

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.database import create_engine
from agent_core.adapters.persistence.sqlalchemy_models import RunRow
from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
from agent_core.bootstrap import build
from agent_core.domain.pending_inputs import follow_up_drop_derivation
from agent_core.domain.runs import RunStatus
from tests.contract.support import NOW
from tests.contract.test_run_pending_input_repository_contract import (
    SCENARIOS,
    Scenario,
    pending,
)
from tests.integration.disposable_database import disposable_database_url
from tests.integration.m2_support import database_settings
from tests.integration.test_cockpit_store_postgres_m35 import _alembic

PRIOR_HEAD = "e6b2d9f4a1c7"


@pytest.mark.parametrize("check", SCENARIOS, ids=lambda function: function.__name__)
async def test_postgres_run_pending_input_repository(check: Scenario, tmp_path: Path) -> None:
    async with build(
        settings=replace(database_settings(), artifact_root=tmp_path), storage="postgres"
    ) as composition:
        run_id = await composition.runs.submit("Start a long task.")
        async with composition.uow_factory() as uow:
            active = await uow.runs.get(run_id, composition.principal)
            await check(uow.pending_inputs, composition.principal, active)


async def test_deleting_the_session_removes_its_follow_ups(tmp_path: Path) -> None:
    async with build(
        settings=replace(database_settings(), artifact_root=tmp_path), storage="postgres"
    ) as composition:
        run_id = await composition.runs.submit("Start a long task.")
        async with composition.uow_factory() as uow:
            active = await uow.runs.get(run_id, composition.principal)
            await uow.pending_inputs.add(pending(composition.principal, active, 1))
    engine = create_engine(disposable_database_url())
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE id = :session_id"),
                {"session_id": active.session_id},
            )
            remaining = await connection.scalar(
                text("SELECT count(*) FROM run_pending_inputs WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
    finally:
        await engine.dispose()
    assert remaining == 0


async def test_downgrade_refuses_to_lose_a_held_follow_up(tmp_path: Path) -> None:
    async with build(
        settings=replace(database_settings(), artifact_root=tmp_path), storage="postgres"
    ) as composition:
        run_id = await composition.runs.submit("Start a long task.")
        async with composition.uow_factory() as uow:
            active = await uow.runs.get(run_id, composition.principal)
            held = await uow.pending_inputs.add(pending(composition.principal, active, 1))
        with pytest.raises(RuntimeError, match="held follow-ups must not be lost"):
            _alembic("downgrade", PRIOR_HEAD)
        async with composition.uow_factory() as uow:
            assert await uow.pending_inputs.get(held.id, composition.principal) == held
            await uow.pending_inputs.delete(held.id, composition.principal)
    _alembic("downgrade", PRIOR_HEAD)
    try:
        engine = create_engine(disposable_database_url())
        try:
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(text("SELECT to_regclass('run_pending_inputs')"))
                    is None
                )
        finally:
            await engine.dispose()
    finally:
        _alembic("upgrade", "head")


async def test_a_run_failed_by_the_crash_allowance_drops_what_it_held(tmp_path: Path) -> None:
    clock = FixedClock(NOW)
    async with build(
        settings=replace(database_settings(), artifact_root=tmp_path),
        storage="postgres",
        clock=clock,
    ) as composition:
        run_id = await composition.runs.submit("Start a long task.")
        async with composition.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork) and uow.queue is not None
            active = await uow.runs.get(run_id, composition.principal)
            held = await uow.pending_inputs.add(pending(composition.principal, active, 1))
            # One expiration short of the allowance, then a worker that never returns.
            await uow.session.execute(
                update(RunRow).where(RunRow.id == run_id).values(lease_expirations=2)
            )
            assert await uow.queue.claim("crashed-worker", [0]) is not None
        clock.advance(timedelta(seconds=31))
        async with composition.uow_factory() as uow:
            assert uow.queue is not None
            assert await uow.queue.reclaim_expired(10) == 1
        failed = await composition.runs.get(run_id)
        async with composition.uow_factory() as uow:
            inbox = await uow.pending_inputs.list_for_run(run_id, composition.principal)
            dropped = await uow.events.get_by_derivation(
                follow_up_drop_derivation(held.id), composition.principal
            )

    assert failed.status is RunStatus.FAILED
    assert inbox == []
    assert dropped is not None and dropped.run_id == run_id
    assert dropped.payload == {"pending_input_id": str(held.id), "reason": "run_failed"}
