"""The follow-up inbox on the real PostgreSQL adapter (ADR-0324)."""

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from agent_core.adapters.persistence.database import create_engine
from agent_core.bootstrap import build
from tests.contract.test_run_pending_input_repository_contract import (
    SCENARIOS,
    Scenario,
    pending,
)
from tests.integration.disposable_database import disposable_database_url
from tests.integration.m2_support import database_settings


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
