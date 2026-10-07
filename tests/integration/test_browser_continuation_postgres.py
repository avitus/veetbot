"""Durable authentication continuation uses the actual PostgreSQL checkpoint/queue."""

import pytest

from agent_core.bootstrap import build
from tests.contract.test_run_repository_contract import waiting_runs_contract
from tests.integration.m2_support import database_settings
from tests.unit import test_browser_recovery


async def test_postgres_waiting_browser_runs_are_scoped_and_paginated() -> None:
    async with build(settings=database_settings(), storage="postgres") as app:
        await waiting_runs_contract(app.uow_factory)


@pytest.mark.parametrize(
    "change", ["none", "recorded_ready", "cancelled", "revoked", "newer_ceremony"]
)
async def test_postgres_verified_continuation_is_durable_and_fenced(change: str) -> None:
    await test_browser_recovery.test_verified_browser_authentication_resumes_once(
        change, settings=database_settings(), storage="postgres"
    )
